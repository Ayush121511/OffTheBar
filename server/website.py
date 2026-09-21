from flask import render_template, send_file, redirect


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
        try:
            return send_file(f"./../client/{folder}/{file}", as_attachment=False)
        except:
            return "File not found", 404
