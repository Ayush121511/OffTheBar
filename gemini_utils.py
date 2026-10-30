import os
import re
import time
from datetime import datetime, timedelta
from threading import Lock
from typing import Any, Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from google import genai
from google.genai import errors as genai_errors

# Keys are tried in a fixed order instead of round-robin: free-tier keys first
# (they cost nothing), then the rest (paid). GEMINI_FREE_TIER_KEYS names the
# free ones by number - "2" for GEMINI_API_KEY_2, "1" for GEMINI_API_KEY,
# "csv1", "csv2", ... for entries of GEMINI_API_KEYS. When a key hits a quota
# it's skipped for that model until the quota should be back, so an exhausted
# free key doesn't add a failed round trip to every call.

# Free-tier daily quotas reset at midnight Pacific time.
QUOTA_TIMEZONE = ZoneInfo("America/Los_Angeles")
DEFAULT_RATE_LIMIT_COOLDOWN = 60
BAD_KEY_COOLDOWN = 60 * 60
RETRY_DELAY = re.compile(r"retryDelay['\"]?\s*[:=]\s*['\"]?(\d+(?:\.\d+)?)s")

_cooldown_lock = Lock()
_cooldown_until: Dict[Tuple[str, str], float] = {}   # (key label, model) -> time.time()


def _collect_gemini_api_keys() -> List[Tuple[str, str]]:
    """(label, key) pairs in preference order: free-tier keys, then the rest."""
    keys: List[Tuple[str, str]] = []

    combined = os.getenv("GEMINI_API_KEYS", "").strip()
    if combined:
        parts = [part.strip() for part in combined.split(",") if part.strip()]
        keys.extend((f"csv{i}", part) for i, part in enumerate(parts, start=1))

    numbered_index = 1
    while True:
        env_name = "GEMINI_API_KEY" if numbered_index == 1 else f"GEMINI_API_KEY_{numbered_index}"
        value = os.getenv(env_name, "").strip()
        if value:
            keys.append((str(numbered_index), value))
            numbered_index += 1
            continue

        if numbered_index == 1:
            numbered_index += 1
            continue
        break

    deduped_keys: List[Tuple[str, str]] = []
    seen = set()
    for label, key in keys:
        if key not in seen:
            seen.add(key)
            deduped_keys.append((label, key))

    if not deduped_keys:
        raise ValueError(
            "At least one Gemini API key must be set via GEMINI_API_KEY, "
            "GEMINI_API_KEY_2, or GEMINI_API_KEYS."
        )

    free = _free_tier_labels()
    return [k for k in deduped_keys if k[0] in free] + [k for k in deduped_keys if k[0] not in free]


def _free_tier_labels() -> set:
    return {part.strip() for part in os.getenv("GEMINI_FREE_TIER_KEYS", "").split(",") if part.strip()}


def get_gemini_api_keys() -> List[str]:
    return [key for _, key in _collect_gemini_api_keys()]


def _key_name(label: str) -> str:
    return f"#{label} ({'free' if label in _free_tier_labels() else 'paid'})"


def _seconds_until_quota_reset() -> float:
    now = datetime.now(QUOTA_TIMEZONE)
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=5, microsecond=0)
    return (midnight - now).total_seconds()


def _is_bad_key_error(exc: Exception) -> bool:
    text = str(exc)
    return getattr(exc, "code", None) in (401, 403) or "API_KEY_INVALID" in text or "PERMISSION_DENIED" in text


def _cooldown_seconds(exc: Exception) -> float:
    """How long a key that failed with this error should be skipped."""
    text = f"{exc} {getattr(exc, 'details', '')}"
    if _is_bad_key_error(exc):
        return BAD_KEY_COOLDOWN
    if "PerDay" in text:
        return _seconds_until_quota_reset()
    match = RETRY_DELAY.search(text)
    return float(match.group(1)) + 1 if match else DEFAULT_RATE_LIMIT_COOLDOWN


def _duration(seconds: float) -> str:
    return f"{seconds:.0f}s" if seconds < 120 else f"{seconds / 60:.0f} min"


def _cool_down(label: str, model_name: str, seconds: float) -> None:
    with _cooldown_lock:
        _cooldown_until[(label, model_name)] = time.time() + seconds


def _keys_to_try(model_name: str) -> List[Tuple[str, str]]:
    """Preference order, keys cooling down for this model moved to the end
    (still tried as a last resort - a cooldown is only an estimate)."""
    keys = _collect_gemini_api_keys()
    now = time.time()
    with _cooldown_lock:
        ready = [k for k in keys if _cooldown_until.get((k[0], model_name), 0) <= now]
    return ready + [k for k in keys if k not in ready]


def is_quota_exceeded_error(exc: Exception) -> bool:
    """Detect Gemini's per-key quota errors (e.g. the free tier's daily
    request limit), so callers know it's worth retrying with a different
    key rather than giving up."""
    if isinstance(exc, genai_errors.ClientError) and exc.code == 429:
        return True
    text = str(exc)
    return (
        "429" in text
        or "RESOURCE_EXHAUSTED" in text
        or "quota" in text.lower()
    )


def _is_key_specific_error(exc: Exception) -> bool:
    """Errors a different key might not hit: quota, a bad or revoked key, and
    server-side failures (500/503 "overloaded", timeouts). A 400 for a bad
    request would fail the same way on every key, so it isn't retried."""
    if is_quota_exceeded_error(exc) or _is_bad_key_error(exc):
        return True
    if getattr(exc, "code", None) in (500, 502, 503, 504):
        return True
    text = str(exc)
    return any(marker in text for marker in ("UNAVAILABLE", "DEADLINE_EXCEEDED", "overloaded"))


def _log_usage(model_name: str, label: str, usage: Any) -> None:
    if usage is None:
        return
    print(
        f"[OffTheBar Gemini] {model_name} via key {_key_name(label)}: "
        f"input {getattr(usage, 'prompt_token_count', None) or 0}, "
        f"output {getattr(usage, 'candidates_token_count', None) or 0}, "
        f"thinking {getattr(usage, 'thoughts_token_count', None) or 0} tokens"
    )


def _stream_keeping_client_alive(client: genai.Client, first_chunk: Any, iterator: Any, model_name: str, label: str):
    """Yield the already-fetched first chunk then the rest of `iterator`,
    logging the token usage (sent on the last chunk) when the stream ends.
    `client` is only kept as a local so this generator's frame holds a
    strong reference to it for its whole lifetime - see the note in
    call_gemini_with_key_retry for why that's required."""
    usage = getattr(first_chunk, "usage_metadata", None)
    try:
        yield first_chunk
        for chunk in iterator:
            usage = getattr(chunk, "usage_metadata", None) or usage
            yield chunk
    finally:
        _log_usage(model_name, label, usage)


def call_gemini_with_key_retry(
    model_name: str,
    call_fn: Callable[[genai.Client], Any],
    streaming: bool = False,
    max_attempts: Optional[int] = None,
):
    """
    Invoke call_fn(client) with the preferred API key (see the top of this
    module), moving on to the next key whenever the call fails in a way
    another key might not: quota exceeded, a bad key, or a server error.
    Tries every configured key at most once before giving up.

    call_fn is responsible for choosing the model and building its own
    per-call config (e.g. client.models.generate_content_stream(model=
    model_name, contents=..., config=types.GenerateContentConfig(...))).
    model_name must match it: quota cooldowns are tracked per model.

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
    keys = _keys_to_try(model_name)[: max_attempts or None]
    last_exc: Optional[Exception] = None

    for label, api_key in keys:
        try:
            print(f"[OffTheBar Gemini] {model_name} using key {_key_name(label)}")
            client = genai.Client(api_key=api_key)
            response = call_fn(client)
            if not streaming:
                _log_usage(model_name, label, getattr(response, "usage_metadata", None))
                return response
            iterator = iter(response)
            first_chunk = next(iterator)
            return _stream_keeping_client_alive(client, first_chunk, iterator, model_name, label)
        except StopIteration:
            return iter(())
        except Exception as exc:
            if not _is_key_specific_error(exc):
                raise
            # Quota and bad-key failures will repeat for a while; a server
            # error is retried on the next key but not held against this one.
            cooldown = _cooldown_seconds(exc) if is_quota_exceeded_error(exc) or _is_bad_key_error(exc) else 0
            if cooldown:
                _cool_down(label, model_name, cooldown)
            print(
                f"[OffTheBar Gemini] {model_name} failed on key {_key_name(label)}"
                f"{f', skipping it for {_duration(cooldown)}' if cooldown else ''}: {str(exc)[:200]}"
            )
            last_exc = exc

    raise last_exc
