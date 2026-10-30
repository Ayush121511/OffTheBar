"""
Football analysis backend API with streaming Gemini responses.

"""

from __future__ import annotations
import json
import random
import re
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional
from flask import Response, request, stream_with_context
from google.genai import types as genai_types

from chat_storage import save_chat_turn
from gemini_utils import call_gemini_with_key_retry, get_gemini_api_keys
from prompt_templates import (
    build_answer_turn,
    build_chat_system_prompt,
    build_complete_context_instruction,
    build_first_draft_instruction,
    build_follow_ups_only_prompt,
    build_refinement_prompt,
)
import football_data
from ragwithsearch import ask_from_llm, error_messages, judge_kb_coverage, order_context, run_of_play_sources

# What the answer is waiting on, streamed as a "## HALFTIME!!! <status>" line
# at each stage: Run of Play search, web search, then "Generating" until the
# first word. The clients (wix/offthebar-page.js, client/js/chat.js) show only
# the latest status, as a quiet "working" line with a seconds counter, while
# nothing follows it, and strip every status line from the answer. The
# "## HALFTIME!!!" prefix is the marker they look for - keep it, or older
# embeds show the raw line.
HALFTIME_MARKER = "## HALFTIME!!! "
STATUS_RUN_OF_PLAY = "Searching The Run of Play"
STATUS_WEB = "Searching the web"
STATUS_GENERATING = "Generating"
# After the answer, for clients that ask (payload "sources": true): the Run of
# Play articles it drew on, as "## SOURCES!!! [{"title": ..., "url": ...}]".
# The clients show them as links at the end of the answer and strip the line.
SOURCES_MARKER = "## SOURCES!!! "
NO_NEW_UPDATES = "NO_NEW_UPDATES"   # Phase 2's "nothing to add" reply (prompt_templates.build_refinement_prompt)
# Phase 1's reply when Run of Play's articles hold nothing that answers the
# question (prompt_templates.build_first_draft_instruction): instead of a
# draft built on stale material, the answer goes straight to the web search
# and one combined answer.
NO_RELEVANT_CONTEXT = "NO_RELEVANT_CONTEXT"
HALFTIME_LINE = re.compile(r"^##\s*HALFTIME!!!.*$", re.M)
# Gemini 2.5 Flash thinks before its first visible word, and thinking is billed
# as output. Phase 1 is a ~60-word draft: measured 676 thinking tokens for 66
# visible ones. Complete answers and Phase 2 thought for 1,100-2,350 tokens,
# 6-13s before the first word - the bulk of their wait.
# 0 = off, None = Gemini's default (dynamic) thinking.
PHASE1_THINKING_BUDGET: Optional[int] = 0
ANSWER_THINKING_BUDGET: Optional[int] = 0   # complete answers and Phase 2


def _thinking_config(budget: Optional[int]) -> Optional[genai_types.ThinkingConfig]:
    return genai_types.ThinkingConfig(thinking_budget=budget) if budget is not None else None

# The web search starts as soon as the classifier wants one, alongside the Run
# of Play search, the coverage check and the Phase 1 draft, instead of after
# them. When the articles turn out to cover the question it goes unused.
WEB_PREFETCH_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="web-prefetch")


def _status(text: str) -> bytes:
    return f"{HALFTIME_MARKER}{text}\n\n".encode("utf-8")

GEMINI_SAFETY_SETTINGS = [
    {
        "category": "HARM_CATEGORY_HARASSMENT",
        "threshold": "BLOCK_MEDIUM_AND_ABOVE",
    },
    {
        "category": "HARM_CATEGORY_HATE_SPEECH",
        "threshold": "BLOCK_MEDIUM_AND_ABOVE",
    },
    {
        "category": "HARM_CATEGORY_SEXUALLY_EXPLICIT",
        "threshold": "BLOCK_MEDIUM_AND_ABOVE",
    },
    {
        "category": "HARM_CATEGORY_DANGEROUS_CONTENT",
        "threshold": "BLOCK_MEDIUM_AND_ABOVE",
    },
]


def _build_llm_query(conversation_history: List[Dict[str, str]], latest_question: str) -> str:
    """
    Compose the single text blob ask_from_llm expects: prior assistant/model
    context, prior user questions, then the question to answer now.
    """
    prior_context = "\n".join(
        entry["content"] for entry in conversation_history if entry["role"] != "user"
    )
    prior_user_queries = "\n".join(
        entry["content"] for entry in conversation_history if entry["role"] == "user"
    )
    return (
        f"Context given:{prior_context}"
        f"Previous Query:{prior_user_queries}"
        f"\nMain query to be answered:{latest_question}"
    )


def _without_sentinel(
    pieces: Iterable[str],
    sentinel: str,
    found: Optional[List[bool]] = None,
    keep_rest: bool = True,
) -> Iterable[str]:
    """
    Pass streamed text through as it arrives, holding back only the start
    until it can't be `sentinel` (NO_NEW_UPDATES, NO_RELEVANT_CONTEXT), which
    must never reach the user. If the text does start with it, `found` gets
    True appended, and whatever follows the sentinel is yielded only when
    `keep_rest`. (Phase 2 used to be collected whole and sent in one piece
    for this check, so the update appeared all at once.)
    """
    pending: Optional[str] = ""
    for piece in pieces:
        if pending is None:
            yield piece
            continue
        pending += piece
        head = pending.lstrip()
        if sentinel.startswith(head) or head.startswith(sentinel):
            continue
        yield pending
        pending = None

    if pending is not None:
        rest = pending.strip()
        if rest.startswith(sentinel):
            if found is not None:
                found.append(True)
            rest = rest[len(sentinel):].strip() if keep_rest else ""
        if rest:
            yield rest


def _to_gemini_role(role: str) -> str:
    if role == "assistant":
        return "model"
    return role


def _extract_finish_reason(chunk: Any) -> Optional[str]:
    candidates = getattr(chunk, "candidates", None)
    if not candidates:
        return None

    candidate = candidates[0]
    finish_reason = getattr(candidate, "finish_reason", None)
    if finish_reason is None:
        return None
    return str(finish_reason)


def _extract_text(chunk: Any) -> str:
    try:
        text = getattr(chunk, "text", None)
        if text:
            return text
    except Exception:
        pass

    candidates = getattr(chunk, "candidates", None) or []
    collected_parts: List[str] = []
    for candidate in candidates:
        content = getattr(candidate, "content", None)
        parts = getattr(content, "parts", None) or []
        for part in parts:
            part_text = getattr(part, "text", None)
            if part_text:
                collected_parts.append(part_text)
    return "".join(collected_parts)


def _friendly_llm_error_message(exc: Exception, err_msgs: List[str]) -> str:
    message = str(exc)
    if "API key" in message or "environment variable is not set" in message:
        return "Off The Bar is missing a server configuration value. Please set the required environment variables and try again."
    return random.choice(err_msgs)


class Backend_Api:

    """
    Backend API for handling football-related conversational requests.

    This class encapsulates the route handling for a single endpoint that:
      1) Builds a system message with tone and rules for a football analyst.
      2) Optionally performs a web search (disabled by default).
      3) Uses an LLM helper (ask_from_llm) to determine the need for search
         and to prepare contextual "news content".
      4) Streams responses from Gemini (phase 1: first-draft context;
         phase 2: refined with updated/latest context).
    """

    def __init__(self, app: Any, config: Dict[str, Any]) -> None:
        """
        Initialize the API with Flask app and config.

        Args:
            app: The Flask application instance (or None if not needed).
            config: Configuration dictionary containing:
                - 'proxy': Optional proxy settings
        """
        self.app = app
        self.proxy = config["proxy"]
        self.gemini_api_keys = get_gemini_api_keys()

        # Route registry (in case you autowire these elsewhere)
        self.routes = {
            "/backend-api/v2/conversation": {
                "function": self._conversation,
                "methods": ["POST"],
            }
        }

    def _conversation(self, payload: Optional[Dict[str, Any]] = None) -> Response:
        """
        Handle a conversation request and return a streaming response.

        This endpoint orchestrates:
          - System prompt assembly
          - Context and "news_content" resolution via ask_from_llm
          - Phase 1: Fast streaming answer with "first draft" context
          - Phase 2: Optional refinement with updated context

        Args:
            payload: Optional payload (dict). If None, uses flask.request.json.

        Returns:
            A Flask Response object streaming Gemini output as text/event-stream.
        """
        try:
            # Backward-compatible: pull JSON from Flask request when not provided
            if payload is None:
                payload = request.json

            jailbreak = payload["jailbreak"]
            conversation_id = payload.get("conversation_id")
            # Clients that show a status per stage say so. Older ones - the
            # live Wix page until the new code is published, or a tab opened
            # before that - strip only one status line and render any other as
            # a "HALFTIME!!! ..." heading, so they get just the one line they
            # were built for: before the web update or the combined answer.
            stage_statuses = bool(payload.get("stage_statuses"))
            want_sources = bool(payload.get("sources"))

            def stage_status(text: str) -> List[bytes]:
                return [_status(text)] if stage_statuses else []

            conversation_history = payload["meta"]["content"]["conversation"]
            prompt = payload["meta"]["content"]["parts"][0]

            # System message governing tone, style, and operational rules
            system_message = build_chat_system_prompt(datetime.now())

            extra: List[Dict[str, str]] = []

            llm_query = _build_llm_query(conversation_history, prompt["content"])
            web_prefetch: List[Future] = []
            # Every piece of context this answer was written from, for the log.
            used_contexts: List[str] = []

            def start_web_search(planned_query: str) -> None:
                web_prefetch.append(WEB_PREFETCH_POOL.submit(
                    ask_from_llm, "", llm_query, "", planned_query, force_search=True
                ))

            # Filled in by answer_stream(), which does the retrieval inside the
            # response so each stage's status reaches the user as it starts.
            news_content = ""
            search_required = False
            formatted_query = ""
            kb_coverage: Optional[str] = None
            print("Jailbreak:", jailbreak)

            # Earlier turns as they were, then one user turn with this
            # question's retrieved context, the question and the instruction.
            # The system prompt goes in as Gemini's system instruction. It used
            # to come after the history, with the context as a "model" turn, so
            # Gemini trusted an old archive article over its own earlier answers
            # (it said Messi never played a farewell match right after telling
            # the user about the 3-0 win over Benin).
            history_turns = [
                {"role": _to_gemini_role(msg["role"]), "parts": [{"text": msg["content"]}]}
                for msg in extra + [entry for entry in conversation_history if entry["role"] != "system"]
            ]

            def answer_question() -> str:
                """The question for the answer prompts. A follow-up carries what
                the classifier resolved it to: given only "their finishing position
                prediction?" and an article on England, an answer about Roma
                came out about England."""
                if conversation_history and formatted_query:
                    return f"{prompt['content']}\n(In this conversation that means: {formatted_query})"
                return prompt["content"]

            def with_football_data(material: Optional[str]) -> str:
                """The context, after the database's facts on what the question names."""
                facts = football_data.context_for(f"{prompt['content']} {formatted_query or ''}")
                return "\n\n".join(part for part in (facts, material) if part)

            def contents_with(instruction: str, context: Optional[str] = None) -> List[Dict[str, Any]]:
                material = with_football_data(news_content if context is None else context)
                used_contexts.append(material)
                answer_turn = build_answer_turn(material, answer_question(), instruction)
                return history_turns + [{"role": "user", "parts": [{"text": answer_turn}]}]

            def stream_gemini_flash_fast(
                convo: List[Dict[str, Any]],
                err_msgs: List[str],
            ) -> Iterable[bytes]:
                """
                Stream a complete, single-phase answer: the fast path's, and the
                combined answer when Phase 1 found nothing relevant.

                Args:
                    convo: The conversation history formatted for Gemini.
                    err_msgs: A list of fallback error messages to use on failure.

                Yields:
                    UTF-8 encoded bytes chunks of the response.
                """
                try:
                    model_name = "gemini-2.5-flash"

                    def _call_complete_context(client):
                        return client.models.generate_content_stream(
                            model=model_name,
                            contents=convo,
                            config=genai_types.GenerateContentConfig(
                                system_instruction=system_message,
                                thinking_config=_thinking_config(ANSWER_THINKING_BUDGET),
                                # Thinking tokens count against this cap: at 2100, long tactical
                                # answers were cut off before their Ask next block.
                                max_output_tokens=6000,
                                temperature=0.5,
                                top_p=0.5,
                                safety_settings=GEMINI_SAFETY_SETTINGS,
                            ),
                        )

                    response = call_gemini_with_key_retry(
                        model_name,
                        _call_complete_context,
                        streaming=True,
                    )

                    for chunk in response:
                        try:
                            finish_reason = _extract_finish_reason(chunk)
                            if finish_reason:
                                print(f"Gemini finish reason (complete-context): {finish_reason}")
                            chunk_text = _extract_text(chunk)
                            if chunk_text:
                                yield chunk_text.encode("utf-8")
                        except GeneratorExit:
                            break
                        except Exception as exc:
                            print(f"Error streaming chunk: {exc}")
                            yield random.choice(err_msgs).encode("utf-8")
                            break

                except Exception as e:
                    print(f"Error during Gemini API call: {e}")
                    yield random.choice(err_msgs).encode("utf-8")

            # === Two-phase path: first-draft context, then refine with latest context ===

            def stream_gemini_flash(
                err_msgs: List[str],
                user_prompt: Dict[str, str],
            ) -> Iterable[bytes]:
                """
                Generate the refinement response with a fresh prompt only.

                Args:
                    err_msgs: Error messages fallback.
                    user_prompt: The user prompt dict with 'content'.

                Yields:
                    UTF-8 encoded bytes chunks of the response.
                """
                print("Streaming response with latest context...")
                try:
                    def _call_phase2(client):
                        return client.models.generate_content_stream(
                            model="gemini-2.5-flash",
                            contents=user_prompt["content"],
                            config=genai_types.GenerateContentConfig(
                                system_instruction=system_message,
                                thinking_config=_thinking_config(ANSWER_THINKING_BUDGET),
                                # Thinking counts against this too: at 2400 an update was cut off
                                # mid-sentence, before its Ask next block.
                                max_output_tokens=6000,
                                temperature=0.4,
                                top_p=0.6,
                                safety_settings=GEMINI_SAFETY_SETTINGS,
                            ),
                        )

                    response = call_gemini_with_key_retry(
                        "gemini-2.5-flash",
                        _call_phase2,
                        streaming=True,
                    )
                    def texts() -> Iterable[str]:
                        for chunk in response:
                            finish_reason = _extract_finish_reason(chunk)
                            if finish_reason:
                                print(f"Gemini finish reason (phase-2): {finish_reason}")
                            chunk_text = _extract_text(chunk)
                            if chunk_text:
                                yield chunk_text

                    for piece in _without_sentinel(texts(), NO_NEW_UPDATES):
                        yield piece.encode("utf-8")

                except Exception as exc:
                    print(f"Error during Gemini API call: {exc}")
                    yield random.choice(err_msgs).encode("utf-8")

            def refine_with_latest_context(
                original_question: str,
                initial_response: str,
                news_content_latest: str,
                err_msgs: List[str],
            ) -> Iterable[bytes]:

                """
                Continue the initial answer with updated news context, streaming the result.

                Args:
                    original_question: The original user question text.
                    initial_response: The text produced in phase 1.
                    news_content_latest: Updated/slow-fetched news context.
                    err_msgs: Error messages fallback.

                Returns:
                    An iterator yielding UTF-8 encoded bytes chunks.
                """

                news_content_latest = with_football_data(news_content_latest)
                used_contexts.append(news_content_latest)
                refined_prompt = build_refinement_prompt(
                    original_question,
                    initial_response,
                    news_content_latest,
                )
                return stream_gemini_flash(
                    err_msgs, user_prompt={"content": refined_prompt}
                )

            def stream_fast_gemini() -> Iterable[bytes]:

                """
                Phase 1 and Phase 2 streaming generator.

                Phase 1: Produce a brief, stylish answer based on "first draft" context.
                Phase 2: Optionally refine the answer if updated context arrives.
                """
                nonlocal kb_coverage

                # Phase 1 (fast)
                def _call_phase1(client):
                    return client.models.generate_content_stream(
                        model="gemini-2.5-flash",
                        contents=contents_with(build_first_draft_instruction()),
                        config=genai_types.GenerateContentConfig(
                            system_instruction=system_message,
                            thinking_config=_thinking_config(PHASE1_THINKING_BUDGET),
                            max_output_tokens=5000,
                            temperature=0.3,
                            top_p=0.5,
                            safety_settings=GEMINI_SAFETY_SETTINGS,
                        ),
                    )

                def phase1_texts() -> Iterable[str]:
                    for chunk in response:
                        try:
                            finish_reason = _extract_finish_reason(chunk)
                            if finish_reason:
                                print(f"Gemini finish reason (phase-1): {finish_reason}")
                            chunk_text = _extract_text(chunk)
                            if chunk_text:
                                yield chunk_text
                        except Exception as exc:
                            print(chunk)
                            print(exc)

                def fetch_latest_context():
                    """The web search's context (None on failure), via `yield from`:
                    yields the web status while the search is still running."""
                    try:
                        print("Fetching latest news content...")
                        if not stage_statuses or not (web_prefetch and web_prefetch[0].done()):
                            yield _status(STATUS_WEB)
                        if web_prefetch:
                            latest, _, _ = web_prefetch[0].result()
                        else:
                            latest, _, _ = ask_from_llm(
                                "", llm_query, news_content, formatted_query, force_search=True
                            )
                        print("Fetched latest news content")
                        return latest
                    except Exception as exc:
                        print(exc)
                        print("Error fetching latest news content, using default context.")
                        return None

                def combined_answer() -> Iterable[bytes]:
                    # No draft: one answer from Run of Play's articles and the web together.
                    latest = yield from fetch_latest_context()
                    combined = order_context(news_content, latest)
                    yield from stage_status(STATUS_GENERATING)
                    yield from stream_gemini_flash_fast(
                        contents_with(build_complete_context_instruction(), combined),
                        error_messages,
                    )

                if kb_coverage == "none":
                    print("Run of Play context doesn't answer the question - skipping the draft, searching, then one combined answer.")
                    yield from combined_answer()
                    return

                yield from stage_status(STATUS_GENERATING)
                try:
                    response = call_gemini_with_key_retry(
                        "gemini-2.5-flash", _call_phase1, streaming=True
                    )
                except Exception as exc:
                    print(f"Phase 1 failed on every available key: {exc}")
                    yield random.choice(error_messages).encode("utf-8")
                    return

                initial_response = ""
                nothing_relevant: List[bool] = []
                print("=== Phase 1 starting ===")
                for piece in _without_sentinel(
                    phase1_texts(), NO_RELEVANT_CONTEXT, found=nothing_relevant, keep_rest=False
                ):
                    initial_response += piece
                    yield piece.encode("utf-8")

                if nothing_relevant or not initial_response.strip():
                    print("Phase 1 flagged nothing relevant - searching, then one combined answer.")
                    kb_coverage = "none"
                    yield from combined_answer()
                    return

                yield "\n\n".encode("utf-8")

                print("Initial response received from Gemini API.")
                print("Waiting for latest context (Phase 2)...")

                # Phase 2: Attempt to fetch updated context via ask_from_llm
                news_content_latest = yield from fetch_latest_context()
                yield from stage_status(STATUS_GENERATING)

                # If updated context exists, continue streaming a refined answer
                if news_content_latest:
                    print("Streaming Phase 2: Refining with updated news...")
                    followup_response = refine_with_latest_context(
                        answer_question(),
                        initial_response,
                        news_content_latest,
                        error_messages,
                    )
                    yielded_any = False
                    for chunk in followup_response:
                        yielded_any = True
                        try:
                            yield chunk
                        except Exception as exc:
                            print(chunk)
                    # NO_NEW_UPDATES now comes with an Ask next block; if even
                    # that's missing, the user still gets follow-up suggestions.
                    if not yielded_any:
                        print("Phase 2 yielded nothing - adding follow-up suggestions to the draft.")
                        yield from follow_ups_only(prompt["content"], initial_response)
                else:
                    print("Phase 2 found no new context - adding follow-up suggestions to the draft.")
                    yield from follow_ups_only(prompt["content"], initial_response)

            def follow_ups_only(question: str, answer: str) -> Iterable[bytes]:
                """An Ask next block (flash-lite) for a draft that Phase 2
                couldn't add to - so the wait still ends somewhere useful."""
                try:
                    response = call_gemini_with_key_retry(
                        "gemini-2.5-flash-lite",
                        lambda client: client.models.generate_content(
                            model="gemini-2.5-flash-lite",
                            contents=build_follow_ups_only_prompt(question, answer),
                        ),
                    )
                    block = (response.text or "").strip()
                except Exception as exc:
                    print(f"Follow-up suggestions failed: {exc}")
                    return
                if block.lower().startswith("ask next"):
                    yield ("\n\n" + block).encode("utf-8")

            def answer_stream() -> Iterable[bytes]:
                """Retrieval, then the answer path it picks, with a status line
                for each stage."""
                nonlocal news_content, search_required, formatted_query, kb_coverage
                try:
                    yield from stage_status(STATUS_RUN_OF_PLAY)
                    news_content, search_required, formatted_query = ask_from_llm(
                        "", llm_query, on_search_planned=start_web_search
                    )
                    print("news_content", news_content)
                    print("search_required", search_required)

                    # When fresh news matters, a quick check of how much of the answer
                    # the Run of Play articles hold picks the path: "full" -> answer from
                    # them alone; "partial" -> draft from them, then the web update;
                    # "none" -> no draft, one answer from the web search plus them.
                    kb_coverage = judge_kb_coverage(prompt["content"], formatted_query, news_content) if search_required else None

                    # === Fast path: no search needed, or the articles cover it (complete context) ===
                    if not search_required or kb_coverage == "full":
                        print("Streaming response from Gemini AI (complete context)...")
                        yield from stage_status(STATUS_GENERATING)
                        yield from stream_gemini_flash_fast(
                            contents_with(build_complete_context_instruction()),
                            error_messages,
                        )
                        return

                    yield from stream_fast_gemini()
                except Exception as exc:
                    import traceback

                    print(traceback.format_exc())
                    yield _friendly_llm_error_message(exc, error_messages).encode("utf-8")

            def generate() -> Iterable[bytes]:
                """Wraps answer_stream() to save the transcript, without its
                status lines, once it ends, and adds the sources line."""
                response_parts: List[str] = []
                try:
                    for chunk in answer_stream():
                        response_parts.append(chunk.decode("utf-8", errors="replace"))
                        yield chunk
                    if want_sources:
                        # Coverage "none" can mean nothing recent enough, so a recent
                        # report the answer used still counts; old articles don't.
                        sources = run_of_play_sources(
                            news_content, prompt["content"], "".join(response_parts),
                            include_archive=kb_coverage != "none",
                        )
                        print(f"[OffTheBar sources] {[source['title'] for source in sources]}")
                        if sources:
                            yield f"\n\n{SOURCES_MARKER}{json.dumps(sources, ensure_ascii=False)}\n".encode("utf-8")
                finally:
                    transcript = re.sub(r"\n{3,}", "\n\n", HALFTIME_LINE.sub("", "".join(response_parts))).strip()
                    print("[OffTheBar context] " + json.dumps({
                        "conversation_id": conversation_id,
                        "question": prompt["content"],
                        "contexts": list(dict.fromkeys(part for part in used_contexts if part)),
                        "answer": transcript,
                    }, ensure_ascii=False))
                    save_chat_turn(conversation_id, prompt["content"], transcript)

            return Response(
                stream_with_context(generate()),
                content_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
                direct_passthrough=True,
            )

        except Exception as exc:
            # Top-level error handling to keep API stable
            import traceback

            print(exc)
            print(getattr(exc, "__traceback__", None))
            print(traceback.format_exc())
            friendly_message = _friendly_llm_error_message(exc, error_messages)
            return Response(
                friendly_message,
                status=200,
                mimetype="text/plain",
            )


def conversation_endpoint(request_json: Dict[str, Any], config: Dict[str, Any]) -> Response:
    """
    Public entrypoint suitable for Flask routes.

    Wraps BackendApi to route a single request payload through the _conversation handler.

    Args:
        request_json: The JSON body of the incoming request.
        config: Config dictionary passed through to BackendApi.

    Returns:
        A Flask Response streaming the model output.
    """
    # Instantiate without a Flask app if you wire routes elsewhere.
    api = Backend_Api(app=None, config=config)
    return api._conversation(request_json)
