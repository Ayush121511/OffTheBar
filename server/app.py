import os

from flask import Flask, redirect, url_for, session, render_template, abort, request
from google_auth_oauthlib.flow import Flow
import cachecontrol
import google.auth.transport.requests
from google.oauth2 import id_token
import requests
from flask_cors import CORS
from server.backend import conversation_endpoint
from config_env import require_env

app = Flask(__name__, template_folder='./../client/html')

app.secret_key = require_env("FLASK_SECRET_KEY") or os.urandom(32)

CORS(app, origins=["*"])


GOOGLE_CLIENT_ID = require_env("GOOGLE_CLIENT_ID")
SECRETS_DIR = require_env("OFFTHEBAR_SECRETS_DIR")
default_client_secrets_file = (
    os.path.join(SECRETS_DIR, "google-oauth-client-secret.json")
    if SECRETS_DIR
    else ""
)
client_secrets_file = require_env(
    "GOOGLE_OAUTH_CLIENT_SECRETS_FILE"
)
google_redirect_uri = require_env(
    "GOOGLE_REDIRECT_URI",
)

flow = None
if GOOGLE_CLIENT_ID and client_secrets_file and os.path.exists(client_secrets_file):
    flow = Flow.from_client_secrets_file(
        client_secrets_file=client_secrets_file,
        scopes=[
            "https://www.googleapis.com/auth/userinfo.profile",
            "https://www.googleapis.com/auth/userinfo.email",
            "openid",
        ],
        redirect_uri=google_redirect_uri,
    )

def login_is_required(function):
    def wrapper(*args,**kwargs):
        if "google_id" not in session:
            return abort(401) # Authorization required
        else:
            return function()
    return wrapper

@app.route("/login")
def login():
    if flow is None:
        return abort(500, description="Google OAuth is not configured.")
    authorization_url, state = flow.authorization_url()
    session["state"] = state
    return redirect(authorization_url)

@app.route("/callback")
def callback():
    if flow is None or not GOOGLE_CLIENT_ID:
        return abort(500, description="Google OAuth is not configured.")
    flow.fetch_token(authorization_response = request.url)
    if not session["state"] == request.args["state"]:
        abort(500) #State does not match
    credentials = flow.credentials
    request_session = requests.session()
    cached_session = cachecontrol.CacheControl(request_session)
    token_request = google.auth.transport.requests.Request(session=cached_session)

    id_info = id_token.verify_oauth2_token(
        id_token=credentials._id_token,
        request=token_request,
        audience=GOOGLE_CLIENT_ID
    )

    session["google_id"] = id_info.get("sub")
    session["name"] = id_info.get("name")
    return redirect("/chat/")

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")

@app.route("/")
def index():
    login_url = url_for('login')
    return render_template('login.html',login_url=login_url)

@app.route("/chat/")
# @login_is_required
def chat():
    return render_template('index.html')

@app.route("/api/chat", methods=["POST"])
def chat_api():
    # get raw payload
    payload = request.get_json(force=True)

    cfg = {
        "proxy": None
    }

    return conversation_endpoint(payload, cfg)

@app.errorhandler(Exception)
def global_handler(e):
    import traceback, json
    return json.dumps({"error": str(e), "trace": traceback.format_exc()}), 500
