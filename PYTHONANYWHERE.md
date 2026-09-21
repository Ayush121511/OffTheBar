# PythonAnywhere Setup

Keep code in Git and keep secrets in the PythonAnywhere filesystem outside the repo.

Recommended layout:

```text
/home/OffTheBar/OffTheBar/        # git clone
```

Create `/home/OffTheBar/OffTheBar/.env` from `.env.example`, then fill in the real values.
`run.py` loads `.env` and `.env.sh` automatically before importing the Flask app.

Required environment values:

```bash
FLASK_SECRET_KEY="..."
GEMINI_API_KEY="..."
HF_TOKEN="..."
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

## Scheduled Wix -> Pinecone ingestion

`ingest_kb.py` pulls articles from the Wix blog and (re)indexes them into
Pinecone. It needs `WIX_API_KEY` and `WIX_SITE_ID` in `.env` (see
`.env.example`) in addition to the app's required values above.

Run it periodically with PythonAnywhere's Task Scheduler ("Tasks" tab):

```bash
cd /home/OffTheBar/OffTheBar && python3 ingest_kb.py >> /home/OffTheBar/logs/ingest.log 2>&1
```

Scheduling limits as of 2026 (worth checking against your actual plan):
- Free accounts created before 2026-01-15 get exactly **one** daily task, at
  a fixed time.
- Free accounts created on/after 2026-01-15 have **no** scheduled tasks at
  all.
- Paid plans allow up to 20 tasks, each either daily at a fixed time or
  hourly at a fixed minute. There's no native "every N hours" option — to
  approximate a quarter-day cadence, create 4 separate daily tasks at fixed
  hours (e.g. 00:05, 06:05, 12:05, 18:05 UTC), each running the command
  above.

Re-running `ingest_kb.py` is safe: each chunk's Pinecone vector ID is
deterministic (`{post_id}-{chunk_index}`), so re-ingesting a post overwrites
its existing vectors instead of duplicating them or requiring a destructive
full-index wipe first.
