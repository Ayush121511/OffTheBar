"""
Football analysis backend API with streaming Gemini responses.

"""

from __future__ import annotations

import os
import random
from datetime import datetime
from typing import Any, Dict, Generator, Iterable, List, Optional, Tuple
from flask import Response, request, stream_with_context
from requests import get
import google.generativeai as genai

from gemini_utils import build_gemini_model, get_gemini_api_keys
from prompt_templates import (
    build_chat_system_prompt,
    build_complete_context_instruction,
    build_first_draft_instruction,
    build_refinement_prompt,
)
from ragwithsearch import ask_from_llm, error_messages

halftime_phrases = [
    "HALFTIME!!! fetching latest updates",
    "HALFTIME!!! loading fresh news",
    "HALFTIME!!! pulling in live details",
    "HALFTIME!!! grabbing new reports",
    "HALFTIME!!! bringing fresh updates",
    "HALFTIME!!! checking for latest info",
    "HALFTIME!!! scanning for fresh news",
    "HALFTIME!!! gathering live updates"
]


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ValueError(f"{name} environment variable is not set.")
    return value


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
            # Toggle for optional internet search (currently disabled)
            internet_access = False

            # Backward-compatible: pull JSON from Flask request when not provided
            if payload is None:
                payload = request.json

            jailbreak = payload["jailbreak"]
            conversation_history = payload["meta"]["content"]["conversation"]
            prompt = payload["meta"]["content"]["parts"][0]

            current_date = datetime.now().strftime("%Y-%m-%d")

            # System message governing tone, style, and operational rules
            system_message = build_chat_system_prompt(datetime.now())

            extra: List[Dict[str, str]] = []
            if internet_access:
                # Optional duckduckgo search (disabled by default).
                # If enabled, collects brief snippets for quick citations.
                search = get(
                    "https://ddg-api.herokuapp.com/search",
                    params={"query": prompt["content"], "limit": 3},
                    timeout=15,
                )
                blob = ""
                for index, result in enumerate(search.json()):
                    blob += (
                        f'[{index}] "{result["snippet"]}"\n'
                        f'URL:{result["link"]}\n\n'
                    )
                blob += (
                    "\n\nInstructions: Using the provided web search results, write a "
                    "comprehensive reply to the next user query. Make sure to cite results "
                    "using [[number](URL)] notation after the reference. If the provided "
                    "search results refer to multiple subjects with the same name, write "
                    "separate answers for each subject. Ignore your previous response if any."
                )
                extra = [{"role": "user", "content": blob}]

            # Ask your LLM helper if search is required and obtain news context.
            try:
                news_content, search_required, formatted_query = ask_from_llm(
                    "",
                    "Context given:"
                    + "\n".join(
                        [
                            entry_now["content"]
                            for entry_now in conversation_history
                            if entry_now["role"] != "user"
                        ]
                    )
                    + "Previous Query:"
                    + "\n".join(
                        [
                            entry_now["content"]
                            for entry_now in conversation_history
                            if entry_now["role"] == "user"
                        ]
                    )
                    + "\n"
                    + "Main query to be answered:"
                    + prompt["content"],
                )
            except TypeError as exc:
                return Response(str(exc), status=500, mimetype="text/plain")

            print("news_content", news_content)
            print("search_required", search_required)
            print("Jailbreak:", jailbreak)

            # Build the combined conversation for Gemini
            first_user_prompt = f"{system_message}\n\n{prompt['content']}"

            built_conversation: List[Dict[str, str]] = (
                extra
                + [entry_now for entry_now in conversation_history if entry_now["role"] != "system"]
                + [{"role": "user", "content": first_user_prompt}]
                + [{"role": "model", "content": f"\n{news_content}"}]
            )

            gemini_conversation = [
                {"role": _to_gemini_role(msg["role"]), "parts": [msg["content"]]}
                for msg in built_conversation
            ]

            # === Fast path: if no search is required, stream immediately (complete context) ===
            if not search_required:

                def stream_gemini_flash_fast(
                    convo: List[Dict[str, Any]],
                    err_msgs: List[str],
                ) -> Iterable[bytes]:
                    """
                    Stream a single-phase response from Gemini using the complete context.

                    Args:
                        convo: The conversation history formatted for Gemini.
                        err_msgs: A list of fallback error messages to use on failure.

                    Yields:
                        UTF-8 encoded bytes chunks of the response.
                    """
                    try:
                        # Add a final instruction indicating we have complete context.
                        convo.append(
                            {
                                "role": "user",
                                "parts": [
                                    build_complete_context_instruction()
                                ],
                            }
                        )

                        model_name = "gemini-2.5-flash"
                        safety_settings = [
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

                        model = build_gemini_model(model_name, safety_settings=safety_settings)
                        chat = model.start_chat(history=convo)

                        response = chat.send_message(
                            prompt["content"],
                            stream=True,
                            generation_config=genai.GenerationConfig(
                                max_output_tokens=2100,
                                temperature=0.5,
                                top_p=0.5,
                            ),
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

                def generate_fast() -> Iterable[bytes]:
                    """Flask generator: stream the fast single-phase answer."""
                    print("Streaming response from Gemini AI (complete context)...")
                    yield from stream_gemini_flash_fast(
                        list(gemini_conversation),
                        error_messages,
                    )

                return Response(
                    stream_with_context(generate_fast()),
                    content_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
                    direct_passthrough=True,
                )

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
                    safety_settings = [
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

                    gemini_model = build_gemini_model(
                        "gemini-2.5-flash", safety_settings=safety_settings
                    )
                    response = gemini_model.generate_content(
                        user_prompt["content"],
                        stream=True,
                        generation_config=genai.GenerationConfig(
                            max_output_tokens=2400,
                            temperature=0.4,
                            top_p=0.6,
                        ),
                    )
                    response_text_parts: List[str] = []
                    for chunk in response:
                        finish_reason = _extract_finish_reason(chunk)
                        if finish_reason:
                            print(f"Gemini finish reason (phase-2): {finish_reason}")
                        chunk_text = _extract_text(chunk)
                        if not chunk_text:
                            continue
                        response_text_parts.append(chunk_text)
                        yield chunk_text.encode("utf-8")

                    response_text = "".join(response_text_parts).strip()
                    if not response_text:
                        print("Phase 2 returned no text.")
                        return
                    if response_text == "NO_NEW_UPDATES":
                        return

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

                # Phase 1 (fast)
                model = build_gemini_model("gemini-2.5-flash")
                chat = model.start_chat(history=gemini_conversation)

                response = chat.send_message(
                    prompt["content"]
                    + "\n"
                    + build_first_draft_instruction(),
                    stream=True,
                    generation_config=genai.GenerationConfig(
                        max_output_tokens=960,
                        temperature=0.3,
                        top_p=0.5,
                    ),
                )

                initial_response = ""
                print("=== Phase 1 starting ===")

                for chunk in response:
                    try:
                        finish_reason = _extract_finish_reason(chunk)
                        if finish_reason:
                            print(f"Gemini finish reason (phase-1): {finish_reason}")
                        chunk_text = _extract_text(chunk)
                        if chunk_text:
                            initial_response += chunk_text
                            yield chunk_text.encode("utf-8")
                    except GeneratorExit:
                        # yield "\n\n ##Sharing further insights...\n\n".encode("utf-8")
                        break
                    except Exception as exc:
                        print(chunk)
                        print(exc)
                        continue

                yield "\n\n".encode("utf-8")
                yield ("## " + random.choice(halftime_phrases) + "\n\n").encode("utf-8")


                print("Initial response received from Gemini API.")
                print("Waiting for latest context (Phase 2)...")

                # Phase 2: Attempt to fetch updated context via ask_from_llm
                news_content_latest: Optional[str] = None
                try:
                    print("Fetching latest news content...")
                    latest_result = ask_from_llm(
                        "",
                        "Context given:"
                        + "\n".join(
                            [
                                entry_now["content"]
                                for entry_now in conversation_history
                                if entry_now["role"] != "user"
                            ]
                        )
                        + "Previous Query:"
                        + "\n".join(
                            [
                                entry_now["content"]
                                for entry_now in conversation_history
                                if entry_now["role"] == "user"
                            ]
                        )
                        + "\n"
                        + "Main query to be answered:"
                        + prompt["content"],
                        news_content,
                        formatted_query,
                        force_search=True,
                    )
                    if isinstance(latest_result, tuple):
                        news_content_latest = latest_result[0]
                    else:
                        news_content_latest = latest_result
                    print("Fetched latest news content")
                except Exception as exc:
                    print(exc)
                    print("Error fetching latest news content, using default context.")
                    news_content_latest = None

                # If updated context exists, continue streaming a refined answer
                if news_content_latest:
                    print("Streaming Phase 2: Refining with updated news...")
                    followup_response = refine_with_latest_context(
                        prompt["content"],
                        initial_response,
                        news_content_latest,
                        error_messages,
                    )
                    for chunk in followup_response:
                        try:
                            yield chunk
                        except Exception as exc:
                            print(chunk)
                else:
                    yield "\n\n_No further live updates were available, so this answer used the first-pass context only._".encode("utf-8")

            return Response(
                stream_with_context(stream_fast_gemini()),
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
