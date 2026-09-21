import os
from os import urandom
from time import time

from flask import Flask, redirect, render_template, request
from flask_cors import CORS
from server.backend import conversation_endpoint
from config_env import require_env

app = Flask(__name__, template_folder='./../client/html')

app.secret_key = require_env("FLASK_SECRET_KEY") or os.urandom(32)

CORS(app, origins=["*"])


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

@app.errorhandler(Exception)
def global_handler(e):
    import traceback, json
    return json.dumps({"error": str(e), "trace": traceback.format_exc()}), 500
