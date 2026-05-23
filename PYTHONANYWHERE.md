# PythonAnywhere Setup

Keep code in Git and keep secrets in the PythonAnywhere filesystem outside the repo.

Recommended layout:

```text
/home/OffTheBar/OffTheBar/        # git clone
/home/OffTheBar/secrets/          # never committed
  google-oauth-client-secret.json
  google-service-account.json
```

Create `/home/OffTheBar/OffTheBar/.env` from `.env.example`, then fill in the real values.
`run.py` loads `.env` and `.env.sh` automatically before importing the Flask app.

Required environment values:

```bash
FLASK_SECRET_KEY="..."
GEMINI_API_KEY="..."
JINA_API_KEY="..."
HF_TOKEN="..."
PINECONE_API_KEY="..."
GOOGLE_CLIENT_ID="..."
GOOGLE_REDIRECT_URI="https://offthebar.pythonanywhere.com/callback"
OFFTHEBAR_SECRETS_DIR="/home/OffTheBar/secrets"
GOOGLE_OAUTH_CLIENT_SECRETS_FILE="/home/OffTheBar/secrets/google-oauth-client-secret.json"
GOOGLE_SERVICE_ACCOUNT_FILE="/home/OffTheBar/secrets/google-service-account.json"
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

After pulling this cleaned repo, upload the two JSON files into `/home/OffTheBar/secrets/`, not into `client/json/` or the repo root.
