"""
The Run of Play knowledge store: every article chunk's metadata, a hash of its
content, whether its vector is in Pinecone, and the facts extracted from it at
ingestion (ingest_kb.py) - lasting facts, time-bound facts, scheduled events
and the writer's opinions. One gzipped JSON file:

    {"updated_at": "...", "chunks": {"<vector id>": {"hash": "...", "embedded": true,
        "metadata": {...the Pinecone metadata...}, "facts": {...} or null}}}

ingest_kb.py writes it and only processes chunks whose hash changed or that
lack a vector or facts, so a daily run touches only new or edited posts.
kb_search.py builds its keyword index from it, facts included.

Where it lives: KB_STORE - "gs://bucket/path" in production (Cloud Storage,
same project and region), a local path otherwise (default .cache/kb_store.json.gz).
"""
import gzip
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_STORE = Path(__file__).resolve().parent / ".cache" / "kb_store.json.gz"


def store_location() -> str:
    return os.getenv("KB_STORE") or str(DEFAULT_STORE)


def chunk_hash(metadata: dict) -> str:
    """Changes when the chunk's text, title or tags change."""
    content = json.dumps([metadata.get("title"), metadata.get("tags"), metadata.get("chunk")], ensure_ascii=False)
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def _gcs_blob(location: str):
    from google.cloud import storage  # only needed for gs:// stores
    bucket, _, name = location[len("gs://"):].partition("/")
    return storage.Client().bucket(bucket).blob(name)


def read_json_gz(location: str):
    """The gzipped JSON at a local path or gs:// location, or None if there is none."""
    try:
        if location.startswith("gs://"):
            blob = _gcs_blob(location)
            if not blob.exists():
                return None
            data = blob.download_as_bytes()
        else:
            data = Path(location).read_bytes()
    except FileNotFoundError:
        return None
    return json.loads(gzip.decompress(data))


def load_store() -> dict:
    """The store, or an empty one if there isn't one yet."""
    return read_json_gz(store_location()) or {"chunks": {}}


def save_store(store: dict) -> None:
    write_json_gz(store_location(), store)


def write_json_gz(location: str, store: dict) -> None:
    store["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    data = gzip.compress(json.dumps(store, ensure_ascii=False).encode("utf-8"))
    if location.startswith("gs://"):
        _gcs_blob(location).upload_from_string(data, content_type="application/gzip")
    else:
        path = Path(location)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
