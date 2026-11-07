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
import time
import unicodedata
import requests
import urllib.robotparser
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse
from config_env import require_env
from kb_search import article_url, hybrid_search, iso_age_days, known_tags
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

from crawl4AI import crawl_pages

def _log_query(label, value):
    print(f"[OffTheBar query] {label}: {value!r}")


# Words too common to show a search result is about the query.
RELEVANCE_STOPWORDS = {
    "the", "and", "for", "with", "who", "what", "when", "where", "which", "why",
    "how", "was", "were", "are", "did", "does", "has", "have", "had", "his",
    "her", "its", "their", "they", "this", "that", "from", "about", "will",
    "would", "can", "could", "latest", "news", "update", "updates", "today",
}


# American football must never reach an answer. Bing News reads a bare
# "football" as the NFL ("football injury news": 8 of 8 results; "best
# football player this week": 5 of 5, fantasy start/sit and waiver pages), so
# web results go through three layers: the search query names soccer
# (_soccer_search_query), a flash-lite check keeps only results about
# association football (_keep_soccer_results), and these unambiguous terms
# drop results and scraped paragraphs before either. Not "sack" or
# "interception" (association football words too), not team names ("Eagles",
# "Saints", "Lions" are Crystal Palace, Southampton, Millwall), and not
# "fantasy football" (Fantasy Premier League is welcome).
AMERICAN_FOOTBALL = re.compile(
    r"\b(?:nfl|ncaa|quarterbacks?|qbs?|touchdowns?|super bowl|gridiron|american football|college football|"
    r"high school|varsity|wide receivers?|running backs?|linebackers?|tight ends?|field goals?|rushing yards|"
    r"passing yards|end zone|heisman|flag football|(?:thursday|sunday|monday) night football|start[/ -]sit|"
    r"waiver wire|ppr)\b",
    re.I,
)

# Words that mark text as association football, alongside Run of Play's own
# tags (clubs, players, competitions).
SOCCER_TERMS = re.compile(
    r"\b(?:soccer|fpl|fantasy premier league|premier league|la ?liga|serie a|bundesliga|ligue 1|eredivisie|mls|"
    r"champions league|europa league|conference league|fa cup|carabao cup|efl|world cup|euro \d{4}|copa|afcon|"
    r"uefa|fifa|ballon d ?or|clasico)\b"
)
# Run of Play tags that don't mark text as soccer on their own ("USA
# Football" governs American football; the AFC is an NFL conference too;
# Run of Play's "Fantasy Football" means FPL, Bing's the NFL; CAS hears every sport).
AMBIGUOUS_TAGS = {"football", "fantasy football", "usa", "canada", "australia", "ireland", "afc", "cas", "transfer news"}


def _is_american_football(result):
    return bool(AMERICAN_FOOTBALL.search(f"{result.get('title', '')} {result.get('content', '')}"))


def _folded_words(text):
    """Lowercase, accent-folded words, in order."""
    return re.findall(r"[a-z0-9]+", unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower())


def _names_soccer(text):
    """Whether the text names a soccer competition or one of Run of Play's
    tags (whole names: "lionel messi", "real madrid")."""
    words = _folded_words(text)
    if SOCCER_TERMS.search(" ".join(words)):
        return True
    phrases = {" ".join(words[i:i + n]) for n in range(1, 5) for i in range(len(words) - n + 1)}
    tags = {" ".join(_folded_words(tag)) for tag in known_tags()} - AMBIGUOUS_TAGS
    return bool(phrases & tags)


def _soccer_search_query(query):
    """The query as sent to Bing News. "Fantasy football" becomes Fantasy
    Premier League, and a "football" query that names no soccer competition,
    club or player gets "Premier League" in its place: "football injury news"
    returned 8 NFL stories of 8, "Premier League injury news" 10 soccer ones."""
    query = re.sub(r"\bfantasy football\b", "Fantasy Premier League", query, flags=re.I)
    if not re.search(r"\bfootball\b", query, re.I) or _names_soccer(query):
        return query
    return " ".join(re.sub(r"\bfootball\b", "Premier League", query, flags=re.I).split())


def _finished_entries(text):
    """The complete {"n": ...} entries of a JSON reply that stopped part-way."""
    entries = []
    for raw in re.findall(r'\{"n":\s*\d+(?:[^{}"]|"(?:[^"\\]|\\.)*")*\}', text or ""):
        try:
            entries.append(json.loads(raw))
        except ValueError:
            continue
    return entries


def _keep_soccer_results(question, results):
    """The results about association football, judged by gemini-2.5-flash-lite
    from their site, date, headline and snippet, each marked with what that
    same call found (result["check"]: kind, event_date, facts). If the call
    fails, only results that name soccer outright are kept, unmarked - never
    the uncertain ones."""
    if not results:
        return results
    lines = []
    for n, r in enumerate(results, 1):
        age = _search_result_age_days(r)
        published = (date.today() - timedelta(days=age)).isoformat() if age is not None else "unknown"
        lines.append(f"{n}. [{urlparse(r.get('url', '')).hostname or ''}] {published} | {r.get('title', '')} - {(r.get('content') or '')[:200]}")
    prompt = build_soccer_results_prompt(question, "\n".join(lines), date.today())
    try:
        response = call_gemini_with_key_retry(
            "gemini-2.5-flash-lite",
            lambda client: client.models.generate_content(
                model="gemini-2.5-flash-lite", contents=prompt, config=SOCCER_CHECK_CONFIG
            ),
        )
        verdict = _best_effort_json(response.text)
        entries = verdict.get("results") if isinstance(verdict, dict) else verdict
        cut_off = not isinstance(entries, list) or not entries
        if cut_off:
            entries = _finished_entries(response.text)
            finish = getattr((response.candidates or [None])[0], "finish_reason", None)
            print(f"Soccer check reply cut off ({finish}): {len(entries)} finished entries of {len(results)}")
        if not entries:
            raise ValueError(f"no results list in {response.text!r}")
        kept = {int(entry["n"]): entry for entry in entries if entry.get("keep")}
        if cut_off:
            judged = {int(entry["n"]) for entry in entries}
            kept.update({
                n: {} for n, r in enumerate(results, 1)
                if n not in judged and _names_soccer(f"{r.get('title', '')} {r.get('content', '')}")
            })
    except Exception as exc:
        print(f"Soccer check failed, keeping only results that name soccer: {exc}")
        kept = {n: {} for n, r in enumerate(results, 1) if _names_soccer(f"{r.get('title', '')} {r.get('content', '')}")}
    for n, r in enumerate(results, 1):
        if n not in kept:
            print(f"Dropping {r['url']} - not association football")
    return [dict(r, check=kept[n]) for n, r in enumerate(results, 1) if n in kept]


def _name_endings(query):
    """The last word of each capitalised name in a search query: "Lino" of
    "Samuel Lino", "United" of "Manchester United"."""
    names = re.findall(r"[A-Z\u00c0-\u00de][\w'\u2019-]*(?:\s+[A-Z\u00c0-\u00de][\w'\u2019-]*)*", query or "")
    return set().union(*(_word_tokens(name.split()[-1]) for name in names)) - RELEVANCE_STOPWORDS - NOT_NAME_ENDINGS if names else set()


def _word_tokens(text):
    """Lowercase, accent-folded words of 3+ characters, as a set. Without the
    folding, "Clásico" became "sico" and "Mbappé" "mbapp", so results spelled
    "Clasico"/"Mbappe" shared no words with the query and were dropped."""
    folded = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    return {t for t in re.findall(r"[a-z0-9]+", folded) if len(t) > 2}


# "Latest news" questions only get articles published within this many days;
# older or undated articles are dropped rather than presented as current.
LATEST_NEWS_MAX_AGE_DAYS = 30
# For those questions, Run of Play's articles only count as answering them
# (a draft from them, or an answer from them alone) if one is this recent.
LATEST_NEWS_COVERAGE_MAX_AGE_DAYS = 14
LATEST_NEWS_MARKERS = [
    "latest", "news", "recent", "recently", "update", "updates", "today",
    "tonight", "yesterday", "last night", "this morning", "this week",
    "this weekend", "right now", "now", "currently", "current", "breaking",
    "rumour", "rumours", "rumor", "rumors", "last match", "last game",
    # questions about how someone is doing now ("How is United doing under Carrick?")
    "doing", "lately", "these days", "at the moment", "form", "this season", "so far",
    # what happens next depends on where things stand now
    "what's next", "whats next", "next match", "next game", "upcoming", "title race", "standings",
    # slang for the same ("yo whats good with united rn")
    "rn", "what's good", "whats good", "what's happening", "whats happening",
    "going on", "what's up", "whats up", "getting on", "this year",
]


# Questions about the next match: fixtures, previews, predicted line-ups. A
# preview is only useful while its match is still ahead, so these get web
# results and Run of Play articles from the last FIXTURE_MAX_AGE_DAYS only.
# "barca upcoming fixture predicted XI" got 10- and 13-day-old previews of
# matches already played (PSG, Betis) and gave the PSG match as upcoming,
# while Bing News had same-day previews of the real one (Alavés).
FIXTURE_MAX_AGE_DAYS = 3
FIXTURE_MARKERS = [
    "upcoming", "next match", "next game", "next fixture", "fixture", "fixtures",
    "predicted xi", "predicted lineup", "predicted line-up", "lineup", "line-up", "line up",
    "starting xi", "team news", "preview", "play next", "playing next", "face next",
    "kick-off", "kick off",
]
# Who matters in a coming match: without a line-up in the material the answer
# names players from the model's memory ("Gabriel Martinelli", long gone).
MATCHUP_PLAYER_MARKERS = ["key player", "key players", "who starts", "who will start", "players to watch", "danger man"]
# Words that describe the fixture rather than name the club - as search terms
# they pulled in other clubs' previews ("Barcelona upcoming fixture predicted
# XI" over the last week: Nice, Le Havre, Deportivo), while "Barcelona preview"
# and "Barcelona predicted lineup" found the Alavés previews.
FIXTURE_QUERY_NOISE = {
    "key", "player", "players", "versus", "else", "might", "be",
    "upcoming", "next", "fixture", "fixtures", "match", "game", "predicted", "xi", "lineup",
    "line-up", "line", "up", "starting", "team", "news", "preview", "play", "playing", "face",
    "kick-off", "kick", "off", "this", "weekend", "their", "the", "what", "who", "when", "is",
    "are", "will", "do", "does", "for", "vs", "v", "against",
}


def _is_fixture_question(question, formatted_query):
    """Asks about the next match - unless the question names a past year
    ("Barcelona's lineup in the 2009 final")."""
    past_year = any(int(year) < date.today().year for year in re.findall(r"\b(?:19|20)\d{2}\b", question or ""))
    text = f"{question or ''} {formatted_query or ''}".lower()
    matchup = _has_marker(text, MATCHUP_PLAYER_MARKERS) and _has_marker(text, ["against", "vs", "versus"])
    return not past_year and (matchup or _has_marker(text, FIXTURE_MARKERS))


def _fixture_search_query(query):
    """'<club> preview predicted lineup' - the wording Bing News answers with
    previews of the club's next match. The query unchanged if it names nothing."""
    names = [re.sub(r"'s$", "", word) for word in re.findall(r"[\w'-]+", query or "")
             if word.lower() not in FIXTURE_QUERY_NOISE]
    return " ".join(names + ["preview", "predicted", "lineup"]) if names else query


def _is_latest_news_question(question, formatted_query):
    """True when the question asks for latest news, by its wording or by the
    classifier having read it that way: a date from the last month, this
    season's label or "recent" in the search query."""
    return (
        _has_marker((question or "").lower(), LATEST_NEWS_MARKERS)
        or _names_recent_date(formatted_query)
        or _season_label(date.today()) in (formatted_query or "")
        or _has_marker((formatted_query or "").lower(), ["recent"])
    )


def _names_recent_date(text):
    for found in ISO_DATE.findall(text or ""):
        try:
            if abs((date.today() - date.fromisoformat(found)).days) <= LATEST_NEWS_MAX_AGE_DAYS:
                return True
        except ValueError:
            continue
    return False


ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
# time_range already keeps latest-news searches current. In the query the season
# narrows Bing News to fixture pages: "Vincent Kompany injury status 2026-27"
# found a match blog, without it news from hours earlier.
SEASON_LABEL = re.compile(r"\b20\d{2}[-/](?:20)?\d{2}\b")
# "stats" pulls Bing News to American sports: "Samuel Lino season performance
# stats" found baseball pages, without it his club news.
STATS_WORD = re.compile(r"\b(?:stats?|statistics)\b", re.I)


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
    published_on = date.today() - timedelta(days=age)
    return f"{published_on.isoformat()} ({_days_ago(age)}{_season_note(published_on.isoformat())})"


# Run of Play chunks per answer.
KB_TOP_K = 4

# Web results per answer: scraped in full, then headline + snippet only.
# At most WEB_SCRAPE_ATTEMPTS pages are fetched, all at once: with MSN, NYT
# and others blocking the scraper, trying every result until two succeeded
# took the combined Clásico answer 50s to its first words.
WEB_SCRAPED_SOURCES = 2
WEB_SNIPPET_SOURCES = 4
WEB_SCRAPE_ATTEMPTS = 3

# Sites whose pages always come back as anti-bot walls: their headline and
# snippet are used without trying to scrape them.
SCRAPE_BLOCKED_DOMAINS = ("msn.com", "nytimes.com", "thesun.co.uk")


def _scrape_blocked(url):
    host = (urlparse(url).hostname or "").lower()
    return any(host == domain or host.endswith("." + domain) for domain in SCRAPE_BLOCKED_DOMAINS)


# Pre-match pieces, by headline; a word like "predictions" is left to the
# soccer check, since "Serie A predictions: Inter backed to retain their crown"
# is a season forecast, not a match preview. "what's up with atletico" read previews of a
# match already played and said they were "still hunting for their first
# points", though they had won it 2-0. Next-match questions want these; for any
# other question a recent one goes last, labeled, and an old one is dropped.
PREVIEW_HEADLINE = re.compile(
    r"\b(?:previews?|how to watch|where to watch|live ?stream(?:s|ing)?|predicted (?:xi|starting)|line-?ups?|"
    r"betting tips|team news|tv|kick-?off)\b",
    re.I,
)


def _is_preview(result):
    """By the soccer check's reading where there is one, else by headline."""
    kind = (result.get("check") or {}).get("kind")
    return kind == "preview" if kind else bool(PREVIEW_HEADLINE.search(result.get("title") or ""))


def _match_was_played(result):
    """A preview whose event date, as the soccer check read it, has passed."""
    try:
        return _is_preview(result) and date.fromisoformat(str((result.get("check") or {}).get("event_date"))[:10]) < date.today()
    except ValueError:
        return False


def _sort_previews(results):
    """Reports first, recent previews last, and no preview old enough for its
    match to have been played."""
    kept = []
    for res in results:
        age = _search_result_age_days(res)
        if _is_preview(res) and (age is None or age > FIXTURE_MAX_AGE_DAYS):
            print(f"Dropping {res['url']} - old preview, its match has probably been played")
        else:
            kept.append(res)
    return sorted(kept, key=_is_preview)


def _news_label(result, fixture, snippet=False):
    notes = ["headline and search snippet only"] if snippet else []
    if _is_preview(result) and not fixture:
        notes.append("pre-match preview, written before the match, not a result")
    kind = (result.get("check") or {}).get("kind")
    if kind in ("rumour", "opinion"):
        notes.append({"rumour": "unconfirmed rumour", "opinion": "opinion piece"}[kind])
    return f"NEWS ({'; '.join(notes)})" if notes else "NEWS"


def _checked_lines(result):
    """What the soccer check read from a result, for the context."""
    check = result.get("check") or {}
    lines = [f"Likely event date: {check['event_date']}"] if check.get("event_date") else []
    facts = [str(fact) for fact in (check.get("facts") or [])][:2]
    if facts:
        lines.append("Key facts:\n" + "\n".join(f"- {fact}" for fact in facts))
    return "\n".join(lines) + "\n" if lines else ""


def _days_ago(age):
    if age == 0:
        return "today"
    return f"{age} day{'' if age == 1 else 's'} ago"


def _season_note(created):
    """", 2025-26 season" for a publish date, so an April article isn't read as
    part of the season running now."""
    try:
        return f", {_season_label(date.fromisoformat((created or '')[:10]))} season"
    except ValueError:
        return ""


def _format_kb_hit(hit):
    """A Run of Play chunk as context, labeled by age: anything past the
    latest-news window, or a new article about someone else, is background the
    model must not present as current."""
    md = hit["metadata"]
    age = hit["age_days"]
    if age is None:
        label = "RUN OF PLAY ARCHIVE - publish date unknown - background only, do not present as recent"
    else:
        published = f"published {(md.get('created') or '')[:10]}, {_days_ago(age)}{_season_note(md.get('created'))}"
        if age <= LATEST_NEWS_MAX_AGE_DAYS and hit.get("on_subject", True):
            label = f"RUN OF PLAY CONTEXT - {published}"
        else:
            label = f"RUN OF PLAY ARCHIVE - {published} - background only, not current news"
    facts = md.get("facts")
    if label.startswith("RUN OF PLAY ARCHIVE") and facts:
        # An old article's own text says "Joao Pedro, Brighton's prolific
        # striker" in the present tense, and answers repeated it as current.
        # Its extracted facts say which parts still hold and date the rest.
        return f"[{label}]\nArticle: {hit['title']}\n\n{_format_facts(facts, (md.get('created') or '')[:10] or 'publication')}\n"
    # A recent article keeps its text, with what it says that can change spelled
    # out, since a 25-day-old injury or line-up may already be out of date.
    changing = _format_facts({"time_bound_facts": facts.get("time_bound_facts")}, (md.get('created') or '')[:10]) if facts else ""
    return f"[{label}]\nArticle: {hit['title']}\n\n{md.get('chunk', '')}\n" + (f"\n{changing}\n" if changing else "")


def _format_facts(facts, published):
    sections = [
        ("Lasting facts", facts.get("lasting_facts")),
        (f"True as of {published} - may have changed since (clubs, managers, injuries, form, standings, line-ups)",
         facts.get("time_bound_facts")),
        (f"Still to come as of {published} - may have happened since", facts.get("scheduled_events")),
        (f"The writer's opinions and predictions as of {published}", facts.get("opinions")),
    ]
    return "\n".join(f"{name}:\n" + "\n".join(f"- {item}" for item in items) for name, items in sections if items)


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
        # The classifier's rewrite can drop the very name the user asked
        # about ("How is United doing under Carrick?" became "Manchester
        # United 2026-27 season performance manager"), so the user's own
        # words are searched too.
        kb_query = retrieval_query if query.lower() in retrieval_query.lower() else f"{retrieval_query} {query}"
        _log_query("kb_query", kb_query)
        try:
            hits = hybrid_search(kb_query, latest_news=latest_news, top_k=KB_TOP_K)
        except Exception as exc:
            print(f"Run of Play search failed: {exc}")
            _log_query("kb_failed", str(exc))
            hits = []
        if hits:
            if latest_news:
                # Newest first: the answer leans on what it reads first, and in
                # relevance order "what's going on with nottingham" opened with
                # April's relegation fight, ahead of a 20-day-old article.
                hits = sorted(hits, key=lambda hit: hit["age_days"] if hit["age_days"] is not None else float("inf"))
            kb_context = "\n\n".join(_format_kb_hit(hit) for hit in hits)
            # Whether these articles are enough on their own is the backend's
            # call (judge_kb_coverage); the web search is left to Phase 2.
            return kb_context, search_required, formatted_query
    else:
        _log_query("pinecone_skipped", "fallback context was supplied")

    if not search_required:
        _log_query("web_search_skipped", "search_required is false")
        return fallback_context or "", False, formatted_query

    # Results are matched against the query as sent, so a rewritten "Premier
    # League injury news" doesn't drop soccer headlines for lacking "football".
    fixture = _is_fixture_question(query, formatted_query)
    _log_query("fixture_question", fixture)
    search_query = _soccer_search_query(_fixture_search_query(retrieval_query) if fixture else retrieval_query)
    if latest_news:
        search_query = " ".join(STATS_WORD.sub(" ", SEASON_LABEL.sub(" ", search_query)).split()) or search_query
    relevance_query = search_query
    url = f"{require_env('SEARXNG_URL').rstrip('/')}/search"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
    }
    results = {"results": []}
    print("Searching for news context...")
    _log_query("searx_query", search_query)
    try:
        params = {
            "q": search_query,
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
        if fixture:
            params["time_range"] = "week"
        elif latest_news:
            # Bing News ranks by relevance, not date - without this, half its
            # results for "Messi performance" were months or a year old.
            params["time_range"] = "month"

        # Bing News occasionally answers a query with nothing and then with 10
        # results a second later, so an empty answer gets one retry.
        for attempt in range(2):
            response = requests.get(url, headers=headers, params=params, timeout=10)
            response.raise_for_status()
            results = response.json()
            if results.get("results") or attempt:
                break
            print("Search returned no results; retrying once.")
            time.sleep(1)
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
    # A preview published today of Saturday's match was answered as "against
    # Alavés today" - publish dates aren't match dates, and naming the coming
    # days lets "this Saturday" in an article be placed.
    coming_days = ", ".join(f"{date.today() + timedelta(days=n):%A %Y-%m-%d}" for n in range(1, 8))
    prompt_begin = (
        f"Today's real date is {date.today():%A %Y-%m-%d}. The coming days: {coming_days}.\n"
        "A \"Published\" date is when an article came out, not when the match it covers is played: "
        "take match days from the text (\"this Saturday\", \"Tuesday night\") and place them with the dates above.\n"
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
    # junk pages match just as well. A name's last word is enough alone:
    # "Pedro and Lino shine" is about "Samuel Lino", "Lionel" isn't about "Lionel Messi".
    # Words like "season" say nothing about who it's about, so "lino season
    # performance" comes down to "lino" (the soccer check then judges the results).
    query_tokens = (_word_tokens(relevance_query) - RELEVANCE_STOPWORDS - QUERY_FILLER_WORDS) or _word_tokens(relevance_query)
    min_overlap = min(2, len(query_tokens)) or 1
    surnames = _name_endings(relevance_query)

    def _is_relevant(ctx):
        haystack = _word_tokens(f"{ctx.get('title', '')} {ctx.get('content', '')}")
        return len(query_tokens & haystack) >= min_overlap or bool(surnames & haystack)

    # No fallback to the unfiltered list: when nothing is relevant, those
    # results are exactly the junk this filter exists to keep out (adult
    # sites, observed 2026-10), so the answer gets no web context instead.
    other_sport = [r for r in results.get("results", []) if _is_american_football(r)]
    for res in other_sport:
        print(f"Dropping {res['url']} - American football")
    candidate_results = [r for r in results.get("results", []) if _is_relevant(r) and not _is_american_football(r)]
    if not candidate_results:
        print("Relevance filter matched nothing; using no web context.")
        _log_query("web_search_no_relevant_results", formatted_query)
        return "", search_required, formatted_query

    # A latest-news question only gets recent articles - Bing News happily
    # returns two-year-old pieces (a 2024 Inter Miami quote was presented as
    # current in a "latest news on Messi" answer). Filtered before scraping.
    # Next-match questions get only the last few days (FIXTURE_MAX_AGE_DAYS).
    if latest_news or fixture:
        max_age = FIXTURE_MAX_AGE_DAYS if fixture else LATEST_NEWS_MAX_AGE_DAYS
        recent_results = []
        for res in candidate_results:
            age = _search_result_age_days(res)
            if age is not None and age <= max_age:
                recent_results.append(res)
            else:
                reason = "no publish date" if age is None else f"{age} days old"
                print(f"Dropping {res['url']} - {reason}, too old for a {'next-match' if fixture else 'latest-news'} question")
        candidate_results = recent_results
        if not candidate_results:
            print("No results recent enough for a latest-news question; using no web context.")
            _log_query("web_search_no_recent_results", formatted_query)
            return "", search_required, formatted_query

    if not fixture:
        candidate_results = _sort_previews(candidate_results)
        if not candidate_results:
            print("Only old match previews found; using no web context.")
            _log_query("web_search_only_old_previews", formatted_query)
            return "", search_required, formatted_query

    candidate_results = _keep_soccer_results(query, candidate_results)
    for res in [res for res in candidate_results if _match_was_played(res)]:
        print(f"Dropping {res['url']} - preview of a match already played")
    candidate_results = [res for res in candidate_results if not _match_was_played(res)]
    if not fixture:
        candidate_results = _sort_previews(candidate_results)
    if not candidate_results:
        print("No association football results; using no web context.")
        _log_query("web_search_no_soccer_results", formatted_query)
        return "", search_required, formatted_query

    # Full text from up to WEB_SCRAPED_SOURCES pages. Results that are
    # blocked (robots.txt, anti-bot walls like NYT's) or past that limit
    # still contribute Bing News's own headline and snippet, so one blocked
    # scrape no longer leaves the answer with nothing.
    to_scrape = [ctx["url"] for ctx in candidate_results if not _scrape_blocked(ctx["url"])][:WEB_SCRAPE_ATTEMPTS]
    with ThreadPoolExecutor(max_workers=WEB_SCRAPE_ATTEMPTS) as pool:
        robots = dict(zip(to_scrape, pool.map(is_scraping_allowed, to_scrape)))
    for page_url, (allowed, reason) in robots.items():
        if not allowed:
            print(f"Skipping {page_url} - scraping not allowed by robots.txt")
            print(reason)
    pages = crawl_pages([page_url for page_url in to_scrape if robots[page_url][0]])

    prompt_context = []
    snippet_only = []
    for ctx in candidate_results:
        page_text = pages.get(ctx["url"], "")
        if len(prompt_context) >= WEB_SCRAPED_SOURCES or not page_text:
            if ctx["url"] in pages and not page_text:
                print(f"Skipping {ctx['url']} - no content found")
            snippet_only.append(ctx)
            continue

        page_metadata = page_text[:200]  # Get first 1000 characters as metadata
        if AMERICAN_FOOTBALL.search(page_metadata):
            page_metadata = ""
        print("Chunking started for", ctx['url'])
        # A soccer article's page still carries other sports in its menus and
        # sidebars, so paragraphs with American football terms are dropped.
        page_chunks = [chunk for chunk in chunk_article(page_text) if not AMERICAN_FOOTBALL.search(chunk)]
        print(f"Page chunks for {ctx['url']}: {len(page_chunks)}")
        if not page_chunks:
            # crawl_pages can return text that survives the `if not page_text`
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
        prompt_context.append(f"{_news_label(ctx, fixture)} - {ctx['title']} :  {ctx['url']}\nPublished: {_published_label(ctx)}\n{_checked_lines(ctx)}Page content: {page_contents.get(ctx['url'], '')} \n\n{page_metadata} {top_chunks_str}\n")

    for ctx in snippet_only[:WEB_SNIPPET_SOURCES]:
        print(f"Using search snippet only for {ctx['url']}")
        prompt_context.append(
            f"{_news_label(ctx, fixture, snippet=True)} - {ctx['title']} :  {ctx['url']}\n"
            f"Published: {_published_label(ctx)}\n"
            f"{_checked_lines(ctx)}"
            f"Snippet: {ctx.get('content') or ctx.get('snippet', '')}\n"
        )

    print(len(prompt_context))
    if not prompt_context:
        return "", search_required, formatted_query
    prompt_context = "\n\n".join(prompt_context)

    formatted_prompt_short = prompt_begin + "Recent news:\n\n" + prompt_context

    return formatted_prompt_short, search_required, formatted_query



import json

from google.genai import types as genai_types

from gemini_utils import call_gemini_with_key_retry

# The classifier and the coverage check are decisions, not writing: sampled at
# Gemini's default temperature the same question flipped between runs
# (search queries dropping "Carrick", coverage "none" vs "partial").
DECISION_CONFIG = genai_types.GenerateContentConfig(temperature=0)
# Capped: flash-lite now and then loops to its 65k-token limit, and this runs
# on every web search. A cut-off reply falls back to names-only filtering.
SOCCER_CHECK_CONFIG = genai_types.GenerateContentConfig(
    temperature=0, response_mime_type="application/json", max_output_tokens=3000
)
from prompt_templates import (
    _season_label,
    build_relevance_check_prompt,
    build_search_classifier_prompt,
    build_soccer_results_prompt,
)


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


LAST_ANSWER_EXCERPT_CHARS = 1200


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


def _last_answer_excerpt(raw_query: str) -> str:
    """The end of the conversation's latest answer: a follow-up can point at
    something only the answer mentioned (\"the Bayern Munich match\")."""
    text = raw_query or ""
    if "Context given:" not in text:
        return ""
    answers = text.split("Context given:", 1)[1].split("Previous Query:", 1)[0]
    return _strip_error_noise(answers.strip())[-LAST_ANSWER_EXCERPT_CHARS:]


def _build_contextual_question(raw_query: str) -> str:
    primary_question = _extract_primary_question(raw_query)
    previous_questions = _extract_previous_user_queries(raw_query)
    if not previous_questions:
        return primary_question

    previous_text = "\n".join(f"- {question}" for question in previous_questions)
    last_answer = _last_answer_excerpt(raw_query)
    answer_text = f"End of the last answer:\n{last_answer}\n\n" if last_answer else ""
    return (
        "Previous user questions, for resolving pronouns and follow-ups:\n"
        f"{previous_text}\n\n"
        f"{answer_text}"
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
                model="gemini-2.5-flash-lite", contents=prompt, config=DECISION_CONFIG
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

KB_COVERAGE = ("full", "partial", "none")


RUN_OF_PLAY_BLOCK = re.compile(r"(?=\[RUN OF PLAY (?:CONTEXT|ARCHIVE))")
# A new article labeled ARCHIVE is about someone else (kb_search's on_subject).
RECENT_ARCHIVE_LABEL = re.compile(r"\[RUN OF PLAY ARCHIVE - published [^,\]]*, (?:today|[12]?\d days? ago|30 days ago)")


def order_context(kb_context, web_context):
    """Context for an answer from both sources, in priority order: Run of
    Play's recent articles, then the web results, then Run of Play's archive.
    The archive used to come before the web news, and answers lead with what
    they read first."""
    blocks = [block.strip() for block in RUN_OF_PLAY_BLOCK.split(kb_context or "") if block.strip()]
    recent = [block for block in blocks if not block.startswith("[RUN OF PLAY ARCHIVE")]
    archive = [block for block in blocks if block.startswith("[RUN OF PLAY ARCHIVE")]
    return "\n\n".join(recent + ([web_context.strip()] if web_context and web_context.strip() else []) + archive)


RUN_OF_PLAY_SITE = "https://www.therunofplay.com/"
# Shared names or figures that count as an answer drawing on an article. At 2,
# "Real" and "Madrid" alone linked a Vinicius transfer piece to a Dybala answer.
NOT_NAME_ENDINGS = {"league", "cup", "football", "soccer", "news"}
QUERY_FILLER_WORDS = {"season", "performance", "form", "result", "results", "stats", "recent", "latest", "news"}
SOURCE_MIN_SHARED_TERMS = 3
# An article sharing under this share of the best article's terms only shares
# club and player names with the answer.
SOURCE_MIN_SHARE_OF_BEST = 0.5
# Capitalised words that say nothing about which article an answer drew on.
SOURCE_STOPWORDS = {
    "january", "february", "march", "april", "may", "june", "july", "august", "september",
    "october", "november", "december", "monday", "tuesday", "wednesday", "thursday", "friday",
    "saturday", "sunday", "premier", "league", "football", "club", "season", "article", "lasting",
    "true", "still", "the", "this", "that", "they", "their", "with", "from", "after", "while",
}


def _source_terms(text):
    """An article's or answer's names and figures: capitalised words and
    numbers, accent-folded - not years, months or section labels."""
    terms = set()
    for word in re.findall(r"\b(?:[A-ZÀ-Þ][\w'’-]{3,}|\d[\d.,]*)", text or ""):
        folded = " ".join(_folded_words(word))
        if folded and folded not in SOURCE_STOPWORDS and not re.fullmatch(r"(?:19|20)\d\d", folded):
            terms.add(folded)
    return terms


def run_of_play_sources(kb_context, question, answer, include_archive=True):
    """The Run of Play articles in an answer's context that the answer drew
    on - at least SOURCE_MIN_SHARED_TERMS of an article's names or figures,
    ones the question didn't already contain, appear in the answer - as
    [{"title", "url"}]. include_archive=False leaves out articles labeled ARCHIVE.
    Only Run of Play's own pages, never web results."""
    body = re.split(r"^[ \t]*(?:\*\*|__)?Ask next", answer or "", maxsplit=1, flags=re.M | re.I)[0]
    answer_terms = _source_terms(body)
    question_terms = _source_terms(question) | set(_folded_words(question))
    sources = []
    shared_counts = {}
    for block in RUN_OF_PLAY_BLOCK.split(kb_context or ""):
        title_line = re.search(r"^Article: (.+)$", block, re.M)
        if not title_line or any(source["title"] == title_line.group(1).strip() for source in sources):
            continue
        if block.startswith("[RUN OF PLAY ARCHIVE") and (not include_archive or RECENT_ARCHIVE_LABEL.match(block)):
            continue
        title = title_line.group(1).strip()
        # Without the age label and the facts' section headers.
        text = "\n".join(line for line in block.splitlines()[1:] if not line.rstrip().endswith(":"))
        url = article_url(title)
        shared = len((_source_terms(text) - question_terms) & answer_terms)
        if url and url.startswith(RUN_OF_PLAY_SITE) and shared >= SOURCE_MIN_SHARED_TERMS:
            sources.append({"title": title, "url": url})
            shared_counts[title] = shared
    best = max(shared_counts.values(), default=0)
    return [source for source in sources if shared_counts[source["title"]] >= best * SOURCE_MIN_SHARE_OF_BEST]


def _kb_context_ages(context):
    """Ages in days of the recent Run of Play articles in a context, from
    their "[RUN OF PLAY CONTEXT - published ..., N days ago]" labels."""
    return [
        0 if when == "today" else int(days)
        for when, days in re.findall(r"\[RUN OF PLAY CONTEXT - published [^,\]]*, (today|(\d+) days? ago)", context or "")
    ]


def judge_kb_coverage(question: str, search_query: str, context: str) -> str:
    """
    How much of the answer Run of Play's retrieved articles hold as of today -
    "full", "partial" or "none" - judged by gemini-2.5-flash-lite. The backend
    answers from them alone on "full", drafts from them and adds the web
    update on "partial", and on "none" skips the draft (which used to linger
    on stale material before the web update contradicted it) for one answer
    from the web search plus these articles. "partial" whenever the check
    fails, so the draft-then-update flow stays the default.
    """
    if not (context or "").strip():
        return "none"
    if _is_latest_news_question(question, search_query) and not any(
        age <= LATEST_NEWS_COVERAGE_MAX_AGE_DAYS for age in _kb_context_ages(context)
    ):
        # Nothing recent enough to hold the current answer, and flash-lite kept
        # rating such sets "partial" (a 55-day-old result for "what's next in
        # the title race"; a 20-day-old FPL tip and April's relegation fight
        # for "what's going on with nottingham") - a draft from them led with
        # old news before the web update.
        _log_query("kb_coverage", {"coverage": "none", "reason": f"latest-news question, no Run of Play article from the last {LATEST_NEWS_COVERAGE_MAX_AGE_DAYS} days"})
        return "none"
    if _is_fixture_question(question, search_query) and not any(
        age <= FIXTURE_MAX_AGE_DAYS for age in _kb_context_ages(context)
    ):
        # A preview from a week or two ago is about a match that's been
        # played, and could otherwise be judged to answer "next match" in full.
        _log_query("kb_coverage", {"coverage": "none", "reason": f"next-match question, no Run of Play article from the last {FIXTURE_MAX_AGE_DAYS} days"})
        return "none"
    prompt = build_relevance_check_prompt(question, search_query or question, context, date.today())
    try:
        response = call_gemini_with_key_retry(
            "gemini-2.5-flash-lite",
            lambda client: client.models.generate_content(
                model="gemini-2.5-flash-lite", contents=prompt, config=DECISION_CONFIG
            ),
        )
        verdict = _best_effort_json(response.text)
    except Exception as exc:
        print(f"Run of Play coverage check failed, assuming partial: {exc}")
        return "partial"
    coverage = str(verdict.get("coverage", "")).lower()
    _log_query("kb_coverage", {"coverage": coverage, "reason": verdict.get("reason")})
    return coverage if coverage in KB_COVERAGE else "partial"


def ask_from_llm(token,query, fallback_context=None, formatted_query = None, force_search=False, on_search_planned=None):
    """on_search_planned(formatted_query), if given, is called as soon as the
    classifier wants a search - before the Run of Play search - so the caller
    can start the web search alongside it."""
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
    if on_search_planned and search_required and fallback_context is None:
        on_search_planned(formatted_query)

    return build_news_context(
        primary_question,
        formatted_query,
        fallback_context,
        search_required,
    )

if __name__ == "__main__":
    print("start")
    print(ask_from_llm("","what should messi do in the next world cup?"))
