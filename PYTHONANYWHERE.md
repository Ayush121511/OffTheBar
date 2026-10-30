# PythonAnywhere Setup (legacy)

**Status (2026-10-30): legacy, no longer serving the site.** The backend runs on
Google Cloud Run — see `CLOUDRUN.md`. The Wix page's `API_URL` already points
at Cloud Run (its request logs show traffic from `https://www.therunofplay.com`
back to at least 2026-10-01), so this host can be shut down.

## Decommission checklist

1. ~~Change the Wix Velo page's `API_URL` to the Cloud Run URL~~ - done.
2. ~~Confirm real traffic shows up in the `offthebar-api` Cloud Run logs~~ - done (2026-10-30).
3. Disable the PythonAnywhere web app and any Task Scheduler entry for
   `ingest_kb.py` (ingestion now runs as the `offthebar-ingest-kb` Cloud Run Job).
4. Delete `/home/OffTheBar/OffTheBar/.env` from the server. It sits in the repo
   root, which the `/assets/<folder>/<file>` path-traversal bug in the code this
   host still runs can serve (fixed in the repo on 2026-10-30; see CLAUDE.md). If that bug turns out to be reachable
   through PythonAnywhere's front end, rotate every key that was in the file.

## Layout (as deployed)

```text
/home/OffTheBar/OffTheBar/        # git clone
/home/OffTheBar/OffTheBar/.env    # secrets - inside the repo root (gitignored, but see step 4 above)
```

`.env` is created from `.env.example` with the real values filled in.
`run.py` loads `.env` and `.env.sh` automatically before importing the Flask app.

Required environment values:

```bash
FLASK_SECRET_KEY="..."
GEMINI_API_KEY="..."
PINECONE_API_KEY="..."
OFFTHEBAR_CONFIG_PATH="/home/OffTheBar/OffTheBar/config.json"
```

Install dependencies in your PythonAnywhere virtualenv:

```bash
cd /home/OffTheBar/OffTheBar
pip install -r requirements.txt
```

In the PythonAnywhere WSGI file, point at the repo and import the app:

```python
import sys

project_home = "/home/OffTheBar/OffTheBar"
if project_home not in sys.path:
    sys.path.insert(0, project_home)

from run import app as application
```

## Scheduled Wix -> Pinecone ingestion (superseded)

Superseded by the `offthebar-ingest-kb` Cloud Run Job + Cloud Scheduler
(`CLOUDRUN.md`, step 4). Kept for reference only. Don't run both: re-runs are
idempotent (deterministic `{post_id}-{chunk_index}` vector IDs), but each run
re-embeds every post through Hugging Face, the likely reason the HF credits
ran out (see CLAUDE.md, Known issues).

The old Task Scheduler command was:

```bash
cd /home/OffTheBar/OffTheBar && python3 ingest_kb.py >> /home/OffTheBar/logs/ingest.log 2>&1
```

PythonAnywhere scheduling limits as of 2026: free accounts created before
2026-01-15 get one daily task; free accounts created on/after 2026-01-15 get
none; paid plans allow up to 20 daily-or-hourly tasks.
