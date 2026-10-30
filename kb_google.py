from config_env import require_env

import os
from threading import Lock
from typing import List

# Embeddings are computed locally with fastembed's ONNX build of this model.
# The Pinecone index was built with the same model through Hugging Face's
# inference API, and the local vectors match the stored ones (cosine 1.0000
# on a sample of 30), so the index stays valid. Replaces the HF API, which
# started returning 402 Payment Required in 2026-10, and its fallback models,
# whose vectors weren't comparable with the index. fastembed caches the model
# under FASTEMBED_CACHE_PATH (the Docker image bakes it in there).
EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"

_embedding_model = None
_embedding_model_lock = Lock()


def _get_embedding_model():
    global _embedding_model
    with _embedding_model_lock:
        if _embedding_model is None:
            from fastembed import TextEmbedding
            _embedding_model = TextEmbedding(EMBEDDING_MODEL)
        return _embedding_model


def _embed(texts: List[str]):
    vectors = [vector.tolist() for vector in _get_embedding_model().embed(texts)]
    return vectors, EMBEDDING_MODEL

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
