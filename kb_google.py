from config_env import require_env

import os, time, requests
from typing import List

# Primary + fallbacks (all sentence-embedding models)
MODELS: List[str] = [
    "BAAI/bge-small-en-v1.5",                 # primary: small, fast, very popular
    "sentence-transformers/all-MiniLM-L6-v2", # fallback 1: tiny + fast
    "Snowflake/snowflake-arctic-embed-xs",    # fallback 2: very small, fast
]

ROUTER = "https://router.huggingface.co/hf-inference/models/{model}/pipeline/feature-extraction"
_hf_token_shape_logged = False

def _hf_headers():
    global _hf_token_shape_logged
    token = require_env('HF_TOKEN')
    if not _hf_token_shape_logged:
        print(
            "[OffTheBar config] HF_TOKEN loaded "
            f"length={len(token)} prefix={token[:3]!r} suffix={token[-4:]!r}"
        )
        _hf_token_shape_logged = True
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

def _embed(texts: List[str], max_retries_per_model: int = 2, backoff_seconds: float = 1.0):
    last_err = None
    for model in MODELS:
        url = ROUTER.format(model=model)
        for attempt in range(1, max_retries_per_model + 1):
            try:
                resp = requests.post(
                    url,
                    headers=_hf_headers(),
                    json={"inputs": texts},
                    timeout=5   # <-- timeout reduced to 5 seconds
                )
                if resp.status_code == 401:
                    print(
                        "[OffTheBar config] Hugging Face returned 401. "
                        f"Response: {resp.text[:200]}"
                    )
                resp.raise_for_status()
                out = resp.json()
                # If shaped like [[vector], [vector], ...], return as-is
                if isinstance(out, list) and out and isinstance(out[0], list) and isinstance(out[0][0], (int, float)):
                    if len(texts) == 1 and all(isinstance(x, (int, float)) for x in out):
                        return [out], model
                    return out, model
                # Mean pool if token-level embeddings returned
                if isinstance(out, list) and out and isinstance(out[0], list) and isinstance(out[0][0], list):
                    pooled = []
                    for item in out:
                        vec = [sum(col)/len(col) for col in zip(*item)]
                        pooled.append(vec)
                    return pooled, model
                raise ValueError(f"Unexpected output shape from {model}: {type(out)}")
            except Exception as e:
                last_err = e
                if attempt < max_retries_per_model:
                    time.sleep(backoff_seconds * attempt)
                else:
                    pass
    raise RuntimeError(f"All embedding models failed. Last error: {last_err}")

print("Loading Pinecone")
from pinecone import Pinecone, ServerlessSpec
proxy_url = os.getenv("https_proxy") or os.getenv("http_proxy")

index = None


def _get_index():
    global index
    if index is not None:
        return index

    pc = Pinecone(
        api_key=require_env("PINECONE_API_KEY"),
        proxy_url=proxy_url,
    )
    print("Pinecone connected")
    index_name = os.getenv("PINECONE_INDEX_NAME", "football-docs")
    if index_name not in pc.list_indexes().names():
        pc.create_index(
            name=index_name,
            dimension=384,
            metric='cosine',
            spec=ServerlessSpec(
                cloud=os.getenv("PINECONE_CLOUD", "aws"),
                region=require_env("PINECONE_REGION"),
            )
        )

    print("Connecting to index")
    index = pc.Index(index_name)
    return index


def embed_texts(texts: List[str]):
    """Public entry point for embedding text (used by ingest_kb.py)."""
    return _embed(texts)


def get_pinecone_index():
    """Public entry point for the shared Pinecone index handle (used by ingest_kb.py)."""
    return _get_index()


def search_pinecone(query, top_k=3):
    query_list = [query]
    q_vec,model = _embed(query_list)                # same 384-d vector, no local model
    print(model)
    results = _get_index().query(
        vector=q_vec[0],
        top_k=top_k,
        include_metadata=True
    )
    return results["matches"]

if __name__ == "__main__":
    print("Searching Pinecone...")
    query = "emi martinez bids farwell to villa park with strong interest from barcelona"
    matches = search_pinecone(query)
    print(len(matches))

    for match in matches:
        print(f"[{match['score']:.2f}] {match['metadata']['doc_name']} - {match['metadata']['created']} \n\n {match['metadata']['chunk']} ")
