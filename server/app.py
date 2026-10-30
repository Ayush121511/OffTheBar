import os
import traceback
from os import urandom
from time import time

from flask import Flask, redirect, render_template, request
from flask_cors import CORS
from werkzeug.exceptions import HTTPException
from server.backend import conversation_endpoint
from config_env import require_env
import kb_search

app = Flask(__name__, template_folder='./../client/html')

app.secret_key = require_env("FLASK_SECRET_KEY") or os.urandom(32)

CORS(app, origins=["*"])

WARM_WAIT_SECONDS = 45


@app.route("/")
def index():
    return redirect("/chat/")

def _new_chat_id() -> str:
    return f'{urandom(4).hex()}-{urandom(2).hex()}-{urandom(2).hex()}-{urandom(2).hex()}-{hex(int(time() * 1000))[2:]}'

@app.route("/chat/")
def chat():
    # A fresh chat_id per load means a browser refresh starts a new
    # conversation (chat.js keys localStorage history by this id).
    return render_template('index.html', chat_id=_new_chat_id())

@app.route("/api/chat", methods=["POST"])
def chat_api():
    # get raw payload
    payload = request.get_json(force=True)

    cfg = {
        "proxy": None
    }

    return conversation_endpoint(payload, cfg)

@app.route("/api/warm")
def warm_api():
    # The chat pages call this on load. A cold Cloud Run instance gets almost no
    # CPU between requests, so this stays open until start-up loading is done.
    return {"ready": kb_search.wait_until_ready(WARM_WAIT_SECONDS)}

@app.errorhandler(Exception)
def global_handler(e):
    # 404s, 405s and bad requests keep their own status. Anything else is
    # logged here and answered generically - the traceback (with server paths)
    # used to go back to the caller.
    if isinstance(e, HTTPException):
        return e
    traceback.print_exc()
    return {"error": "Internal server error"}, 500
