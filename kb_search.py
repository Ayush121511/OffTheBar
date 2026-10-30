"""
Hybrid search over The Run of Play's articles in Pinecone.

Embedding similarity alone misses exact names and ranks a two-year-old piece
the same as yesterday's, so each search fuses four signals per chunk:

- dense:    Pinecone cosine similarity (meaning)
- bm25:     BM25 keyword score over every chunk, with the article's title and
            Wix tags counted double
- coverage: share of the query's words found in the chunk, title or tags
- recency:  exponential decay on the publish date

dense and bm25 are min-max normalised across the candidates (relative score
fusion); coverage and recency are already 0-1. Query words the index has
never seen are matched to the closest indexed word ("mesi" -> "messi"). Latest-news questions weight
recency heavily, everything else weights relevance. The top chunks are
always returned, whatever their age - callers label old ones as background.

The keyword index is a snapshot of every chunk's metadata, built from the Wix
blog the way ingest_kb.py builds what it stores in Pinecone (same ids, same
fields), in a background thread, and refreshed every few hours since ingestion
runs daily. It used to be read back from Pinecone, every vector included
(~30MB), on each start and refresh; that used up Pinecone's 1GB monthly read
allowance on 2026-10-30. A copy on disk (KB_SNAPSHOT_CACHE) serves restarts
within the refresh interval - local reloads and eval runs. A search that
arrives before the first snapshot waits for it (INDEX_WAIT_SECONDS), then ranks
Pinecone's candidates alone. On a cold Cloud Run instance the loading only gets
CPU while a request is open, which is what wait_until_ready() is for.

Pinecone calls get a deadline, and after a quota error Pinecone is skipped for
an hour: a request over the limit took ~12s of client retries to fail.
"""
import difflib
import json
import os
import re
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from nltk.stem import PorterStemmer
from rank_bm25 import BM25Okapi

import numpy as np

from kb_google import embed_texts, get_pinecone_index

DENSE_CANDIDATES = 30
LEXICAL_CANDIDATES = 30
# Recent chunks about the question's subject always join the candidates:
# BM25's top list alone misses them when filler words ("recent form results")
# outscore a common club name.
SUBJECT_RECENT_DAYS = 45
# A word in a tag or two names a subject only if it starts the tag or is rare
# in the articles ("Newcastle", "Kompany", not the "day" of Deadline Day).
SUBJECT_MIN_IDF = 2.0
SUBJECT_MAX_TAGS = 2
# Share of the subject a chunk's title and tags must name to count as about it.
ON_SUBJECT_SHARE = 0.5
MAX_CHUNKS_PER_ARTICLE = 2
SNAPSHOT_TTL_SECONDS = 6 * 60 * 60
SNAPSHOT_RETRY_SECONDS = 60
INDEX_WAIT_SECONDS = 20
SNAPSHOT_CACHE = Path(os.getenv("KB_SNAPSHOT_CACHE") or Path(__file__).resolve().parent / ".cache" / "kb_snapshot.json")
# Keyword-only candidates whose vectors are fetched for their real similarity
# (the best by BM25); the rest rank at the lowest dense score.
KEYWORD_ONLY_FETCH = 10
PINECONE_DEADLINE_SECONDS = 2
PINECONE_QUOTA_PAUSE_SECONDS = 60 * 60
PINECONE_SLOW_PAUSE_SECONDS = 60

WEIGHTS = {
    "latest": {"dense": 0.30, "bm25": 0.20, "coverage": 0.10, "recency": 0.40},
    "evergreen": {"dense": 0.45, "bm25": 0.25, "coverage": 0.15, "recency": 0.15},
}
RECENCY_HALF_LIFE_DAYS = {"latest": 30, "evergreen": 365}

STOPWORDS = {
    "a", "an", "the", "and", "or", "but", "of", "to", "in", "on", "at", "by",
    "for", "with", "from", "about", "as", "into", "over", "after", "before",
    "is", "are", "was", "were", "be", "been", "being", "do", "does", "did",
    "has", "have", "had", "will", "would", "can", "could", "should", "may",
    "might", "it", "its", "he", "his", "him", "she", "her", "they", "their",
    "them", "this", "that", "these", "those", "who", "whom", "what", "when",
    "where", "which", "why", "how", "me", "my", "we", "our", "you", "your",
    "tell", "give", "show", "any", "some", "all", "so", "than", "then",
    "there", "here", "not", "no", "yes", "up", "out", "just", "also",
    "latest", "news", "recent", "recently", "update", "updates", "today",
    "now", "current", "currently",
}

TITLE_SUFFIX = re.compile(r" - \d+$")  # ingest_kb names chunks "{title} - {n}"
FUZZY_MIN_LENGTH = 4
FUZZY_CUTOFF = 0.8

_stemmer = PorterStemmer()


@lru_cache(maxsize=100_000)
def _stem(word):
    return _stemmer.stem(word)


def _tokenize(text):
    """Accent-folded, lowercased, stemmed words, without stopwords."""
    folded = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    return [_stem(w) for w in re.findall(r"[a-z0-9]+", folded) if len(w) > 1 and w not in STOPWORDS]


def article_title(metadata):
    return metadata.get("title") or TITLE_SUFFIX.sub("", metadata.get("doc_name", "")).strip()


def _heading_text(metadata):
    """Title and tags - the words that say what the whole article is about.
    Tags are only in metadata ingested since tags were added."""
    return " ".join([article_title(metadata), *(metadata.get("tags") or [])])


def iso_age_days(value):
    """Whole days since an ISO-8601 timestamp (with or without fractional
    seconds / "Z"), or None if missing or unparseable."""
    if not value:
        return None
    try:
        published = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - published).days


class _Snapshot:
    """Every chunk's metadata plus a BM25 index over it."""

    def __init__(self, records):
        self.ids = list(records)
        self.metadata = [records[vid] for vid in self.ids]
        self.bm25 = BM25Okapi([
            _tokenize(_heading_text(md)) * 2 + _tokenize(md.get("chunk", ""))
            for md in self.metadata
        ])
        self.position = {vid: i for i, vid in enumerate(self.ids)}
        self.tags = {tag for md in self.metadata for tag in (md.get("tags") or [])}
        self.heading_terms = [set(_tokenize(_heading_text(md))) for md in self.metadata]
        self.tag_terms = [terms for terms in (set(_tokenize(tag)) for tag in self.tags) if terms]
        self.tag_first_words = {_tokenize(tag)[0] for tag in self.tags if _tokenize(tag)}
        self.tag_word_counts = {}
        for terms in self.tag_terms:
            for term in terms:
                self.tag_word_counts[term] = self.tag_word_counts.get(term, 0) + 1
        self.urls = {article_title(md): md["url"] for md in self.metadata if md.get("url")}
        self.vocabulary = {}
        for term in self.bm25.idf:
            self.vocabulary.setdefault(term[0], []).append(term)
        self.loaded_at = time.monotonic()

    def subject_terms(self, query_terms):
        """The query words that name who it's about: every word of a tag it
        spells out ("Manchester United", "Arsenal"), and words found in a tag
        or two that start the tag ("Newcastle") or are rare ("Kompany")."""
        subject = {
            term for term in query_terms
            if 0 < self.tag_word_counts.get(term, 0) <= SUBJECT_MAX_TAGS
            and (term in self.tag_first_words or self.bm25.idf.get(term, 0.0) >= SUBJECT_MIN_IDF)
        }
        for terms in self.tag_terms:
            if terms <= query_terms:
                subject |= terms
        return subject

    def correct(self, terms):
        """Swap words the index has never seen for the closest indexed word
        with the same first letter, so a typo still finds the article."""
        corrected = set()
        for term in terms:
            if term in self.bm25.idf or len(term) < FUZZY_MIN_LENGTH:
                corrected.add(term)
                continue
            match = difflib.get_close_matches(term, self.vocabulary.get(term[0], []), n=1, cutoff=FUZZY_CUTOFF)
            if match:
                print(f"[OffTheBar kb] fuzzy match: {term!r} -> {match[0]!r}")
            corrected.add(match[0] if match else term)
        return corrected


def _cached_records():
    """The chunk records saved on disk, if saved within SNAPSHOT_TTL_SECONDS."""
    try:
        saved = json.loads(SNAPSHOT_CACHE.read_text())
        if time.time() - saved["saved_at"] < SNAPSHOT_TTL_SECONDS and saved["records"]:
            return saved["records"]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def _save_records(records):
    try:
        SNAPSHOT_CACHE.parent.mkdir(parents=True, exist_ok=True)
        tmp = SNAPSHOT_CACHE.with_suffix(".tmp")
        tmp.write_text(json.dumps({"saved_at": time.time(), "records": records}))
        tmp.replace(SNAPSHOT_CACHE)
    except OSError as exc:
        print(f"[OffTheBar kb] couldn't save the keyword index to {SNAPSHOT_CACHE}: {exc}")


def _store_records():
    """Every chunk's metadata with its extracted facts, from the knowledge
    store ingestion writes (kb_store.py) - None if there isn't one yet."""
    from kb_store import load_store
    try:
        chunks = load_store().get("chunks", {})
    except Exception as exc:
        print(f"[OffTheBar kb] couldn't read the knowledge store: {exc}")
        return None
    return {vid: dict(entry["metadata"], facts=entry.get("facts")) for vid, entry in chunks.items()} or None


def _load_snapshot():
    records = _store_records()
    if records is not None:
        return _Snapshot(records)
    # No store yet: built from the Wix blog, without facts.
    records = _cached_records()
    if records is None:
        # Imported here: ingest_kb imports ragwithsearch, which imports this module.
        from ingest_kb import wix_chunk_records
        records = wix_chunk_records()
        if not records:
            raise RuntimeError("Wix returned no chunks")
        _save_records(records)
    return _Snapshot(records)


_pinecone_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="pinecone")
_pinecone_paused_until = 0.0


def _note_pinecone_error(exc):
    """After a quota error ("egress limit", 429), skip Pinecone for a while."""
    global _pinecone_paused_until
    text = str(exc).lower()
    if exc is not None and ("429" in text or "quota" in text or "egress" in text):
        _pinecone_paused_until = max(_pinecone_paused_until, time.monotonic() + PINECONE_QUOTA_PAUSE_SECONDS)
        print(f"[OffTheBar kb] Pinecone over its limit; skipping it for {PINECONE_QUOTA_PAUSE_SECONDS // 60} min: {str(exc)[:160]}")


def _pinecone(call):
    """call(index) with a deadline, unless Pinecone is paused after a quota error."""
    global _pinecone_paused_until
    if time.monotonic() < _pinecone_paused_until:
        raise RuntimeError("Pinecone skipped after a quota error")
    future = _pinecone_pool.submit(lambda: call(get_pinecone_index()))
    # A call that outlives the deadline still reports a quota error when it ends.
    future.add_done_callback(lambda f: f.cancelled() or _note_pinecone_error(f.exception()))
    try:
        return future.result(timeout=PINECONE_DEADLINE_SECONDS)
    except FutureTimeout:
        # A healthy call takes well under a second; until this one's error
        # (if any) arrives, don't make the next request wait too.
        _pinecone_paused_until = max(_pinecone_paused_until, time.monotonic() + PINECONE_SLOW_PAUSE_SECONDS)
        raise RuntimeError(f"Pinecone took over {PINECONE_DEADLINE_SECONDS}s") from None


_snapshot = None
_snapshot_lock = threading.Lock()
_first_load_done = threading.Event()
_connect_done = threading.Event()
_refreshing = False
_last_attempt = float("-inf")


def _refresh():
    global _snapshot, _refreshing
    started = time.monotonic()
    try:
        _snapshot = _load_snapshot()
        print(f"[OffTheBar kb] keyword index ready: {len(_snapshot.ids)} chunks in {time.monotonic() - started:.1f}s")
    except Exception as exc:
        print(f"[OffTheBar kb] keyword index load failed: {exc}")
    finally:
        _first_load_done.set()
        with _snapshot_lock:
            _refreshing = False


def article_url(title):
    """The page of the Run of Play article with this title, or None."""
    snapshot = _snapshot
    return snapshot.urls.get(title) if snapshot else None


def known_tags():
    """Every Wix tag on Run of Play's articles (clubs, players, competitions),
    or an empty set until the keyword index has loaded."""
    snapshot = _snapshot
    return snapshot.tags if snapshot else set()


def _connect():
    """Load the embedding model, open the Pinecone connection and start the
    scraper's browser once, all of which the first question otherwise paid for."""
    try:
        embed_texts(["warm up"])
        get_pinecone_index()
    except Exception as exc:
        print(f"[OffTheBar kb] warm-up failed: {exc}")
    try:
        import crawl4AI
        crawl4AI.warm_up()
    finally:
        _connect_done.set()


def start_up():
    """At startup, in the background: the keyword index, the embedding model,
    the Pinecone connection and the scraper's browser."""
    warm_up()
    threading.Thread(target=_connect, name="kb-connect", daemon=True).start()


def wait_until_ready(timeout):
    """Block until the keyword index, the embedding model and the Pinecone
    connection have finished loading, or `timeout` seconds pass. Returns
    whether they finished."""
    warm_up()
    deadline = time.monotonic() + timeout
    return all(event.wait(max(0, deadline - time.monotonic())) for event in (_first_load_done, _connect_done))


def warm_up():
    """Load the keyword index in the background if it's missing or stale."""
    global _refreshing, _last_attempt
    now = time.monotonic()
    with _snapshot_lock:
        fresh = _snapshot is not None and now - _snapshot.loaded_at < SNAPSHOT_TTL_SECONDS
        if fresh or _refreshing or now - _last_attempt < SNAPSHOT_RETRY_SECONDS:
            return
        _refreshing = True
        _last_attempt = now
    threading.Thread(target=_refresh, name="kb-keyword-index", daemon=True).start()


def _named_share(subject, heading):
    """How much of the question's subject a chunk's title and tags name."""
    return len(subject & heading) / len(subject) if subject else 1.0


def _relative(scores):
    """Min-max normalise to 0-1; all-equal positive scores count as 1."""
    if not scores:
        return {}
    low, high = min(scores.values()), max(scores.values())
    if high <= 0:
        return {key: 0.0 for key in scores}
    if high == low:
        return {key: 1.0 for key in scores}
    return {key: (value - low) / (high - low) for key, value in scores.items()}


def hybrid_search(query, latest_news=False, top_k=4):
    """
    Best Run of Play chunks for `query`, at most MAX_CHUNKS_PER_ARTICLE per
    article. Each hit is a dict with the chunk's metadata, title, age_days,
    the raw cosine similarity `dense` (None if it couldn't be computed), the
    other signals and the fused `score`. Empty only if both Pinecone and the
    keyword index are unavailable.
    """
    warm_up()
    # Without the index a cold instance has no hits at all while Pinecone is down.
    if _snapshot is None:
        _first_load_done.wait(INDEX_WAIT_SECONDS)
    snapshot = _snapshot
    mode = "latest" if latest_news else "evergreen"
    weights = WEIGHTS[mode]

    candidates = {}
    dense = {}
    query_vector = None
    try:
        query_vector = embed_texts([query])[0][0]
        results = _pinecone(lambda index: index.query(vector=query_vector, top_k=DENSE_CANDIDATES, include_metadata=True))
        for match in results["matches"]:
            dense[match["id"]] = match["score"]
            # The keyword index's copy carries the chunk's extracted facts.
            known = snapshot is not None and match["id"] in snapshot.position
            candidates[match["id"]] = snapshot.metadata[snapshot.position[match["id"]]] if known else dict(match["metadata"])
    except Exception as exc:
        print(f"Pinecone search failed: {exc}")

    query_terms = set(_tokenize(query))
    if snapshot is not None:
        query_terms = snapshot.correct(query_terms)
    subject = snapshot.subject_terms(query_terms) if snapshot is not None else set()
    bm25 = {}
    if snapshot is not None and query_terms:
        scores = snapshot.bm25.get_scores(list(query_terms))
        for i in scores.argsort()[::-1][:LEXICAL_CANDIDATES]:
            if scores[i] > 0:
                candidates.setdefault(snapshot.ids[i], snapshot.metadata[i])
        for i, heading in enumerate(snapshot.heading_terms):
            if subject and _named_share(subject, heading) >= ON_SUBJECT_SHARE:
                age = iso_age_days(snapshot.metadata[i].get("created"))
                if age is not None and age <= SUBJECT_RECENT_DAYS:
                    candidates.setdefault(snapshot.ids[i], snapshot.metadata[i])
        for vid in candidates:
            if vid in snapshot.position:
                bm25[vid] = float(scores[snapshot.position[vid]])
    elif snapshot is None:
        print("[OffTheBar kb] keyword index not loaded yet; ranking Pinecone candidates only")

    if not candidates:
        return []

    # Keyword-only hits fell outside Pinecone's top DENSE_CANDIDATES; fetch
    # their vectors so they're fused on their real similarity, not dropped to 0.
    keyword_only = sorted((vid for vid in candidates if vid not in dense), key=lambda vid: -bm25.get(vid, 0.0))
    keyword_only = keyword_only[:KEYWORD_ONLY_FETCH]
    if keyword_only and query_vector is not None and dense:
        try:
            fetched = _pinecone(lambda index: index.fetch(ids=keyword_only)).vectors
            q = np.asarray(query_vector)
            for vid, vector in fetched.items():
                v = np.asarray(vector.values)
                dense[vid] = float(q @ v / (np.linalg.norm(q) * np.linalg.norm(v)))
        except Exception as exc:
            print(f"[OffTheBar kb] couldn't fetch keyword-only vectors: {exc}")

    # Anything still missing ranks no higher on similarity than the lowest match.
    dense_floor = min(dense.values()) if dense else 0.0
    dense_norm = _relative({vid: dense.get(vid, dense_floor) for vid in candidates})
    bm25_norm = _relative({vid: bm25.get(vid, 0.0) for vid in candidates})

    hits = []
    for vid, md in candidates.items():
        title = article_title(md)
        terms = set(_tokenize(f"{_heading_text(md)} {md.get('chunk', '')}"))
        coverage = len(query_terms & terms) / len(query_terms) if query_terms else 0.0
        age = iso_age_days(md.get("created"))
        recency = 0.5 ** (max(age, 0) / RECENCY_HALF_LIFE_DAYS[mode]) if age is not None else 0.0
        score = (
            weights["dense"] * dense_norm[vid]
            + weights["bm25"] * bm25_norm[vid]
            + weights["coverage"] * coverage
            + weights["recency"] * recency
        )
        # An article about someone else can't lead, however new it is.
        share = _named_share(subject, set(_tokenize(_heading_text(md))))
        score *= 0.5 + 0.5 * share
        hits.append({
            "id": vid,
            "metadata": md,
            "title": title,
            "age_days": age,
            "dense": dense.get(vid),
            "bm25": bm25.get(vid, 0.0),
            "coverage": coverage,
            "recency": recency,
            "score": score,
            "on_subject": share >= ON_SUBJECT_SHARE,
        })

    hits.sort(key=lambda hit: hit["score"], reverse=True)
    picked, per_article = [], {}
    for hit in hits:
        article = hit["metadata"].get("doc_id") or hit["id"].rsplit("-", 1)[0]
        if per_article.get(article, 0) >= MAX_CHUNKS_PER_ARTICLE:
            continue
        per_article[article] = per_article.get(article, 0) + 1
        picked.append(hit)
        if len(picked) == top_k:
            break

    for hit in picked:
        dense_label = f"{hit['dense']:.2f}" if hit["dense"] is not None else " -- "
        age_label = f"{hit['age_days']}d" if hit["age_days"] is not None else "?d"
        print(
            f"[OffTheBar kb] {hit['score']:.2f} = dense {dense_label} bm25 {hit['bm25']:5.1f} "
            f"cover {hit['coverage']:.2f} recency {hit['recency']:.2f} | {age_label:>5} | {hit['title'][:70]}"
        )
    return picked
