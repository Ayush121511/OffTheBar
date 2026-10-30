# Google Cloud Run Setup

Replaces PythonAnywhere as the host for `/api/chat`. Runs alongside the
existing `searxng-docker` Cloud Run service in the same GCP project — same
billing account, no new vendor.

## Current deployment (verified 2026-10-30)

Project `runofplay` (number `607277832880`), region `asia-south1`.

| Resource | What it is | State |
|---|---|---|
| `offthebar-api` (service) | this Flask backend | Live. Revision `offthebar-api-00010-4th` (deployed 2026-10-31), 100% traffic. Public (`allUsers` invoker). `--max-instances 2`, no minimum instances: the chat pages wake it with `GET /api/warm` on load. |
| `offthebar-football-data` (job) | `python football_data.py` on the same image | Every 6 hours, Cloud Scheduler job `offthebar-football-data-6h`. See step 7. |
| `offthebar-ingest-kb` (job) | `python ingest_kb.py` on the same image | Healthy since 2026-10-31, on the same image as `offthebar-api`. Failed every run from 2026-10-08 to 2026-10-30 (Hugging Face `402 Payment Required`; embeddings are now computed locally). Reads and writes the knowledge store in Cloud Storage. |
| `offthebar-ingest-kb-daily` (Cloud Scheduler) | triggers the job at `0 4 * * *` UTC | Enabled. |
| `searxng-docker` (service) | web search backend | Live. Request-based billing (CPU only during requests), 1 vCPU / 512Mi, scales to zero. |
| `runofplay-offthebar-kb` (Cloud Storage bucket) | the knowledge store `kb_store.json.gz` (step 6) | Created 2026-10-30, private, asia-south1. |
| `searxng-keep-warm` (Cloud Scheduler) | `GET /robots.txt` on `searxng-docker` every 10 min | Enabled 2026-10-30 (step 5). |
| `searx-proxy` | legacy Gen2 Cloud Function `searx_proxy` | Still ACTIVE, unused by this code. Candidate for deletion. |

API URL: `https://offthebar-api-607277832880.asia-south1.run.app/api/chat`
(`https://offthebar-api-tivjlndb6a-el.a.run.app` is the same service).
The Wix page already calls this URL (step 3 is done): request logs show
`/api/chat` traffic from `https://www.therunofplay.com` back to at least
2026-10-01.

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
for name in FLASK_SECRET_KEY GEMINI_API_KEY GEMINI_API_KEY_2 \
            PINECONE_API_KEY WIX_API_KEY WIX_SITE_ID; do
  gcloud secrets create "$name" --replication-policy="automatic" 2>/dev/null
  printf '%s' "REPLACE_ME" | gcloud secrets versions add "$name" --data-file=-
done
```

Set each secret's actual value (via `gcloud secrets versions add NAME
--data-file=-`, piping in the real value) instead of leaving `REPLACE_ME` —
the loop above just creates the slots. Add any other keys from
`.env.example` you use (e.g. `GEMINI_API_KEYS` if you use the CSV form
instead of numbered keys). The `HF_TOKEN` secret from the original setup is
no longer used (embeddings run locally since 2026-10-30) and can be deleted
once a deploy without it is live.

Then let the service's runtime account read them. Cloud Run's default
compute service account has no Secret Manager access, and the first real
deploy failed on exactly this (`Permission denied on secret ... for Revision
service account 607277832880-compute@developer.gserviceaccount.com`):

```bash
PROJECT_NUMBER=$(gcloud projects describe "$(gcloud config get-value project)" --format="value(projectNumber)")
for name in FLASK_SECRET_KEY GEMINI_API_KEY GEMINI_API_KEY_2 \
            PINECONE_API_KEY WIX_API_KEY WIX_SITE_ID; do
  gcloud secrets add-iam-policy-binding "$name" \
    --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
    --role="roles/secretmanager.secretAccessor"
done
```

### 2. Build and deploy

From the repo root (where the `Dockerfile` lives):

```bash
gcloud run deploy offthebar-api \
  --source . \
  --region asia-south1 \
  --allow-unauthenticated \
  --memory 2Gi \
  --cpu 2 \
  --max-instances 2 \
  --timeout 300 \
  --set-env-vars "PINECONE_INDEX_NAME=football-docs,PINECONE_CLOUD=aws,PINECONE_REGION=us-east-1,SEARXNG_URL=https://searxng-docker-607277832880.asia-south1.run.app,GEMINI_FREE_TIER_KEYS=2,KB_STORE=gs://runofplay-offthebar-kb/kb_store.json.gz,FOOTBALL_DATA_STORE=gs://runofplay-offthebar-kb/football_data.json.gz" \
  --set-secrets "FLASK_SECRET_KEY=FLASK_SECRET_KEY:latest,GEMINI_API_KEY=GEMINI_API_KEY:latest,GEMINI_API_KEY_2=GEMINI_API_KEY_2:latest,PINECONE_API_KEY=PINECONE_API_KEY:latest,WIX_API_KEY=WIX_API_KEY:latest,WIX_SITE_ID=WIX_SITE_ID:latest"
```

`GEMINI_FREE_TIER_KEYS=2`: `GEMINI_API_KEY_2` is a free-tier key and
`GEMINI_API_KEY` a paid one, so every Gemini call tries key 2 first and falls
back to key 1 (see `gemini_utils.py`).

`--memory 2Gi` (was 1Gi up to revision `00002`): the local embedding model adds
roughly 260MB per process (measured: ~80MB for fastembed/onnxruntime, ~180MB
for the loaded model), on top of headless Chromium during web scraping. At
current traffic the extra memory costs next to nothing.

`--source .` has Cloud Build build the `Dockerfile` and push straight to Cloud
Run — no separate `docker build`/`docker push`/Artifact Registry steps
needed. Re-run the same command to redeploy after code changes.

`--timeout 300` gives the two-phase (draft + forced-search refinement)
streaming response room; raise it if Phase 2 (SearXNG scrape + rank + Gemini)
routinely runs longer under real traffic.

The command prints a `*.run.app` URL when it finishes — that's your new
`API_URL`.

The service is public and the app has no API credential check or rate limit.
`--max-instances 2` above caps how far it scales under abuse, with room to
spare at the site's traffic. It limits how fast the Gemini key can be spent,
not whether it can: two instances of 8 threads still serve ~16 answers at once.
A rate limit is the real protection. The cap has been live since 2026-10-31. When both instances are full, Cloud
Run answers 429 or 503 and the Wix page shows a "try again" message. To change it:

```bash
gcloud run services update offthebar-api --region asia-south1 --max-instances 2
```

### 3. Point the Wix page at the new URL

**Status: done** (confirmed 2026-10-30 from the Velo page code and the
request logs). The Velo page code sets:

```js
const API_URL = 'https://offthebar-api-607277832880.asia-south1.run.app/api/chat';
```

No other change needed — the `fetch()`/streaming logic is host-agnostic.
Once it's switched, decommission PythonAnywhere (see `PYTHONANYWHERE.md`).

### 4. Scheduled `ingest_kb.py`

Replaces PythonAnywhere's Task Scheduler entirely. Ran cleanly every day
2026-09-21 → 2026-10-07; **every run since 2026-10-08 has failed** on
Hugging Face `402 Payment Required` (the job re-embedded all ~660 posts each
run through the paid API). Embeddings now run locally inside the image, so
the job needs no Hugging Face account; it recovers once it runs an image
built from the fixed code. Check run history with:
`gcloud run jobs executions list --job offthebar-ingest-kb --region asia-south1 --limit 10`

Reuses the same
image already pushed for `offthebar-api` — no second image to build or
store — just overrides the container command:

```bash
gcloud run jobs create offthebar-ingest-kb \
  --region asia-south1 \
  --image "asia-south1-docker.pkg.dev/<project>/cloud-run-source-deploy/offthebar-api@<current-sha>" \
  --command python \
  --args ingest_kb.py \
  --memory 1Gi --cpu 1 --task-timeout 900 --max-retries 1 \
  --set-env-vars "PINECONE_INDEX_NAME=football-docs,PINECONE_CLOUD=aws,PINECONE_REGION=us-east-1,GEMINI_FREE_TIER_KEYS=2,KB_STORE=gs://runofplay-offthebar-kb/kb_store.json.gz" \
  --set-secrets "PINECONE_API_KEY=PINECONE_API_KEY:latest,WIX_API_KEY=WIX_API_KEY:latest,WIX_SITE_ID=WIX_SITE_ID:latest,GEMINI_API_KEY=GEMINI_API_KEY:latest,GEMINI_API_KEY_2=GEMINI_API_KEY_2:latest"
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

### 5. Keep SearXNG warm

`searxng-docker` scales to zero after ~15 idle minutes, and the next search
then waits for a cold start: its request logs from 2026-10-29/30 show a median
of 8.0s (worst 10.7s) for the 8 searches that came after 15+ idle minutes,
against 0.34s for the other 127. With the site's light traffic, nearly every
real user's web search was a cold one. A Cloud Scheduler ping every 10
minutes keeps an instance up:

```bash
gcloud scheduler jobs create http searxng-keep-warm \
  --project runofplay --location asia-south1 \
  --schedule "*/10 * * * *" --time-zone "Etc/UTC" \
  --uri "https://searxng-docker-607277832880.asia-south1.run.app/robots.txt" \
  --http-method GET --attempt-deadline 30s
```

`/robots.txt` is a static 117-byte response (this SearXNG has no `/healthz`),
so a ping never reaches Bing. Cost: $0. It's the billing account's second
Cloud Scheduler job (3 are free; the account's other project has Scheduler
disabled), and `searxng-docker` uses request-based billing, so an idle warm
instance isn't charged - only the ~4,300 pings a month, a few milliseconds
each, inside Cloud Run's free tier. Cloud Run doesn't promise to keep an idle
instance, so a cold start can still happen now and then.

## Known limitation carried over from local dev

Cloud Run containers are stateless and can scale to zero or run multiple
concurrent instances — `chat_storage.py`'s per-conversation JSON files under
`chat_logs/` will **not** persist reliably here (same underlying issue
already flagged in CLAUDE.md's Future work section, just guaranteed to bite
immediately instead of only under hypothetical multi-worker gunicorn). If
conversation history persistence matters in production, that needs a real
datastore (Firestore is the natural pick, same GCP project) before or
shortly after this migration — not solved as part of this change.

## Security note

Secrets reach the service only as env vars from Secret Manager, and
`.dockerignore` keeps `.env` out of the image. Keep it that way: never `COPY` a
`.env` or key file into the image. (Revisions up to `00002` also had an
`/assets` path traversal that served repo files like `config.json`; fixed on
2026-10-30, see CLAUDE.md.)

## Local image test (optional, before deploying)

```bash
docker build -t offthebar-api .
docker run --rm -p 8080:8080 --env-file .env offthebar-api
curl -X POST http://localhost:8080/api/chat \
  -H "Content-Type: application/json" \
  -d '{"jailbreak": true, "meta": {"content": {"conversation": [], "parts": [{"content": "hi"}]}}}'
```

Gemini keys: `offthebar-ingest-kb` runs on the separate free key alone (secret
`GEMINI_DATA_API_KEY`, mapped to `GEMINI_API_KEY`, with `GEMINI_FREE_TIER_KEYS=1`),
so ingestion never spends on the paid key: anything past its quota is retried
the next day. `offthebar-api` keeps key 2 first with the paid key as fallback.

### 7. Football data (`FOOTBALL_DATA_STORE`)

`football_data.py` fetches tables, matches, squads, managers and top scorers
from football-data.org's free plan (about 5 minutes at 10 calls a minute) into
`gs://runofplay-offthebar-kb/football_data.json.gz`. `offthebar-api` re-reads
that file every 30 minutes and needs no key. Update this job's image whenever
`offthebar-api` is redeployed, as with `offthebar-ingest-kb`.

```bash
printf %s "$FOOTBALL_DATA_API_KEY" | gcloud secrets create FOOTBALL_DATA_API_KEY --data-file=-
gcloud secrets add-iam-policy-binding FOOTBALL_DATA_API_KEY \
  --member="serviceAccount:607277832880-compute@developer.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"

gcloud run jobs create offthebar-football-data --region asia-south1 \
  --image <the offthebar-api image> --command python --args football_data.py \
  --memory 512Mi --cpu 1 --task-timeout 900 --max-retries 0 \
  --set-env-vars FOOTBALL_DATA_STORE=gs://runofplay-offthebar-kb/football_data.json.gz \
  --set-secrets FOOTBALL_DATA_API_KEY=FOOTBALL_DATA_API_KEY:latest

gcloud run jobs add-iam-policy-binding offthebar-football-data --region asia-south1 \
  --member="serviceAccount:ingest-kb-scheduler@runofplay.iam.gserviceaccount.com" \
  --role="roles/run.invoker"

gcloud scheduler jobs create http offthebar-football-data-6h --location asia-south1 \
  --schedule "20 */6 * * *" --http-method POST \
  --uri https://asia-south1-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/runofplay/jobs/offthebar-football-data:run \
  --oauth-service-account-email ingest-kb-scheduler@runofplay.iam.gserviceaccount.com
```

The site must show "Football data provided by the Football-Data.org API"
(their terms, section 7.1), and the key belongs to one domain.

### 6. Knowledge store (`KB_STORE`)

`ingest_kb.py` writes every chunk's metadata, a content hash and the facts
extracted from it (lasting facts, time-bound facts, scheduled events, the
writer's opinions - see `kb_store.py`) to one gzipped JSON in Cloud Storage;
`offthebar-api` builds its keyword index from it and gives the answer model
old articles' facts, dated, instead of their text. Both read
`KB_STORE=gs://runofplay-offthebar-kb/kb_store.json.gz` (set in the commands
above); the default service account's `roles/editor` covers the bucket.

```bash
gcloud storage buckets create gs://runofplay-offthebar-kb --project runofplay \
  --location asia-south1 --uniform-bucket-level-access --public-access-prevention
```

Ingestion is incremental: only posts with a new or edited chunk (by hash), or
a chunk still missing its vector or facts, are embedded and sent to
gemini-2.5-flash-lite for facts (one call per post), so a daily run costs
fractions of a cent. The first full extraction (660 posts) is a one-time cost.
The ingestion job needs the Gemini keys for it (added to the job's secrets
above). Without `KB_STORE`, both use `.cache/kb_store.json.gz` (local dev).

