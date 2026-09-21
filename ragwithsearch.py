error_messages = [
    "🥅 Server missed the penalty! Try again later.",
    "🚦 VAR checking... Please try again later!",
    "🏃‍♂️ Server took a dive! Retry later.",
    "⚡ Counterattack failed! Try again in a bit.",
    "🔥 Server pulled a hamstring! Come back soon.",
    "🥶 Cold feet! Server needs a warm-up. Try again later.",
    "🎩 Server tried a fancy flick… and lost possession. Retry later!",
    "⏳ Injury time added… Try again in a moment.",
    "🛑 Server parked the bus! Refresh and try again later.",
    "📢 Server’s manager is fuming! Reconnecting… Try again soon.",
    "🎭 Server pulled a Neymar! Rolling back online soon. Retry later.",
    "⚔️ The server got tackled hard! Give it a sec and try again.",
    "🔄 Tactical substitution in progress… Try again shortly.",
    "👀 Server’s looking for an open pass… Stay tuned and retry later.",
    "💤 Server caught ball-watching! Wake it up with a retry soon."
]

import requests
import urllib.robotparser
from urllib.parse import urlparse
from config_env import require_env
from kb_google import search_pinecone
from rank_bm25 import BM25Okapi
from nltk.tokenize import word_tokenize
from nltk.tokenize import sent_tokenize


def is_scraping_allowed(url, user_agent='*'):
    # Parse the domain from the URL
    parsed_url = urlparse(url)
    base_url = f"{parsed_url.scheme}://{parsed_url.netloc}"
    robots_url = f"{base_url}/robots.txt"

    # Initialize and read the robots.txt file
    rp = urllib.robotparser.RobotFileParser()
    rp.set_url(robots_url)
    try:
        rp.read()
    except Exception as e:
        return False, str(e)

    # Check if the URL is allowed
    allowed = rp.can_fetch(user_agent, url)
    return allowed, f"Checked {robots_url}"

def chunk_article(text, max_chunk_length=250):
    sentences = sent_tokenize(text)
    chunks = []
    chunk = ""
    for sentence in sentences:
        if len(chunk) + len(sentence) <= max_chunk_length:
            chunk += " " + sentence
        else:
            chunks.append(chunk.strip())
            chunk = sentence
    if chunk:
        chunks.append(chunk.strip())
    return chunks

def rank_chunks_bm25(chunks, query, top_k=5):
    # Tokenize chunks
    tokenized_chunks = [word_tokenize(chunk.lower()) for chunk in chunks]

    # Initialize BM25
    bm25 = BM25Okapi(tokenized_chunks)

    # Tokenize query
    tokenized_query = word_tokenize(query.lower())

    # Get scores
    scores = bm25.get_scores(tokenized_query)

    # Get top k
    top_indices = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:top_k]
    return [chunks[i] for i in top_indices]

from datetime import date
from datetime import datetime

from crawl4AI import crawl_page

def _log_query(label, value):
    print(f"[OffTheBar query] {label}: {value!r}")


def build_news_context(query, formatted_query, fallback_context=None, search_required=None):
    """
    Resolve the context text to feed the LLM: a fresh Pinecone match, a stale
    Pinecone match kept as fallback, or freshly scraped web search results.

    Returns (context_text, search_required, formatted_query).
    """
    print("Formatted query:", formatted_query)
    retrieval_query = formatted_query or query
    relevance_query = formatted_query or query
    _log_query("retrieval_input", retrieval_query)
    _log_query("fallback_context_supplied", fallback_context is not None)
    _log_query("web_search_requested", bool(search_required))


    pinecone_contexts = []


    if fallback_context is None:
        # Query Pinecone index for top 3 matches
        try:
            _log_query("pinecone_query", retrieval_query)
            matches = search_pinecone(retrieval_query, top_k=4)
        except Exception as exc:
            print(f"Pinecone search failed: {exc}")
            _log_query("pinecone_failed", str(exc))
            matches = []
        if matches:
            top_match = matches[0]
            score = top_match['score']
            metadata = top_match['metadata']
            created_date_str = metadata.get("created", "")

            try:
                created_date = datetime.strptime(created_date_str, "%Y-%m-%dT%H:%M:%S.%fZ")
            except Exception as e:
                print(f"Date parse error: {e}")
                created_date = datetime.min

            days_diff = (datetime.utcnow() - created_date).days

            if score >= 0.7 and days_diff <=30:
                print(f"Using Pinecone context only. Score: {score:.2f}, Days ago: {days_diff}")
                # for match in reranked_matches:
                for match in matches:
                    md = match['metadata']
                    pinecone_contexts.append(
                        f"[RUN OF PLAY CONTEXT]\n{md.get('created', 'Unknown Date')}\n\n{md.get('chunk', 'No content')}\n"
                    )
                return "\n\n".join(pinecone_contexts), False, formatted_query
            else:
                print(f"Pinecone context is not fresh enough. Score: {score:.2f}, Days ago: {days_diff}")
                for match in matches:
                        md = match['metadata']
                        match_created_str = md.get('created', '')
                        try:
                            match_created = datetime.strptime(match_created_str, "%Y-%m-%dT%H:%M:%S.%fZ")
                            match_days_old = (datetime.utcnow() - match_created).days
                            staleness_note = f"OUTDATED - published {match_days_old} days ago, not recent news"
                        except Exception:
                            staleness_note = "OUTDATED - publish date unknown, do not assume this is recent"
                        pinecone_contexts.append(
                            f"[RUN OF PLAY CONTEXT - {staleness_note}]\n{match_created_str or 'Unknown Date'}\n\n{md.get('chunk', 'No content')}\n"
                        )

                fallback_context = "\n\n".join(pinecone_contexts)
                return fallback_context, (True and search_required), formatted_query
    else:
        _log_query("pinecone_skipped", "fallback context was supplied")

    if not search_required:
        _log_query("web_search_skipped", "search_required is false")
        return fallback_context or "", False, formatted_query

    url = f"{require_env('SEARXNG_URL').rstrip('/')}/search"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
    }
    results = {"results": []}
    print("Searching for news context...")
    print("Formatted query:", formatted_query)
    _log_query("searx_query", formatted_query)
    try:
        params = {
            "q": formatted_query,
            "format": "json",
            "language": "en",
            "safesearch": 1,
            # Our self-hosted SearXNG runs on Cloud Run; its outbound IPs get
            # instant CAPTCHAs/rate-limits from duckduckgo/google/brave/startpage
            # (a universal problem for cloud-hosted SearXNG, not fixable via
            # config). Bing is the one major engine that doesn't block it.
            "engines": "bing,bing news",
        }

        response = requests.get(url, headers=headers, params=params, timeout=10)
        response.raise_for_status()

        results = response.json()
    except Exception as e:
        print(f"Error: {e}")
    print("Search results:", len(results.get("results", [])))
    page_contents = {}
    for r_now in results.get("results", []):
        # page_contents[r_now[url]] = r_now.get("snippet", []) + scrape_page(url, keywords)
        page_contents[r_now["url"]] = r_now.get("title", " ") + r_now.get("content", " ") +  r_now.get("snippet", " ")
    print("Page contents:", len(page_contents))
    # Unlike the Pinecone branch above, SearXNG/Bing results carry no
    # verified publish date we compute against - without saying so
    # explicitly, the model has no factual basis for "current" claims and
    # will confabulate confidence (observed live: presenting gameweek-old
    # FPL data as freshly "confirmed current"). State today's real date and
    # make the model responsible for not asserting recency it can't verify.
    prompt_begin = (
        f"Today's real date is {date.today().isoformat()}.\n"
        f"These are web search results for the following query - {query}.\n"
        "None of these results have a verified publish date unless the "
        "source explicitly states one below. Do not describe this "
        "information as \"confirmed current\", \"fresh\", or \"up to date\" "
        "unless a date in the text itself supports that conclusion. If you "
        "cannot tell how recent something is, say so plainly instead of "
        "asserting confidence you don't have.\n\n"
    )

    # Bing sometimes ranks tangentially-matched pages highly (e.g. a query
    # containing "Lionel" surfacing "first name vs last name" grammar pages,
    # or Lionel-brand model-train stores). A single shared token like "lionel"
    # isn't discriminating enough - Bing itself gets confused by it. Require
    # overlap on at least 2 distinct query terms (or all of them, if the
    # query is short) before trusting a result; fall back to the unfiltered
    # list if that empties it out.
    query_tokens = {t for t in relevance_query.lower().split() if len(t) > 2}
    min_overlap = min(2, len(query_tokens)) or 1

    def _is_relevant(ctx):
        haystack = f"{ctx.get('title', '')} {ctx.get('content', '')}".lower()
        overlap = sum(1 for token in query_tokens if token in haystack)
        return overlap >= min_overlap

    candidate_results = [r for r in results.get("results", []) if _is_relevant(r)]
    if not candidate_results:
        print("Relevance filter matched nothing; falling back to unfiltered results.")
        candidate_results = results.get("results", [])

    prompt_context = []
    for ctx in candidate_results:
        if(len(prompt_context) >=2):
            break
        scrape_premission = is_scraping_allowed(ctx["url"])
        if scrape_premission[0] == False:
            print(f"Skipping {ctx['url']} - scraping not allowed by robots.txt")
            print(scrape_premission[1])
            continue

        page_text = crawl_page(ctx['url'])
        if not page_text:
            print(f"Skipping {ctx['url']} - no content found")
            continue

        page_metadata = page_text[:200]  # Get first 1000 characters as metadata
        print("Chunking started for", ctx['url'])
        page_chunks = chunk_article(page_text)
        print(f"Page chunks for {ctx['url']}: {len(page_chunks)}")
        if not page_chunks:
            # crawl_page can return text that survives the `if not page_text`
            # check (e.g. whitespace, or a page blocked by anti-bot
            # protection that still yields a near-empty string) but has no
            # tokenizable sentences. BM25Okapi divides by corpus size, so an
            # empty chunk list crashes the whole request - skip instead.
            print(f"Skipping {ctx['url']} - no usable chunks after tokenization")
            continue
        top_chunks = rank_chunks_bm25(page_chunks, relevance_query, top_k=5)
        print(f"Top chunks for {ctx['url']}: {len(top_chunks)}")
        # print(top_chunks[0])
        top_chunks_str = '\n\n'.join(top_chunks)
        published = ctx.get("publishedDate") or "unknown - do not assume this is recent"
        prompt_context.append(f"NEWS - {ctx['title']} :  {ctx['url']}\nPublished: {published}\nPage content: {page_contents.get(ctx['url'], '')} \n\n{page_metadata} {top_chunks_str}\n")

    print(len(prompt_context))
    prompt_context = "\n\n".join(prompt_context)

    formatted_prompt_short = prompt_begin + "Recent news:\n\n" + prompt_context

    return formatted_prompt_short, search_required, formatted_query



import json
import re

from gemini_utils import call_gemini_with_key_retry
from prompt_templates import build_search_classifier_prompt


def _heuristic_query_classifier(user_question: str) -> dict:
    question = user_question.strip()
    lowered = question.lower()

    gaming_markers = [
        "fifa", "ea fc", "efootball", "pes ", "ultimate team", "career mode",
        "fut ", "my formation", "my squad", "my team", "player instructions",
        "custom tactics", "wingback", "wing-back",
    ]
    if any(marker in lowered for marker in gaming_markers):
        # Checked before fresh_markers below: a video-game question about
        # "my squad" or "transfers" is about game mechanics, not real-world
        # news, even though it can share vocabulary with a genuine freshness
        # question - the football-club transfer window isn't what's meant.
        return {
            "search_required": False,
            "reason": "Heuristic fallback detected a video-game tactics/formation question.",
            "query": "",
        }

    fresh_markers = [
        "latest", "right now", "today", "currently", "recent", "recently",
        "this season", "last match", "next match", "fixture", "fixtures",
        "injury", "injuries", "transfer", "transfers", "rumour", "rumor",
        "news", "update", "updates", "table", "standings", "form",
        "score", "result", "results", "live", "quote", "quotes",
        "contract", "renewal", "world cup", "2026", "squad", "call up",
        "called up", "retire", "retirement", "available", "availability",
        "will ",
    ]
    evergreen_markers = [
        "favourite", "favorite", "best ever", "greatest ever", "of all time",
        "who is better", "who was better", "legacy", "history of",
        "historical", "all-time", "all time", "why was", "why is",
        "what is the offside rule", "size of a football pitch",
    ]

    if any(marker in lowered for marker in fresh_markers):
        normalized = re.sub(
            r"previous user questions,? for resolving pronouns and follow-ups:|current question:",
            " ",
            question.lower(),
        )
        normalized = re.sub(r"[^a-z0-9\s-]", " ", normalized)
        normalized = " ".join(normalized.split())
        return {
            "search_required": True,
            "reason": "Heuristic fallback detected a freshness-sensitive football question.",
            "query": normalized[:140],
        }

    if any(marker in lowered for marker in evergreen_markers):
        return {
            "search_required": False,
            "reason": "Heuristic fallback detected an evergreen or opinion-based football question.",
            "query": "",
        }

    return {
        "search_required": False,
        "reason": "Heuristic fallback defaulted to no-search for a non-obviously fresh question.",
        "query": "",
    }


def _extract_primary_question(raw_query: str) -> str:
    question = (raw_query or "").strip()
    marker = "Main query to be answered:"
    if marker in question:
        question = question.rsplit(marker, 1)[-1].strip()

    # Drop obvious backend error blobs that may have been serialized into chat history.
    if "Traceback (most recent call last)" in question:
        question = question.split("Traceback (most recent call last)", 1)[0].strip()
    if "{'_action':" in question:
        question = question.split("{'_action':", 1)[0].strip()

    return question


def _strip_error_noise(text: str) -> str:
    text = (text or "").strip()
    for marker in (
        "Traceback (most recent call last)",
        "{'_action':",
        "[stacktrace in console]",
    ):
        if marker in text:
            text = text.split(marker, 1)[0].strip()
    return text


def _extract_previous_user_queries(raw_query: str) -> list[str]:
    text = raw_query or ""
    if "Previous Query:" not in text:
        return []

    previous_block = text.split("Previous Query:", 1)[1]
    if "Main query to be answered:" in previous_block:
        previous_block = previous_block.split("Main query to be answered:", 1)[0]

    lines = [
        _strip_error_noise(line)
        for line in previous_block.splitlines()
        if _strip_error_noise(line)
    ]
    return lines[-4:]


def _build_contextual_question(raw_query: str) -> str:
    primary_question = _extract_primary_question(raw_query)
    previous_questions = _extract_previous_user_queries(raw_query)
    if not previous_questions:
        return primary_question

    previous_text = "\n".join(f"- {question}" for question in previous_questions)
    return (
        "Previous user questions, for resolving pronouns and follow-ups:\n"
        f"{previous_text}\n\n"
        f"Current question: {primary_question}"
    )


def _best_effort_json(text: str) -> dict:
    """
    Robust JSON extraction: safely parse the first {...} block if the model
    wraps output in prose. Falls back to a safe default.
    """
    text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except Exception:
            pass

    return {
        "search_required": True,
        "reason": "Could not parse JSON; defaulting to search.",
        "query": text
    }


def classify_and_build_query(user_question: str) -> dict:
    """
    ONE call → returns:
      {
        "search_required": true/false,
        "reason": "...",
        "query": "keyword-rich one-line football query ('' if not needed)"
      }
    """
    user_question = _strip_error_noise(user_question)
    today = date.today()
    heuristic_result = _heuristic_query_classifier(user_question)

    if (
        not heuristic_result["search_required"]
        and "defaulted to no-search" not in heuristic_result["reason"]
    ):
        return heuristic_result

    prompt = build_search_classifier_prompt(user_question, today)
    try:
        response = call_gemini_with_key_retry(
            "gemini-2.5-flash-lite",
            lambda client: client.models.generate_content(
                model="gemini-2.5-flash-lite", contents=prompt
            ),
        )
        parsed = _best_effort_json(response.text)
        if heuristic_result["search_required"] and not parsed.get("search_required"):
            parsed["search_required"] = True
            parsed["reason"] = (
                f"{parsed.get('reason', '').strip()} "
                f"Heuristic freshness signal also matched."
            ).strip()
        if parsed.get("search_required") and not parsed.get("query"):
            parsed["query"] = heuristic_result["query"] or user_question
        return parsed
    except Exception as exc:
        print(exc)
        print("Gemini query classification failed, falling back to heuristic classification.")
        return heuristic_result

def ask_from_llm(token,query, fallback_context=None, formatted_query = None, force_search=False):
    print("Hello query")
    print("query loading")
    search_required = False
    primary_question = _extract_primary_question(query)
    contextual_question = _build_contextual_question(query)
    _log_query("primary_question", primary_question)
    _log_query("contextual_question", contextual_question)

    if not primary_question:
        safe_fallback = fallback_context or ""
        _log_query("primary_question_skipped", "empty question")
        return safe_fallback, False, ""

    if formatted_query is None:
        result = classify_and_build_query(contextual_question)
        print(result)
        formatted_query = result["query"].strip()
        _log_query("classifier_result", result)

        search_required = force_search or search_required or result["search_required"]
    else:
        _log_query("reused_formatted_query", formatted_query)
        search_required = force_search or bool(formatted_query)
    _log_query("force_search", force_search)


    print("Formatted Query:", formatted_query)
    _log_query("final_formatted_query", formatted_query)
    _log_query("search_required", search_required)

    return build_news_context(
        primary_question,
        formatted_query,
        fallback_context,
        search_required,
    )

if __name__ == "__main__":
    print("start")
    print(ask_from_llm("","what should messi do in the next world cup?"))
