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

import re
import requests
import urllib.robotparser
from urllib.parse import urlparse
from config_env import require_env
from kb_search import hybrid_search, iso_age_days
from rank_bm25 import BM25Okapi
from nltk.tokenize import word_tokenize
from nltk.tokenize import sent_tokenize


ROBOTS_TIMEOUT_SECONDS = 5


def is_scraping_allowed(url, user_agent='*'):
    # Parse the domain from the URL
    parsed_url = urlparse(url)
    base_url = f"{parsed_url.scheme}://{parsed_url.netloc}"
    robots_url = f"{base_url}/robots.txt"

    # Fetch robots.txt ourselves: RobotFileParser.read() has no timeout, and a
    # single hanging robots.txt stalled whole requests for ~75s. Status handling
    # mirrors read(): 401/403 disallow, other 4xx allow, anything else unparsed disallows.
    try:
        response = requests.get(robots_url, timeout=ROBOTS_TIMEOUT_SECONDS)
    except Exception as e:
        return False, str(e)
    if response.status_code in (401, 403):
        return False, f"{robots_url} returned {response.status_code}"
    if 400 <= response.status_code < 500:
        return True, f"No robots.txt at {robots_url}"
    if response.status_code >= 500:
        return False, f"{robots_url} returned {response.status_code}"

    rp = urllib.robotparser.RobotFileParser()
    rp.set_url(robots_url)
    rp.parse(response.text.splitlines())

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
from datetime import datetime, timedelta, timezone

from crawl4AI import crawl_page

def _log_query(label, value):
    print(f"[OffTheBar query] {label}: {value!r}")


# Words too common to show a search result is about the query.
RELEVANCE_STOPWORDS = {
    "the", "and", "for", "with", "who", "what", "when", "where", "which", "why",
    "how", "was", "were", "are", "did", "does", "has", "have", "had", "his",
    "her", "its", "their", "they", "this", "that", "from", "about", "will",
    "would", "can", "could", "latest", "news", "update", "updates", "today",
}


def _word_tokens(text):
    """Lowercase words of 3+ characters, as a set."""
    return {t for t in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(t) > 2}


# "Latest news" questions only get articles published within this many days;
# older or undated articles are dropped rather than presented as current.
LATEST_NEWS_MAX_AGE_DAYS = 30
LATEST_NEWS_MARKERS = [
    "latest", "news", "recent", "recently", "update", "updates", "today",
    "tonight", "yesterday", "last night", "this morning", "this week",
    "this weekend", "right now", "now", "currently", "current", "breaking",
    "rumour", "rumours", "rumor", "rumors", "last match", "last game",
]


def _is_latest_news_question(question, formatted_query):
    """True when the question asks for latest news, by its wording or by the
    classifier having pinned today's date into the search query."""
    return (
        _has_marker((question or "").lower(), LATEST_NEWS_MARKERS)
        or date.today().isoformat() in (formatted_query or "")
    )


ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")


def _strip_iso_dates(text):
    """The classifier pins today's date into search queries; as a search term
    it matches nothing in articles and narrows Bing News to a handful of
    results, so retrieval searches without it (recency is scored instead)."""
    return " ".join(ISO_DATE.sub(" ", text or "").split())


RELATIVE_AGE = re.compile(r"(\d+)\s*(minute|min|hour|day|week|month|year)s?\s+ago")
US_DATE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")
DAYS_PER_UNIT = {"minute": 0, "min": 0, "hour": 0, "day": 1, "week": 7, "month": 30, "year": 365}


def _search_result_age_days(result):
    """Age in days of a SearXNG result, or None if it carries no date.
    Bing News leaves publishedDate empty and puts the date in `metadata`
    instead, as "18 hours ago | Source" or "11/22/2024 | Source"."""
    age = iso_age_days(result.get("publishedDate"))
    if age is not None:
        return age

    stamp = str(result.get("metadata") or "").split("|")[0].strip().lower()
    if stamp in ("just now", "now", "today"):
        return 0
    if stamp == "yesterday":
        return 1
    match = RELATIVE_AGE.search(stamp)
    if match:
        return int(match.group(1)) * DAYS_PER_UNIT[match.group(2)]
    match = US_DATE.search(stamp)
    if match:
        month, day, year = (int(g) for g in match.groups())
        if month > 12:  # day-first format
            month, day = day, month
        try:
            return (date.today() - date(year, month, day)).days
        except ValueError:
            return None
    return None


def _published_label(result):
    """The "Published:" line shown to the model for a search result."""
    age = _search_result_age_days(result)
    if age is None:
        return "unknown - do not assume this is recent"
    published_on = (date.today() - timedelta(days=age)).isoformat()
    return f"{published_on} ({_days_ago(age)})"


# Run of Play chunks per answer, and when one is close and recent enough
# (Pinecone cosine and age) to answer without a web search. Latest-news
# questions need a very recent article for that.
KB_TOP_K = 4
KB_FRESH_SCORE = 0.7
KB_FRESH_DAYS = 30
KB_FRESH_DAYS_LATEST_NEWS = 7

# Web results per answer: scraped in full, then headline + snippet only.
WEB_SCRAPED_SOURCES = 2
WEB_SNIPPET_SOURCES = 4


def _days_ago(age):
    if age == 0:
        return "today"
    return f"{age} day{'' if age == 1 else 's'} ago"


def _format_kb_hit(hit):
    """A Run of Play chunk as context, labeled by age: anything past the
    latest-news window is background the model must not present as current."""
    md = hit["metadata"]
    age = hit["age_days"]
    if age is None:
        label = "RUN OF PLAY ARCHIVE - publish date unknown - background only, do not present as recent"
    else:
        published = f"published {(md.get('created') or '')[:10]}, {_days_ago(age)}"
        if age <= LATEST_NEWS_MAX_AGE_DAYS:
            label = f"RUN OF PLAY CONTEXT - {published}"
        else:
            label = f"RUN OF PLAY ARCHIVE - {published} - background only, not current news"
    return f"[{label}]\nArticle: {hit['title']}\n\n{md.get('chunk', '')}\n"


def build_news_context(query, formatted_query, fallback_context=None, search_required=None):
    """
    Resolve the context text to feed the LLM: Run of Play articles from the
    hybrid search (always some, labeled by age), or freshly scraped web
    search results once Phase 2 supplies the first draft's context.

    Returns (context_text, search_required, formatted_query).
    """
    print("Formatted query:", formatted_query)
    retrieval_query = _strip_iso_dates(formatted_query or query) or query
    relevance_query = retrieval_query
    latest_news = _is_latest_news_question(query, formatted_query)
    _log_query("retrieval_input", retrieval_query)
    _log_query("latest_news_question", latest_news)
    _log_query("fallback_context_supplied", fallback_context is not None)
    _log_query("web_search_requested", bool(search_required))

    if fallback_context is None:
        _log_query("kb_query", retrieval_query)
        try:
            hits = hybrid_search(retrieval_query, latest_news=latest_news, top_k=KB_TOP_K)
        except Exception as exc:
            print(f"Run of Play search failed: {exc}")
            _log_query("kb_failed", str(exc))
            hits = []
        if hits:
            kb_context = "\n\n".join(_format_kb_hit(hit) for hit in hits)
            fresh_days = KB_FRESH_DAYS_LATEST_NEWS if latest_news else KB_FRESH_DAYS
            if any(
                (hit["dense"] or 0) >= KB_FRESH_SCORE
                and hit["age_days"] is not None
                and hit["age_days"] <= fresh_days
                for hit in hits
            ):
                print("Using Run of Play context only - a close, recent article covers it")
                return kb_context, False, formatted_query
            # Older or looser matches still give the first draft something
            # to work with; Phase 2 searches the web for anything newer.
            print("Run of Play context is background only" + (" - web search left to Phase 2" if search_required else ""))
            return kb_context, search_required, formatted_query
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
    _log_query("searx_query", retrieval_query)
    try:
        params = {
            "q": retrieval_query,
            "format": "json",
            "language": "en",
            "safesearch": 2,
            # Our self-hosted SearXNG runs on Cloud Run; its outbound IPs get
            # instant CAPTCHAs/rate-limits from duckduckgo/google/brave/startpage
            # (a universal problem for cloud-hosted SearXNG, not fixable via
            # config). Plain Bing stopped blocking and started serving junk
            # instead (as of 2026-10: App Store pages, login pages, adult
            # sites for "who won the 2010 world cup"), so only Bing News,
            # which still returns relevant results, is used.
            "engines": "bing news",
        }
        if latest_news:
            # Bing News ranks by relevance, not date - without this, half its
            # results for "Messi performance" were months or a year old.
            params["time_range"] = "month"

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
    # whole-word overlap on at least 2 distinct query terms (or all of them,
    # if the query is short), ignoring filler words like "the"/"who" that
    # junk pages match just as well.
    query_tokens = _word_tokens(relevance_query) - RELEVANCE_STOPWORDS
    min_overlap = min(2, len(query_tokens)) or 1

    def _is_relevant(ctx):
        haystack = _word_tokens(f"{ctx.get('title', '')} {ctx.get('content', '')}")
        return len(query_tokens & haystack) >= min_overlap

    # No fallback to the unfiltered list: when nothing is relevant, those
    # results are exactly the junk this filter exists to keep out (adult
    # sites, observed 2026-10), so the answer gets no web context instead.
    candidate_results = [r for r in results.get("results", []) if _is_relevant(r)]
    if not candidate_results:
        print("Relevance filter matched nothing; using no web context.")
        _log_query("web_search_no_relevant_results", formatted_query)
        return "", search_required, formatted_query

    # A latest-news question only gets recent articles - Bing News happily
    # returns two-year-old pieces (a 2024 Inter Miami quote was presented as
    # current in a "latest news on Messi" answer). Filtered before scraping.
    if latest_news:
        recent_results = []
        for res in candidate_results:
            age = _search_result_age_days(res)
            if age is not None and age <= LATEST_NEWS_MAX_AGE_DAYS:
                recent_results.append(res)
            else:
                reason = "no publish date" if age is None else f"{age} days old"
                print(f"Dropping {res['url']} - {reason}, too old for a latest-news question")
        candidate_results = recent_results
        if not candidate_results:
            print("No results recent enough for a latest-news question; using no web context.")
            _log_query("web_search_no_recent_results", formatted_query)
            return "", search_required, formatted_query

    # Full text from up to WEB_SCRAPED_SOURCES pages. Results that are
    # blocked (robots.txt, anti-bot walls like NYT's) or past that limit
    # still contribute Bing News's own headline and snippet, so one blocked
    # scrape no longer leaves the answer with nothing.
    prompt_context = []
    snippet_only = []
    for ctx in candidate_results:
        if len(prompt_context) >= WEB_SCRAPED_SOURCES:
            snippet_only.append(ctx)
            continue
        scrape_premission = is_scraping_allowed(ctx["url"])
        if scrape_premission[0] == False:
            print(f"Skipping {ctx['url']} - scraping not allowed by robots.txt")
            print(scrape_premission[1])
            snippet_only.append(ctx)
            continue

        page_text = crawl_page(ctx['url'])
        if not page_text:
            print(f"Skipping {ctx['url']} - no content found")
            snippet_only.append(ctx)
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
            snippet_only.append(ctx)
            continue
        top_chunks = rank_chunks_bm25(page_chunks, relevance_query, top_k=5)
        print(f"Top chunks for {ctx['url']}: {len(top_chunks)}")
        # print(top_chunks[0])
        top_chunks_str = '\n\n'.join(top_chunks)
        prompt_context.append(f"NEWS - {ctx['title']} :  {ctx['url']}\nPublished: {_published_label(ctx)}\nPage content: {page_contents.get(ctx['url'], '')} \n\n{page_metadata} {top_chunks_str}\n")

    for ctx in snippet_only[:WEB_SNIPPET_SOURCES]:
        print(f"Using search snippet only for {ctx['url']}")
        prompt_context.append(
            f"NEWS (headline and search snippet only) - {ctx['title']} :  {ctx['url']}\n"
            f"Published: {_published_label(ctx)}\n"
            f"Snippet: {ctx.get('content') or ctx.get('snippet', '')}\n"
        )

    print(len(prompt_context))
    if not prompt_context:
        return "", search_required, formatted_query
    prompt_context = "\n\n".join(prompt_context)

    formatted_prompt_short = prompt_begin + "Recent news:\n\n" + prompt_context

    return formatted_prompt_short, search_required, formatted_query



import json

from gemini_utils import call_gemini_with_key_retry
from prompt_templates import build_search_classifier_prompt


def _has_marker(text: str, markers: list[str]) -> bool:
    """Whole-word/phrase match, so "live" doesn't match "Liverpool" or "form" "formation"."""
    return re.search(r"\b(?:" + "|".join(re.escape(m) for m in markers) + r")\b", text) is not None


def _heuristic_query_classifier(user_question: str) -> dict:
    question = user_question.strip()
    # Match markers against the current question only - the previous questions
    # included for pronoun resolution would otherwise carry one turn's keywords
    # (e.g. "FIFA") into the classification of every later turn.
    lowered = question.rsplit("Current question:", 1)[-1].lower()

    # Only unambiguous game terms. Bare "FIFA" is usually the governing body,
    # "my team" is often a real club, and "pes" without word boundaries matched
    # "hopes" - those are left to the Gemini classifier, which has its own
    # video-game rule. Game titles like "FIFA 23" / "FC 25" still count.
    gaming_markers = [
        "ea fc", "ea sports fc", "efootball", "pes", "ultimate team", "career mode",
        "fut", "my formation", "my squad", "player instructions", "custom tactics",
    ]
    if _has_marker(lowered, gaming_markers) or re.search(r"\b(?:fifa|fc) ?\d{2}\b", lowered):
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
        "will",
    ]
    evergreen_markers = [
        "favourite", "favorite", "best ever", "greatest ever", "of all time",
        "who is better", "who was better", "legacy", "history of",
        "historical", "all-time", "all time", "why was", "why is",
        "what is the offside rule", "size of a football pitch",
    ]

    if _has_marker(lowered, fresh_markers):
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

    if _has_marker(lowered, evergreen_markers):
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
        # Gemini's verdict stands even when a freshness keyword matched: the
        # keyword list can't tell history from news (it forced a web search
        # for "who won the 2010 World Cup" over Gemini's "evergreen"). The
        # heuristic is only the fallback for when this call fails.
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
