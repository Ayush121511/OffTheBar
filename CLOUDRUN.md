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

### 4. Scheduled `ingest_kb.py`

Live, replacing PythonAnywhere's Task Scheduler entirely. Reuses the same
image already pushed for `offthebar-api` — no second image to build or
store — just overrides the container command:

```bash
gcloud run jobs create offthebar-ingest-kb \
  --region asia-south1 \
  --image "asia-south1-docker.pkg.dev/<project>/cloud-run-source-deploy/offthebar-api@<current-sha>" \
  --command python \
  --args ingest_kb.py \
  --memory 1Gi --cpu 1 --task-timeout 900 --max-retries 1 \
  --set-env-vars "PINECONE_INDEX_NAME=football-docs,PINECONE_CLOUD=aws,PINECONE_REGION=us-east-1" \
  --set-secrets "HF_TOKEN=HF_TOKEN:latest,PINECONE_API_KEY=PINECONE_API_KEY:latest,WIX_API_KEY=WIX_API_KEY:latest,WIX_SITE_ID=WIX_SITE_ID:latest"
```

Get the current image digest with:
`gcloud run services describe offthebar-api --region asia-south1 --format="value(spec.template.spec.containers[0].image)"`
— re-run this and update the job's image (`gcloud run jobs update
offthebar-ingest-kb --image ...`) whenever `offthebar-api` is redeployed
from new source, so the ingestion job stays on the same code.

Triggered daily via a dedicated least-privilege service account (only
`roles/run.invoker` on this one job, nothing else):

```bash
gcloud iam service-accounts create ingest-kb-scheduler \
  --display-name="Cloud Scheduler -> offthebar-ingest-kb invoker"

gcloud run jobs add-iam-policy-binding offthebar-ingest-kb \
  --region asia-south1 \
  --member="serviceAccount:ingest-kb-scheduler@<project>.iam.gserviceaccount.com" \
  --role="roles/run.invoker"

gcloud scheduler jobs create http offthebar-ingest-kb-daily \
  --location asia-south1 \
  --schedule="0 4 * * *" \
  --uri="https://asia-south1-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/<project>/jobs/offthebar-ingest-kb:run" \
  --http-method=POST \
  --oauth-service-account-email="ingest-kb-scheduler@<project>.iam.gserviceaccount.com" \
  --time-zone="Etc/UTC"
```

Cost: negligible. The job's compute (a few minutes of 1 vCPU/1GiB for ~640
posts) is a small fraction of Cloud Run's monthly free tier, shared with
`offthebar-api`; Cloud Scheduler's first 3 jobs per billing account are
free, so this one costs $0 unless other scheduler jobs already exist on the
account. Verified live: manual `gcloud run jobs execute offthebar-ingest-kb
--wait` completed successfully, 638 posts fetched, 1,723 vectors upserted
into Pinecone.

To trigger manually outside the daily schedule:
`gcloud run jobs execute offthebar-ingest-kb --region asia-south1 --wait`

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
