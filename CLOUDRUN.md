# Google Cloud Run Setup

Replaces PythonAnywhere as the host for `/api/chat`. Runs alongside the
existing `searxng-docker` Cloud Run service in the same GCP project — same
billing account, no new vendor.

## Why Cloud Run (not a plain serverless function)

`crawl4AI.py` drives Playwright/Chromium to scrape search-result pages, and
`ingest_kb.py`/the retrieval path need a normal long-lived Python process.
Cloud Run runs a full custom Docker container, so both work unmodified —
most PaaS free tiers (Vercel, Wix Velo's own backend, etc.) block headless
browser automation entirely. See the `Dockerfile` at the repo root.

## One-time setup

```bash
gcloud auth login
gcloud config set project <your-gcp-project-id>   # same project as searxng-docker
```

### 1. Store secrets in Secret Manager (don't bake them into the image)

```bash
for name in FLASK_SECRET_KEY GEMINI_API_KEY GEMINI_API_KEY_2 HF_TOKEN \
            PINECONE_API_KEY WIX_API_KEY WIX_SITE_ID; do
  gcloud secrets create "$name" --replication-policy="automatic" 2>/dev/null
  printf '%s' "REPLACE_ME" | gcloud secrets versions add "$name" --data-file=-
done
```

Set each secret's actual value (via `gcloud secrets versions add NAME
--data-file=-`, piping in the real value) instead of leaving `REPLACE_ME` —
the loop above just creates the slots. Add any other keys from
`.env.example` you use (e.g. `GEMINI_API_KEYS` if you use the CSV form
instead of numbered keys).

### 2. Build and deploy

From the repo root (where the `Dockerfile` lives):

```bash
gcloud run deploy offthebar-api \
  --source . \
  --region asia-south1 \
  --allow-unauthenticated \
  --memory 1Gi \
  --cpu 1 \
  --timeout 300 \
  --set-env-vars "PINECONE_INDEX_NAME=football-docs,PINECONE_CLOUD=aws,PINECONE_REGION=us-east-1,SEARXNG_URL=https://searxng-docker-607277832880.asia-south1.run.app" \
  --set-secrets "FLASK_SECRET_KEY=FLASK_SECRET_KEY:latest,GEMINI_API_KEY=GEMINI_API_KEY:latest,GEMINI_API_KEY_2=GEMINI_API_KEY_2:latest,HF_TOKEN=HF_TOKEN:latest,PINECONE_API_KEY=PINECONE_API_KEY:latest,WIX_API_KEY=WIX_API_KEY:latest,WIX_SITE_ID=WIX_SITE_ID:latest"
```

`--source .` has Cloud Build build the `Dockerfile` and push straight to Cloud
Run — no separate `docker build`/`docker push`/Artifact Registry steps
needed. Re-run the same command to redeploy after code changes.

`--timeout 300` gives the two-phase (draft + forced-search refinement)
streaming response room; raise it if Phase 2 (SearXNG scrape + rank + Gemini)
routinely runs longer under real traffic.

The command prints a `*.run.app` URL when it finishes — that's your new
`API_URL`.

### 3. Point the Wix page at the new URL

In the Velo page code you shared, update:

```js
const API_URL = 'https://offthebar-api-<hash>-<region>.a.run.app/api/chat';
```

No other change needed — the `fetch()`/streaming logic is host-agnostic.

### 4. (Optional) Scheduled `ingest_kb.py`

PythonAnywhere's Task Scheduler ran this periodically. On Cloud Run, either:
- Deploy it as a [Cloud Run job](https://cloud.google.com/run/docs/create-jobs)
  (`gcloud run jobs deploy offthebar-ingest --source . --command python
  --args ingest_kb.py ...`) and trigger it with Cloud Scheduler, or
- Keep running it manually / from a local cron for now.

## Known limitation carried over from local dev

Cloud Run containers are stateless and can scale to zero or run multiple
concurrent instances — `chat_storage.py`'s per-conversation JSON files under
`chat_logs/` will **not** persist reliably here (same underlying issue
already flagged in CLAUDE.md's Future work section, just guaranteed to bite
immediately instead of only under hypothetical multi-worker gunicorn). If
conversation history persistence matters in production, that needs a real
datastore (Firestore is the natural pick, same GCP project) before or
shortly after this migration — not solved as part of this change.

## Local image test (optional, before deploying)

```bash
docker build -t offthebar-api .
docker run --rm -p 8080:8080 --env-file .env offthebar-api
curl -X POST http://localhost:8080/api/chat \
  -H "Content-Type: application/json" \
  -d '{"jailbreak": true, "meta": {"content": {"conversation": [], "parts": [{"content": "hi"}]}}}'
```
