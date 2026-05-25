from config_env import require_env
print("Importing SentenceTransformer and tqdm...")


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

# Load documents
# folder_ids = ['1rzb_1wHcb3_p1I7h1La5p-KHBmubik2g','1TaavTQ8fXUheM2nB8wBQ32GhjK78ns8G','1P3YXnV4GOUhIPK-tPHFAuc9ir42ITbLp']

# for folder_id in folder_ids:
#     docs = list_google_docs_with_dates(folder_id)

#     chunks = []
#     metadatas = []

#     def split_into_chunks(text, chunk_size=2000):
#         return [text[i:i+chunk_size] for i in range(0, len(text), chunk_size)]

#     for doc in tqdm(docs):
#         full_text = extract_text_from_doc(doc['id'])
#         # Simple chunking by paragraph or fixed length
#         paragraphs = [p.strip() for p in split_into_chunks(full_text) if p.strip()]
#         for i, para in enumerate(paragraphs):
#             chunks.append(para)
#             metadatas.append({
#         "doc_id": doc['id'],
#         "doc_name": doc['name'] + f" - {i+1}",
#         "doc_size": len(para),
#         "created": doc['createdTime'],
#         "modified": doc['modifiedTime'],
#         "chunk": para
#     })


# index.delete(
#         deleteAll=True  )
    # import uuid

    # vectors = []
    # embeddings = model.encode(chunks)

    # for i, embedding in enumerate(embeddings):
    #     vectors.append({
    #         "id": str(uuid.uuid4()),
    #         "values": embedding.tolist(),

    #         "metadata": metadatas[i]
    #     })

    # # Batch upserts (max 100 per call)
    # for i in range(0, len(vectors), 100):
    #     index.upsert(vectors[i:i+100])

    # stats = index.describe_index_stats()
    # print(stats)

    # def search_pinecone(query, top_k=3):
    #     q_vec = model.encode([query])[0]
    #     results = index.query(vector=q_vec.tolist(), top_k=top_k, include_metadata=True)
    #     return results['matches']

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
