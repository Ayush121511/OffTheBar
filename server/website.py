from pathlib import Path

from flask import abort, redirect, render_template, send_from_directory

CLIENT_DIR = Path(__file__).resolve().parent.parent / "client"


class Website:
    def __init__(self, app) -> None:
        self.app = app
        self.routes = {
            '/chat/<conversation_id>': {
                'function': self._chat,
                'methods': ['GET', 'POST']
            },
            '/assets/<folder>/<file>': {
                'function': self._assets,
                'methods': ['GET', 'POST']
            }
        }

    def _chat(self, conversation_id):
        if not '-' in conversation_id:
            return redirect(f'/chat')

        return render_template('index.html', chat_id=conversation_id)

    def _assets(self, folder: str, file: str):
        # send_from_directory refuses paths that leave client/ - send_file on
        # the raw segments served repo-root files like .env for folder="..".
        if folder.startswith("."):
            abort(404)
        return send_from_directory(CLIENT_DIR, f"{folder}/{file}")
