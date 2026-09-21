import os
from threading import Lock
from typing import Any, Callable, List, Optional

from google import genai
from google.genai import errors as genai_errors

_gemini_key_lock = Lock()
_gemini_key_index = 0


def _collect_gemini_api_keys() -> List[str]:
    keys: List[str] = []

    combined = os.getenv("GEMINI_API_KEYS", "").strip()
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


def next_gemini_api_key() -> tuple[str, int]:
    global _gemini_key_index

    keys = _collect_gemini_api_keys()
    with _gemini_key_lock:
        used_index = _gemini_key_index % len(keys)
        key = keys[used_index]
        _gemini_key_index += 1
        return key, used_index


def build_gemini_client() -> genai.Client:
    """
    Build a google-genai Client bound to the next rotated API key. Unlike the
    old google.generativeai SDK, a model isn't attached at build time - the
    model name is passed per-call (e.g. client.models.generate_content_stream
    (model=..., ...)), so callers choose the model and per-call config
    (safety settings, generation params) at the call site.
    """
    api_key, key_index = next_gemini_api_key()
    masked_key = (
        api_key[:8] + "..." + api_key[-4:]
        if len(api_key) > 12
        else "INVALID_KEY_FORMAT"
    )

    print(
        f"[OffTheBar Gemini] Using API key "
        f"#{key_index + 1}: {masked_key}"
    )
    return genai.Client(api_key=api_key)


def is_quota_exceeded_error(exc: Exception) -> bool:
    """Detect Gemini's per-key quota errors (e.g. the free tier's 20
    requests/day limit), so callers know it's worth retrying with a
    different key rather than giving up."""
    if isinstance(exc, genai_errors.ClientError) and exc.code == 429:
        return True
    text = str(exc)
    return (
        "429" in text
        or "RESOURCE_EXHAUSTED" in text
        or "quota" in text.lower()
    )


def _stream_keeping_client_alive(client: genai.Client, first_chunk: Any, iterator: Any):
    """Yield the already-fetched first chunk then the rest of `iterator`.
    `client` is only kept as a local so this generator's frame holds a
    strong reference to it for its whole lifetime - see the note in
    call_gemini_with_key_retry for why that's required."""
    yield first_chunk
    yield from iterator


def call_gemini_with_key_retry(
    model_name: str,
    call_fn: Callable[[genai.Client], Any],
    streaming: bool = False,
    max_attempts: Optional[int] = None,
):
    """
    Build a Gemini client and invoke call_fn(client), retrying with the next
    rotated API key whenever the call fails with a quota-exceeded error
    (each key has its own daily/per-minute quota, so a different key often
    just works). Tries every configured key at most once before giving up.

    call_fn is responsible for choosing the model and building its own
    per-call config (e.g. client.models.generate_content_stream(model=
    model_name, contents=..., config=types.GenerateContentConfig(...))).
    model_name here is only used for logging.

    For streaming responses (streaming=True), also validates the first
    chunk before returning - Gemini's SDK can raise the quota error lazily
    on first iteration rather than on the call itself - then hands back an
    equivalent iterator with that first chunk re-attached, so callers see
    the same streaming behavior as before.

    Important: genai.Client owns the underlying httpx connection pool and
    closes it once the Client object is garbage-collected - simply having
    the stream's internal generator reference the client isn't enough to
    keep it alive (confirmed: a stream reader crashes with "client has been
    closed" if the Client isn't kept referenced by something outside the
    SDK's own generator frames). So the streaming branch below returns a
    generator that keeps `client` alive in its own frame for as long as the
    caller is still iterating, instead of e.g. itertools.chain (which does
    not reference `client` at all).
    """
    keys = get_gemini_api_keys()
    attempts = max_attempts or len(keys)
    last_exc: Optional[Exception] = None

    for _ in range(attempts):
        try:
            client = build_gemini_client()
            response = call_fn(client)
            if not streaming:
                return response
            iterator = iter(response)
            first_chunk = next(iterator)
            return _stream_keeping_client_alive(client, first_chunk, iterator)
        except StopIteration:
            return iter(())
        except Exception as exc:
            if is_quota_exceeded_error(exc):
                print(f"[OffTheBar Gemini] Quota exceeded on {model_name}, rotating API key: {exc}")
                last_exc = exc
                continue
            raise

    raise last_exc
