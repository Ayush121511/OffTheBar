"""
Pull articles from The Run of Play's Wix blog and (re)index them into Pinecone.

Replaces the old manual "copy each article into Google Drive" step: this
reads posts directly from the live Wix blog via Wix's Blog REST API
(https://dev.wix.com/api/rest/wix-blog), using the `CONTENT_TEXT` fieldset so
Wix returns plain text directly instead of needing to parse its Rich Content
(Ricos) JSON format.

Requires two env vars (see .env.example):
    WIX_API_KEY  - created in Wix's API Key Manager, scoped to the Blog app
                   (SCOPE.DC-BLOG.READ-BLOGS), targeted at your site.
    WIX_SITE_ID  - your Wix site's ID.

Run manually:
    python ingest_kb.py

Intended to also run periodically via PythonAnywhere's Task Scheduler (see
PYTHONANYWHERE.md) to keep the index in sync with new/edited posts.

Safe to re-run: each chunk's Pinecone vector ID is deterministic
(f"{post_id}-{chunk_index}"), so re-ingesting the same post overwrites its
existing vectors instead of creating duplicates or requiring a destructive
full-index wipe beforehand.
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from typing import Any, Dict, List

import requests
from google.genai import types as genai_types

from config_env import require_env
from gemini_utils import call_gemini_with_key_retry
from kb_google import embed_texts, get_pinecone_index
from kb_store import chunk_hash, load_store, save_store, store_location
from prompt_templates import build_chunk_facts_prompt
from ragwithsearch import chunk_article

WIX_API_BASE = "https://www.wixapis.com"
# A post's page on the site (the API has its slug, not its URL).
POST_URL = "https://www.therunofplay.com/post/"
PAGE_LIMIT = 100
CHUNK_MAX_CHARS = 1500
UPSERT_BATCH_SIZE = 100
# Posts processed at once (embedding, upsert and the facts call).
INGEST_WORKERS = 8
FACTS_MODEL = "gemini-2.5-flash-lite"
FACTS_KEYS = ("lasting_facts", "time_bound_facts", "scheduled_events", "opinions")
# Output cap per chunk: a chunk's facts run ~200 tokens, but now and then
# flash-lite loops until its 65k-token limit (7 of ~1,400 calls on
# 2026-10-30, ~Rs 2.5 each). A cut-off reply is retried once, warmer.
FACTS_TOKENS_PER_CHUNK = 600
# Bump when build_chunk_facts_prompt changes: chunks with facts from an older
# version are extracted again. 2: running season tallies are time-bound, not
# lasting ("Haaland: 20 goals in 27 starts in 2024-25" was filed as lasting).
FACTS_VERSION = 2


def _wix_headers() -> Dict[str, str]:
    return {
        "Authorization": require_env("WIX_API_KEY"),
        "wix-site-id": require_env("WIX_SITE_ID"),
    }


def list_wix_posts() -> List[Dict[str, Any]]:
    """Page through every post on the Wix blog, plain text content included."""
    posts: List[Dict[str, Any]] = []
    offset = 0

    while True:
        response = requests.get(
            f"{WIX_API_BASE}/v3/posts",
            headers=_wix_headers(),
            params={
                "fieldsets": "CONTENT_TEXT",
                "paging.limit": PAGE_LIMIT,
                "paging.offset": offset,
            },
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()

        page = data.get("posts", [])
        posts.extend(page)

        if len(page) < PAGE_LIMIT:
            break
        offset += PAGE_LIMIT

    return posts


def list_wix_tags() -> Dict[str, str]:
    """Every blog tag's label by ID (posts only carry tag IDs)."""
    labels: Dict[str, str] = {}
    offset = 0

    while True:
        response = requests.get(
            f"{WIX_API_BASE}/v3/tags",
            headers=_wix_headers(),
            params={"paging.limit": PAGE_LIMIT, "paging.offset": offset},
            timeout=15,
        )
        response.raise_for_status()
        page = response.json().get("tags", [])
        labels.update({tag["id"]: tag.get("label", "") for tag in page if tag.get("id")})

        if len(page) < PAGE_LIMIT:
            break
        offset += PAGE_LIMIT

    return labels


def chunk_records(post: Dict[str, Any], tag_labels: Dict[str, str] | None = None) -> List[Dict[str, Any]]:
    """A post's chunks as {"id", "metadata"} - the ids and metadata stored in
    Pinecone. kb_search builds its keyword index from these too."""
    post_id = post["id"]
    title = post.get("title", "")
    content_text = (post.get("contentText") or "").strip()
    created = post.get("firstPublishedDate", "")
    modified = post.get("lastPublishedDate", created)
    tags = [tag_labels[t] for t in post.get("tagIds", []) if tag_labels and tag_labels.get(t)]
    if not content_text:
        return []
    return [
        {
            "id": f"{post_id}-{i}",
            "metadata": {
                "doc_id": post_id,
                "doc_name": f"{title} - {i + 1}",
                "title": title,
                "tags": tags,
                "doc_size": len(chunk),
                "created": created,
                "modified": modified,
                "chunk": chunk,
                "url": f"{POST_URL}{post['slug']}" if post.get("slug") else "",
            },
        }
        for i, chunk in enumerate(chunk_article(content_text, max_chunk_length=CHUNK_MAX_CHARS))
    ]


def wix_chunk_records() -> Dict[str, Dict[str, Any]]:
    """Every chunk's metadata by vector id, straight from the Wix blog."""
    tag_labels = list_wix_tags()
    return {record["id"]: record["metadata"] for post in list_wix_posts() for record in chunk_records(post, tag_labels)}


def _vectors_for_post(post: Dict[str, Any], tag_labels: Dict[str, str] | None = None) -> List[Dict[str, Any]]:
    records = chunk_records(post, tag_labels)
    title = post.get("title", "")
    if not records:
        print(f"Skipping '{title}' ({post['id']}) - no content text")
        return []
    # Each chunk is embedded with its article's title in front, so a chunk
    # that only says "he" still matches searches for who the article is about.
    embeddings, model_used = embed_texts([f"{title}\n\n{record['metadata']['chunk']}" for record in records])
    print(f"'{title}': {len(records)} chunks, embedded with {model_used}")
    return [dict(record, values=embedding) for record, embedding in zip(records, embeddings)]


def extract_facts(title: str, published: str, chunks: List[str]) -> List[Dict[str, List[str]] | None]:
    """Each chunk's facts (FACTS_KEYS) from one flash-lite call for the whole
    post; None for a chunk the call didn't cover or if it failed."""
    try:
        published_on = datetime.fromisoformat(published.replace("Z", "+00:00")).date()
    except (ValueError, AttributeError):
        published_on = date.today()
    prompt = build_chunk_facts_prompt(title, published_on, chunks)
    parsed = None
    for attempt in range(2):
        config = genai_types.GenerateContentConfig(
            temperature=0.3 if attempt else 0, response_mime_type="application/json",
            max_output_tokens=300 + FACTS_TOKENS_PER_CHUNK * len(chunks),
        )
        try:
            response = call_gemini_with_key_retry(
                FACTS_MODEL,
                lambda client: client.models.generate_content(model=FACTS_MODEL, contents=prompt, config=config),
            )
            text = (response.text or "").strip()
            # raw_decode ignores anything after the JSON ("Extra data").
            parsed, _ = json.JSONDecoder().raw_decode(text[min(i for i in (text.find("{"), text.find("["), len(text)) if i >= 0):])
            break
        except Exception as exc:
            # Occasionally malformed JSON ("Expecting ',' delimiter"): one retry.
            if attempt:
                print(f"Facts failed for '{title}': {exc}")
                return [None] * len(chunks)
    # Asked for {"chunks": [...]}, but for some posts flash-lite answers with
    # the bare list, or leaves out the chunk numbers.
    entries = parsed.get("chunks") if isinstance(parsed, dict) else parsed
    entries = [entry for entry in entries or [] if isinstance(entry, dict)]
    by_number = {entry.get("chunk", n): entry for n, entry in enumerate(entries, 1)}
    if len(by_number) < len(chunks):
        print(f"Facts for '{title}' cover {len(by_number)} of {len(chunks)} chunks")
    return [
        {key: [str(item) for item in (by_number[n].get(key) or [])] for key in FACTS_KEYS} if n in by_number else None
        for n in range(1, len(chunks) + 1)
    ]


def ingest(assume_embedded: bool = False) -> None:
    """Bring Pinecone and the knowledge store (kb_store.py) up to date with
    the Wix blog. Only posts with a new or edited chunk, or a chunk still
    missing its vector or facts, are embedded and sent for facts; chunks
    that no longer exist are deleted from Pinecone. assume_embedded (once,
    when starting a store while Pinecone is already current) skips
    re-embedding chunks the store has no entry for."""
    print(f"[{datetime.now(timezone.utc).isoformat()}] Fetching posts from Wix...")
    posts = list_wix_posts()
    tag_labels = list_wix_tags()
    print(f"Found {len(posts)} posts, {len(tag_labels)} tags. Store: {store_location()}")

    previous = load_store().get("chunks", {})
    chunks: Dict[str, Dict[str, Any]] = {}
    pending = []
    for post in posts:
        records = chunk_records(post, tag_labels)
        for record in records:
            entry = previous.get(record["id"]) or {}
            unchanged = entry.get("hash") == chunk_hash(record["metadata"])
            chunks[record["id"]] = {
                "hash": chunk_hash(record["metadata"]),
                "metadata": record["metadata"],
                "embedded": (unchanged and entry.get("embedded", False)) or (assume_embedded and not entry),
                "facts": entry.get("facts") if unchanged and entry.get("facts_version") == FACTS_VERSION else None,
                "facts_version": FACTS_VERSION,
            }
        if any(not chunks[r["id"]]["embedded"] or chunks[r["id"]]["facts"] is None for r in records):
            pending.append((post, records))
    print(f"{len(chunks)} chunks; {len(pending)} posts new, edited or incomplete.")

    index = get_pinecone_index()

    def process(item) -> None:
        post, records = item
        ids = [record["id"] for record in records]
        if not all(chunks[vid]["embedded"] for vid in ids):
            try:
                index.upsert(vectors=_vectors_for_post(post, tag_labels))
                for vid in ids:
                    chunks[vid]["embedded"] = True
            except Exception as exc:
                print(f"Upsert failed for '{post.get('title', '')}': {exc}")
        if any(chunks[vid]["facts"] is None for vid in ids):
            facts = extract_facts(post.get("title", ""), post.get("firstPublishedDate", ""),
                                  [record["metadata"]["chunk"] for record in records])
            for vid, chunk_facts in zip(ids, facts):
                chunks[vid]["facts"] = chunk_facts

    try:
        with ThreadPoolExecutor(INGEST_WORKERS) as pool:
            for done, _ in enumerate(pool.map(process, pending), 1):
                if done % 50 == 0 or done == len(pending):
                    print(f"  processed {done}/{len(pending)} posts")
    finally:
        # Saved even if interrupted: unfinished chunks are retried next run.
        save_store({"chunks": chunks})

    gone = [vid for vid in previous if vid not in chunks]
    if gone:
        print(f"Deleting {len(gone)} vectors of removed or shortened posts...")
        for i in range(0, len(gone), 1000):
            index.delete(ids=gone[i:i + 1000])
    missing = sum(1 for entry in chunks.values() if not entry["embedded"] or entry["facts"] is None)
    print(f"Done. {len(chunks)} chunks stored; {missing} still missing a vector or facts.")


if __name__ == "__main__":
    import sys
    ingest(assume_embedded="--assume-embedded" in sys.argv)
