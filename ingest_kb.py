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

from datetime import datetime, timezone
from typing import Any, Dict, List

import requests

from config_env import require_env
from ragwithsearch import chunk_article
from kb_google import embed_texts, get_pinecone_index

WIX_API_BASE = "https://www.wixapis.com"
PAGE_LIMIT = 100
CHUNK_MAX_CHARS = 1500
UPSERT_BATCH_SIZE = 100


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


def _vectors_for_post(post: Dict[str, Any]) -> List[Dict[str, Any]]:
    post_id = post["id"]
    title = post.get("title", "")
    content_text = (post.get("contentText") or "").strip()
    created = post.get("firstPublishedDate", "")
    modified = post.get("lastPublishedDate", created)

    if not content_text:
        print(f"Skipping '{title}' ({post_id}) - no content text")
        return []

    chunks = chunk_article(content_text, max_chunk_length=CHUNK_MAX_CHARS)
    embeddings, model_used = embed_texts(chunks)
    print(f"'{title}': {len(chunks)} chunks, embedded with {model_used}")

    vectors = []
    for i, (chunk, embedding) in enumerate(zip(chunks, embeddings)):
        vectors.append({
            "id": f"{post_id}-{i}",
            "values": embedding,
            "metadata": {
                "doc_id": post_id,
                "doc_name": f"{title} - {i + 1}",
                "doc_size": len(chunk),
                "created": created,
                "modified": modified,
                "chunk": chunk,
            },
        })
    return vectors


def ingest() -> None:
    print(f"[{datetime.now(timezone.utc).isoformat()}] Fetching posts from Wix...")
    posts = list_wix_posts()
    print(f"Found {len(posts)} posts.")

    index = get_pinecone_index()
    all_vectors: List[Dict[str, Any]] = []
    for post in posts:
        all_vectors.extend(_vectors_for_post(post))

    print(f"Upserting {len(all_vectors)} vectors into Pinecone...")
    for i in range(0, len(all_vectors), UPSERT_BATCH_SIZE):
        batch = all_vectors[i:i + UPSERT_BATCH_SIZE]
        index.upsert(vectors=batch)
        print(f"  upserted {i + len(batch)}/{len(all_vectors)}")

    print("Done.")


if __name__ == "__main__":
    ingest()
