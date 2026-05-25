import os
from threading import Lock
from typing import List, Optional
from config_env import require_env

import google.generativeai as genai

_gemini_key_lock = Lock()
_gemini_key_index = 0


def _collect_gemini_api_keys() -> List[str]:
    keys: List[str] = []

    combined = require_env("GEMINI_API_KEYS")
    if combined:
        keys.extend(part.strip() for part in combined.split(",") if part.strip())

    numbered_index = 1
    while True:
        env_name = "GEMINI_API_KEY" if numbered_index == 1 else f"GEMINI_API_KEY_{numbered_index}"
        value = os.getenv(env_name, "").strip()
        if value:
            keys.append(value)
            numbered_index += 1
            continue

        if numbered_index == 1:
            numbered_index += 1
            continue
        break

    deduped_keys: List[str] = []
    for key in keys:
        if key not in deduped_keys:
            deduped_keys.append(key)

    if not deduped_keys:
        raise ValueError(
            "At least one Gemini API key must be set via GEMINI_API_KEY, "
            "GEMINI_API_KEY_2, or GEMINI_API_KEYS."
        )

    return deduped_keys


def get_gemini_api_keys() -> List[str]:
    return _collect_gemini_api_keys()


def next_gemini_api_key() -> str:
    global _gemini_key_index

    keys = _collect_gemini_api_keys()
    with _gemini_key_lock:
        key = keys[_gemini_key_index % len(keys)]
        _gemini_key_index += 1
        return key


def build_gemini_model(model_name: str, safety_settings: Optional[list] = None):
    api_key = next_gemini_api_key()
    masked_key = (
        api_key[:8] + "..." + api_key[-4:]
        if len(api_key) > 12
        else "INVALID_KEY_FORMAT"
    )

    print(
        f"[OffTheBar Gemini] Using API key "
        f"#{current_index + 1}: {masked_key}"
    )
    genai.configure(api_key=api_key)
    if safety_settings is None:
        return genai.GenerativeModel(model_name)
    return genai.GenerativeModel(model_name, safety_settings=safety_settings)
