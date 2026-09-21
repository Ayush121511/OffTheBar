"""
Persist chat conversations to disk so they survive beyond the browser's
localStorage (which is per-browser, wiped by clearing site data, and never
shared across devices).

One JSON file per conversation, under CHAT_LOG_DIR, appended to after every
exchange.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Optional

CHAT_LOG_DIR = Path(__file__).resolve().parent / "chat_logs"

_write_lock = Lock()


def _safe_conversation_id(conversation_id: Optional[str]) -> str:
    """Keep only filename-safe characters; falls back to 'unknown' if empty."""
    cleaned = "".join(c for c in (conversation_id or "") if c.isalnum() or c in "-_")
    return cleaned or "unknown"


def save_chat_turn(conversation_id: Optional[str], user_message: str, assistant_message: str) -> None:
    """Append one user/assistant exchange to that conversation's log file."""
    if not user_message and not assistant_message:
        return

    CHAT_LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = CHAT_LOG_DIR / f"{_safe_conversation_id(conversation_id)}.json"
    now = datetime.now(timezone.utc).isoformat()

    with _write_lock:
        try:
            data = json.loads(log_path.read_text()) if log_path.exists() else None
        except Exception:
            data = None
        if not isinstance(data, dict) or "messages" not in data:
            data = {"conversation_id": conversation_id or "unknown", "messages": []}

        if user_message:
            data["messages"].append({"role": "user", "content": user_message, "timestamp": now})
        if assistant_message:
            data["messages"].append({"role": "assistant", "content": assistant_message, "timestamp": now})

        log_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
