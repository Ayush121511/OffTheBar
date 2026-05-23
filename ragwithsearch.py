import os

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
from kb_google import search_pinecone
from rank_bm25 import BM25Okapi
from nltk.tokenize import word_tokenize
print("Loading DuckDuckGo search tools...")
print("DuckDuckGo search tools loaded.")
# from bs4 import BeautifulSoup
# import json
print("Loading NLTK and Sentence Transformers...")
from nltk.tokenize import sent_tokenize
print("NLTK loaded.")


def _require_env(name):
    value = os.getenv(name)
    if not value:
        raise ValueError(f"{name} environment variable is not set.")
    return value


def _jina_headers():
    return {
        "Authorization": f"Bearer {_require_env('JINA_API_KEY')}",
        "X-Engine": "direct",
        "X-Timeout": "10s"
    }

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

def rank_chunks_by_relevance(chunks, query, top_k=5):
    embeddings = model.encode([query] + chunks, convert_to_tensor=True)
    query_emb = embeddings[0]
    chunk_embs = embeddings[1:]
    scores = util.cos_sim(query_emb, chunk_embs)[0]
    print(len(scores), "scores length")
    top_indices = scores.topk(top_k if top_k<=len(scores) else len(scores)).indices
    return [chunks[i] for i in top_indices]

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

def rank_chunks_by_relevance_and_date(chunks, query, top_k=5):
    # Step 1: Rank chunks by semantic similarity
    embeddings = model.encode([query] + chunks, convert_to_tensor=True)
    query_emb = embeddings[0]
    chunk_embs = embeddings[1:]
    scores = util.cos_sim(query_emb, chunk_embs)[0]
    top_indices = scores.topk(top_k if top_k <= len(scores) else len(scores)).indices.tolist()

    # Step 2: Define date-related keywords to search for
    date_keywords = ['published', 'date', 'updated', 'posted', 'on', 'time', 'release']

    # Step 3: Identify chunks with a likely publication date
    date_chunks = [i for i, chunk in enumerate(chunks)
                  if any(word in chunk.lower() for word in date_keywords)
                  and any(char.isdigit() for char in chunk)]

    # Step 4: Merge top-ranked and date-containing chunks
    all_indices = list(set(top_indices + date_chunks))

    # Step 5: Return unique selected chunks
    return [chunks[i] for i in all_indices]



def get_top_bm25_contexts(page_contents, formatted_query, results, top_n=5):
    """Return top-N URLs and content ranked by BM25 relevance to the query."""
    query_tokens = word_tokenize(formatted_query.lower())

    # Tokenize documents
    tokenized_corpus = []
    url_to_text = []
    for result in results.get("results", []):
        url = result["url"]
        content = page_contents.get(url, " ")
        if content is None:
            content = " "
        tokenized_text = word_tokenize(content.lower())
        tokenized_corpus.append(tokenized_text)
        url_to_text.append((url, content, result["title"], result.get("snippet", "")))

    bm25 = BM25Okapi(tokenized_corpus)
    scores = bm25.get_scores(query_tokens)

    # Pair scores with URLs, titles, content
    scored_results = sorted(
        zip(scores, url_to_text),
        key=lambda x: x[0],
        reverse=True
    )

    # Return top-N
    top_contexts = []
    for score, (url, content, title, snippet) in scored_results[:top_n]:
        top_contexts.append({
            "url": url,
            "title": title,
            "snippet": snippet,
            "content": content
        })

    return top_contexts


def can_fetch(url, user_agent="*"):
    """Check robots.txt to see if scraping is allowed for the URL."""
    domain = "/".join(url.split("/")[:3])
    robots_url = f"{domain}/robots.txt"

    rp = urllib.robotparser.RobotFileParser()
    rp.set_url(robots_url)

    try:
        # Manually fetch robots.txt with timeout
        response = requests.get(robots_url, timeout=2)
        response.raise_for_status()
        rp.parse(response.text.splitlines())  # Manually parse the content
        return rp.can_fetch(user_agent, url)
    except Exception as e:
        print(f"Error accessing robots.txt for {domain}: {e}")
        return False  # Assume disallowed if robots.txt cannot be accessed

def extract_publication_date(soup):
    """Extract the publication date of the webpage."""
    # Check for meta tags with publication date
    date_meta_tags = [
        {'name': 'article:published_time'},
        {'property': 'article:published_time'},
        {'name': 'date'},
        {'property': 'og:updated_time'}
    ]

    for tag in date_meta_tags:
        meta_date = soup.find('meta', tag)
        if meta_date and 'content' in meta_date.attrs:
            return meta_date['content']

    script_tag = soup.find('script', type='application/ld+json')
    if script_tag:
        try:
            data = json.loads(script_tag.string)
            if isinstance(data, dict) and 'datePublished' in data:
                return data['datePublished']
        except json.JSONDecodeError:
            pass

    # Check for visible <time> tags
    time_tag = soup.find('time')
    if time_tag:
        return time_tag.get_text(strip=True)

    return "Publication date not found."

def is_relevant_line(line):
    """Determine if a line is relevant content."""
    irrelevant_keywords = [
        "Home", "News", "Photos", "Videos", "Reviews", "Press Release",
        "Box Office", "More", "Trends", "Movie Schedule", "World", "Life Style",
        "Shorts", "Interviews", "Paparazzi", "Stories"
    ]
    if len(line) < 5:
        return False
    if any(keyword.lower() in line.lower() for keyword in irrelevant_keywords):
        return False
    return True

def extract_relevant_paragraphs(soup, keywords, proximity=1):
    """Extract paragraphs close to specific keywords."""
    paragraphs = soup.find_all('p')  # Find all paragraph tags
    relevant_paragraphs = []

    for i, para in enumerate(paragraphs):
        text = para.get_text(strip=True)
        if any(keyword.lower() in text.lower() for keyword in keywords):
            start = max(0, i - proximity)
            end = min(len(paragraphs), i + proximity + 1)
            relevant_paragraphs.extend(paragraphs[start:end])

    unique_paragraphs = list({para.get_text(strip=True) for para in relevant_paragraphs})
    return unique_paragraphs

def extract_main_content_with_keywords(url, keywords, proximity=1):
    """Fetch and parse the main text content of a page with keyword filtering."""
    try:
        response = requests.get(url, timeout=2)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')

        # Extract publication date
        pub_date = extract_publication_date(soup)
        # Extract relevant paragraphs based on keywords
        relevant_paragraphs = extract_relevant_paragraphs(soup, keywords, proximity)

        # Filter out repetitive or irrelevant lines
        filtered_paragraphs = [
            para for para in relevant_paragraphs if is_relevant_line(para)
        ]

        return f"Publication date: {pub_date}\n" + ('\n'.join(filtered_paragraphs) if filtered_paragraphs else "No relevant content found.")
    except Exception as e:
        return f"Error scraping the page: {e}"

def scrape_page(url, keywords, user_agent="*", proximity=1):
    print(f"Scraping URL: {url}")
    """Check robots.txt and scrape keyword-specific content if allowed."""
    if can_fetch(url, user_agent):
        print(f"Scraping allowed for: {url}")
        content = extract_main_content_with_keywords(url, keywords, proximity)
        return content
    else:
        print(f"Scraping disallowed by robots.txt: {url}")

def html_to_text(html):
    soup = BeautifulSoup(html, "html.parser")

    # Remove script/style
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    # Extract visible text
    return soup.get_text(separator="\n", strip=True)



def get_page_metadata(url):
    try:
        response = requests.get(url, timeout=3, headers={"User-Agent": "Mozilla/5.0"})
        soup = BeautifulSoup(response.text, 'html.parser')
        title = soup.find("meta", property="og:title") or soup.title
        desc = soup.find("meta", property="og:description") or soup.find("meta", attrs={"name": "description"})
        return {
            "title": title["content"] if title else None,
            "description": desc["content"] if desc else None
        }
    except Exception as e:
        return {"error": str(e)}

def get_wayback_snapshot(url):
    headers = {"User-Agent": "TheRunOfPlay/1.0 (+mailto:therunofplay10@gmail.com)"}

    res = requests.get("http://archive.org/wayback/available", params={"url": url},headers=headers)
    data = res.json()
    return data.get("archived_snapshots", {}).get("closest", {}).get("url")


def get_archived_image_url(image_url):

    headers = {
"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/113.0.0.0 Safari/537.36"
    }

    # 1. Wayback Machine
    try:
        res = requests.get("http://archive.org/wayback/available", params={"url": image_url}, headers=headers, timeout=15)
        data = res.json()
        wb_url = data.get("archived_snapshots", {}).get("closest", {}).get("url")
        if wb_url:
            return {"source": "Wayback Machine", "url": wb_url}
    except Exception as e:
        print(f"[Wayback] Error: {e}")
    try:
        archive_today_url = f"https://archive.today/?run=1&url={quote(image_url)}"
        res = get_archive_today_snapshot_with_selenium(archive_today_url, wait_seconds=0.5)
        return {"source": "Archive.today", "url": archive_today_url, "html": res.get("html")}
    except Exception as e:
        print(f"[Archive.today] Error: {e}")

    try:
        google_cache_url = f"https://webcache.googleusercontent.com/search?q=cache:{quote(image_url)}"
        res = requests.head(google_cache_url, headers=headers, timeout=10)
        if res.status_code == 200:
            return {"source": "Google Cache", "url": google_cache_url}
    except Exception as e:
        print(f"[Google Cache] Error: {e}")

    # Fallback
    return {"source": None, "url": None}


def get_rss_summary(feed_url):
    d = feedparser.parse(feed_url)
    return [(entry.title, entry.link, entry.summary) for entry in d.entries]

def scrape_wayback_page(snapshot_url):
    if snapshot_url["source"] == "Archive.today":
        text = html_to_text(snapshot_url["html"])
        print(text)
        return text

    try:
        headers = _jina_headers()
        print("Fetching Wayback snapshot from:", snapshot_url)
        if not snapshot_url:
            return "[Error] No Wayback snapshot URL found."
        res = requests.get("https://r.jina.ai/"+snapshot_url['url'], headers=headers, timeout=10)
        print(res.text)
        return res.text

    except Exception as e:
        return f"[Error] {e}"



def is_wayback_archived(url):
    """
    Returns 'yes' if the URL has a Wayback Machine snapshot,
    otherwise returns 'no'.
    """
    try:
        res = requests.get("http://archive.org/wayback/available", params={"url": url}, timeout=15)
        data = res.json()
        return 1 if "closest" in data.get("archived_snapshots", {}) else 0
    except:
        return 0

def scrape_jinaAI(url):
    try:
        headers = _jina_headers()
        res = requests.get("https://r.jina.ai/"+url, headers=headers, timeout=5)
        print(res.text)
        return res.text
    except Exception as e:
        print(f"Error fetching {url}: {e}")
        return None

def get_wayback_snapshot_and_content(url):

    try:
        headers = _jina_headers()

        # Step 1: Check snapshot availability
        res = requests.get("http://archive.org/wayback/available", params={"url": url}, timeout=15)
        data = res.json()
        print(data)
        snapshot = data.get("archived_snapshots", {}).get("closest", {})

        if not snapshot.get("url"):
            return None

        snapshot_url = snapshot["url"]
        print(f"Wayback snapshot URL: {snapshot_url}")

        # Step 2: Download the snapshot content
        res = requests.get("https://r.jina.ai/"+snapshot_url, headers=headers, timeout=10)
        print(res.text)
        return res.text

    except Exception as e:
        print(f"Error fetching {url}: {e}")
        return None


from datetime import date
from datetime import datetime


def bm25_score(query, document):
    query_tokens = word_tokenize(query.lower())
    doc_tokens = [word_tokenize(document.lower())]
    bm25 = BM25Okapi(doc_tokens)
    return bm25.get_scores(query_tokens)[0]


today = date.today()
from crawl4AI import crawl_page

def formatted_prompting(query,formatted_query, player_query, team_query, fallback_context = None,search_required=None):
    context = False

    print("Formatted query:", formatted_query)


    pinecone_contexts = []


    if fallback_context is None:
        # Query Pinecone index for top 3 matches
        matches = search_pinecone(formatted_query, top_k=4)
        pinecone_fallback = ""
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
                        pinecone_contexts.append(
                            f"[RUN OF PLAY CONTEXT - Historical]\n{md.get('created', 'Unknown Date')}\n\n{md.get('chunk', 'No content')}\n"
                        )

                fallback_context = "\n\n".join(pinecone_contexts)
                return fallback_context, (True and search_required), formatted_query

    url = "https://asia-south1-runofplay.cloudfunctions.net/searx_proxy"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
    }
    search_results = []
    print("Searching for news context...")
    print("Formatted query:", formatted_query)
    try:
        params = {
            "q": formatted_query,
            "format": "json",
            "language": "en",
            "safesearch": 1
        }

        response = requests.get(url, headers=headers, params=params)
        response.raise_for_status()

        results = response.json()
        # search_results = search.invoke(formatted_query)
        context = True
    except Exception as e:
        print(f"Error: {e}")
        # return random.choice(error_messages)
        pass
    print("Search results:", len(results.get("results", [])))
    page_contents = {}
    for r_now in results.get("results", [])[:10]:
        # page_contents[r_now[url]] = r_now.get("snippet", []) + scrape_page(url, keywords)
        page_contents[r_now["url"]] = r_now.get("title", " ") + r_now.get("content", " ") +  r_now.get("snippet", " ")
    print("Page contents:", len(page_contents))
    prompt_begin = f" These are the news context for the following query - {query}.\n\n"

    prompt_context = []
    prompt_context_short = []

    prompt_context = []
    for ctx in results.get("results", []):
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
        # wayback_chunks = chunk_article(wayback_text)
        print(f"Page chunks for {ctx['url']}: {len(page_chunks)}")
        top_chunks = rank_chunks_bm25(page_chunks, query, top_k=5)
        print(f"Top chunks for {ctx['url']}: {len(top_chunks)}")
        # print(top_chunks[0])
        top_chunks_str = '\n\n'.join(top_chunks)
        prompt_context.append(f"NEWS - {ctx['title']} :  {ctx['url']}\nPage content: {page_contents[ctx['url']]} \n\n{page_metadata} {top_chunks_str}\n")

    print(len(prompt_context))
    # return
    prompt_context = "\n\n".join(prompt_context)



    formatted_prompt_short = prompt_begin + "Recent news:\n\n" + prompt_context

    # formatted_prompt_short += "\n\n" + fallback_context

    return formatted_prompt_short



import json
import re
from datetime import date

from gemini_utils import build_gemini_model
from prompt_templates import build_search_classifier_prompt


def _get_gemini_model():
    return build_gemini_model("gemini-2.5-flash-lite")


def _heuristic_query_classifier(user_question: str) -> dict:
    question = user_question.strip()
    lowered = question.lower()

    fresh_markers = [
        "latest", "right now", "today", "currently", "recent", "recently",
        "this season", "last match", "next match", "fixture", "fixtures",
        "injury", "injuries", "transfer", "transfers", "rumour", "rumor",
        "news", "update", "updates", "table", "standings", "form",
        "score", "result", "results", "live", "quote", "quotes",
        "contract", "renewal",
    ]
    evergreen_markers = [
        "favourite", "favorite", "best ever", "greatest ever", "of all time",
        "who is better", "who was better", "legacy", "history of",
        "historical", "all-time", "all time", "why was", "why is",
        "what is the offside rule", "size of a football pitch",
    ]

    if any(marker in lowered for marker in fresh_markers):
        normalized = re.sub(r"[^a-z0-9\s-]", " ", question.lower())
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
    user_question = _extract_primary_question(user_question)
    today = date.today()
    heuristic_result = _heuristic_query_classifier(user_question)

    if not heuristic_result["search_required"]:
        return heuristic_result

    prompt = build_search_classifier_prompt(user_question, today)
    try:
        response = _get_gemini_model().generate_content(prompt)
        return _best_effort_json(response.text)
    except Exception as exc:
        print(exc)
        print("Gemini query classification failed, falling back to heuristic classification.")
        return heuristic_result

def ask_from_llm(token,query, fallback_context=None, formatted_query = None):
    print("Hello query")
    print("query loading")
    search_required = False
    primary_question = _extract_primary_question(query)

    if not primary_question:
        safe_fallback = fallback_context or ""
        return safe_fallback, False, ""

    if formatted_query is None:
        result = classify_and_build_query(primary_question)
        print(result)
        formatted_query = result["query"].strip()

        search_required = search_required or result["search_required"]


    print("Formatted Query:", formatted_query)

    player_query = "player query"

    team_query = "team query"

    formatted_prompt = formatted_prompting(
        primary_question,
        formatted_query,
        player_query,
        team_query,
        fallback_context,
        search_required,
    )

    if isinstance(formatted_prompt, tuple):
        return formatted_prompt

    return formatted_prompt, search_required, formatted_query

if __name__ == "__main__":
    print("start")
    print(ask_from_llm("","what should messi do in the next world cup?"))
