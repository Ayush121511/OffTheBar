"""
Evaluate the Off The Bar pipeline against evals/cases.json and build the dashboard.

    python evals/run_evals.py                  # routing sections only - flash-lite calls, well under a cent
    python evals/run_evals.py --answers        # + real answers for the "core" cases (~Rs 2.5, less on free quota)
    python evals/run_evals.py --all-answers    # + real answers for every "answer" case, before a deploy (~Rs 5)
    python evals/run_evals.py --only clasico-result,false-nine --answers
    python evals/run_evals.py --domain latest-news

Every case belongs to a domain (cases.json "domains"); every check belongs to a
section (evals/scoring.py):

  understanding  search decision (1 flash-lite call), search query, latest-news detection,
                 the query sent to Bing News (web_query_includes)
  retrieval      Run of Play articles from the hybrid search (free); for cases with
                 web_* expectations, the web search as Phase 2 runs it (1 flash-lite
                 call) must hold no American football
  routing        coverage verdict (1 flash-lite call) and the answer path it picks
  accuracy       --answers: expected facts present, stale/contradicting claims absent
  quality        --answers: rubric from a flash-lite judge - answers the question,
                 voice, clarity, consistency with the chat
  safety         --answers: no leaked internals or source jargon, the Ask next block
  performance    --answers: time to first words within the path's budget

Answers come from the real /api/chat through the Flask app (gemini-2.5-flash,
1-3 calls each). Every Gemini call's tokens are read from the [OffTheBar Gemini]
log lines and priced (paid-key calls only), so each run reports its cost. Each
run writes a JSON report to evals/reports/ and rebuilds
evals/reports/dashboard.html from all reports.
"""
import argparse
import contextlib
import io
import json
import os
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
os.chdir(REPO)
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "evals"))

with contextlib.redirect_stdout(io.StringIO()):
    import config_env  # noqa: F401  (loads .env)
    import kb_search
    import ragwithsearch
    from gemini_utils import call_gemini_with_key_retry
    from server.backend import _build_llm_query

import dashboard
import scoring
import server.backend as backend

REPORTS = REPO / "evals" / "reports"

# List prices per million tokens (input, output incl. thinking), USD. Only
# calls made on a paid key are counted. Check Google's pricing page if these move.
PRICES = {
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
}
USAGE_LINE = re.compile(
    r"\[OffTheBar Gemini\] (\S+) via key #\S+ \((free|paid)\): input (\d+), output (\d+), thinking (\d+) tokens"
)
HALFTIME_LINE = re.compile(r"^##\s*HALFTIME!!!.*$", re.M)
SOURCES_LINE = re.compile(r"^##\s*SOURCES!!!\s*(.*)$", re.M)
RUN_OF_PLAY_SITE = "https://www.therunofplay.com/"
FOLLOW_UPS = re.compile(r"^[ \t]*(?:\*\*|__)?Ask next:?(?:\*\*|__)?[ \t]*$", re.I | re.M)
FOLLOW_UP_ITEM = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+\S")
# Things a user must never see (case-sensitive: "halftime" is ordinary football).
LEAKS = ["NO_NEW_UPDATES", "NO_RELEVANT_CONTEXT", "[RUN OF PLAY", "RUN OF PLAY ARCHIVE", "Reference material", "HALFTIME!!!"]
# Talk about its sources the persona shouldn't do ("Argentina dispatch Benin" is fine).
JARGON = re.compile(r"\bdispatches\b|\b(?:our|latest|recent) dispatch\b|\bdispatch from\b|\breference material\b|"
                    r"\bcontext (?:given|provided)\b|\bthe wires\b|\bour desk\b", re.I)
# Association football only - no NFL, ever, even as a contrast. Wider than the
# pipeline's own filter (ragwithsearch.AMERICAN_FOOTBALL), so it can catch what
# that misses: adds NFL team names that aren't also soccer nicknames (not
# Eagles, Saints, Lions, Rams...).
AMERICAN_FOOTBALL = re.compile(
    r"\b(?:nfl|ncaa|quarterbacks?|qbs?|touchdowns?|super bowl|gridiron|american football|college football|"
    r"other kind of football|high school football|flag football|tight ends?|wide receivers?|running backs?|"
    r"linebackers?|end zone|heisman|(?:thursday|sunday|monday) night football|start[/ -]sit|waiver wire|"
    r"chiefs|cowboys|49ers|packers|steelers|ravens|broncos|patriots|vikings|seahawks|commanders|jaguars|"
    r"buccaneers|bengals|texans|colts|raiders|falcons)\b",
    re.I,
)
SEARX_QUERY_LINE = re.compile(r"\[OffTheBar query\] searx_query: (['\"])(.*)\1$", re.M)
# Seconds to the first words, per answer path. Full marks within budget,
# falling to 0 at twice the budget.
LATENCY_BUDGET = {"single": 15, "draft_update": 8, "combined": 25}
RUBRIC = ["answers_question", "voice", "clarity", "consistency"]


def usage_from(log_text):
    calls = []
    for model, tier, inp, out, think in USAGE_LINE.findall(log_text):
        inp, out, think = int(inp), int(out), int(think)
        price_in, price_out = PRICES.get(model, (0.0, 0.0))
        cost = (inp * price_in + (out + think) * price_out) / 1e6 if tier == "paid" else 0.0
        calls.append({"model": model, "tier": tier, "input": inp, "output": out, "thinking": think, "cost": cost})
    return calls


def check(checks, section, name, ok, detail="", score=None):
    checks.append({"check": name, "section": section, "ok": bool(ok),
                   "score": float(bool(ok)) if score is None else round(score, 3), "detail": detail})


def contains(text, needle):
    return needle.lower() in text.lower()


class Reused:
    """Wraps a decision call (classifier, coverage check) so the routing stage
    reuses what the same case's answer stage just paid for. They run at
    temperature 0, so a second call would say the same. Answers go first so
    their timing stays real; anything not seen yet is called as usual."""

    reuse = False

    def __init__(self, call):
        self.call, self.seen = call, {}

    def __call__(self, *args):
        if not (Reused.reuse and args in self.seen):
            self.seen[args] = self.call(*args)
        return self.seen[args]


ragwithsearch.classify_and_build_query = Reused(ragwithsearch.classify_and_build_query)
ragwithsearch.judge_kb_coverage = backend.judge_kb_coverage = Reused(ragwithsearch.judge_kb_coverage)


def route_case(case):
    """Understanding, retrieval and routing. Returns (checks, info, log)."""
    expect = case.get("expect", {})
    question = case["question"]
    checks, info = [], {}
    log = io.StringIO()
    llm_query = _build_llm_query(case.get("history", []), question)
    with contextlib.redirect_stdout(log):
        context, search_required, formatted_query = ragwithsearch.ask_from_llm("", llm_query)
        latest = ragwithsearch._is_latest_news_question(question, formatted_query)
        coverage = ragwithsearch.judge_kb_coverage(question, formatted_query, context) if search_required else None
    titles = re.findall(r"^Article: (.+)$", context, re.M)
    # Same choice as server/backend.py (minus Phase 1's own NO_RELEVANT_CONTEXT
    # backup, which only the answer stage can see).
    path = "single" if not search_required or coverage == "full" else ("combined" if coverage == "none" else "draft_update")
    info.update(search_required=search_required, query=formatted_query, latest_news=latest,
                titles=titles, coverage=coverage, path=path)

    if "search_required" in expect:
        check(checks, "understanding", "classify", search_required == expect["search_required"],
              f"got {search_required}, query {formatted_query!r}")
    for needle in expect.get("query_includes", []):
        check(checks, "understanding", "query", contains(formatted_query, needle),
              f"{needle!r} missing from {formatted_query!r}")
    for needle in expect.get("query_excludes", []):
        check(checks, "understanding", "query", not contains(formatted_query, needle),
              f"{needle!r} in {formatted_query!r}")
    if "latest_news" in expect:
        check(checks, "understanding", "latest", latest == expect["latest_news"], f"got {latest}")
    if "kb_title_any" in expect:
        hit = any(contains(t, n) for t in titles for n in expect["kb_title_any"])
        check(checks, "retrieval", "retrieve", hit, f"top titles: {[t[:50] for t in titles]}")
    if "coverage_in" in expect and coverage is not None:
        check(checks, "routing", "coverage", coverage in expect["coverage_in"], f"got {coverage}")
    if "path_in" in expect:
        check(checks, "routing", "path", path in expect["path_in"], f"got {path}")

    # The web search, exactly as Phase 2 runs it (1 flash-lite call for the
    # soccer check). Run whenever a case has web expectations, even if the
    # classifier wanted no search, so the filtering itself is always tested.
    if any(key.startswith("web_") for key in expect):
        web_log = io.StringIO()
        with contextlib.redirect_stdout(web_log):
            web_context, _, _ = ragwithsearch.ask_from_llm("", llm_query, "", formatted_query or question, force_search=True)
        log.write(web_log.getvalue())
        sent = SEARX_QUERY_LINE.findall(web_log.getvalue())
        web_query = sent[-1][1] if sent else ""
        web_titles = re.findall(r"^NEWS[^\n]*? - (.+?) :  ", web_context, re.M)
        info.update(web_query=web_query, web_titles=web_titles)
        for needle in expect.get("web_query_includes", []):
            check(checks, "understanding", "web query", contains(web_query, needle),
                  f"{needle!r} missing from {web_query!r}")
        if expect.get("web_soccer_only"):
            found = sorted(set(m.lower() for m in AMERICAN_FOOTBALL.findall(web_context)))
            check(checks, "retrieval", "web: soccer only", not found,
                  f"found {found}" if found else f"{len(web_titles)} results, none American football")
    return checks, info, log.getvalue()


def rubric(case, answer):
    """flash-lite judge, temperature 0. Returns ({criterion: 1-5}, note)."""
    history = "\n".join(f"{t['role']}: {t['content']}" for t in case.get("history", [])) or "(none)"
    expect = case.get("expect", {})
    facts = ", ".join(expect.get("answer_contains_all", []) + expect.get("answer_contains_any", [])[:1]
                      + expect.get("facts", [])) or "(none given)"
    prompt = f"""You are grading a football chatbot's answer. Today is {date.today()}. The chatbot reads news from after your training data, so treat every name, score, date and event in the answer as true, even ones you have never heard of - you are grading how well it answers, not whether the facts are right.

Question: {case['question']}

Earlier conversation:
{history}

Confirmed facts for this question (true, whatever you remember): {facts}

Answer:
\"\"\"
{answer}
\"\"\"

Rate each from 1 (poor) to 5 (excellent):
- answers_question: does it address what was asked, not a neighbouring question or an excuse for not answering?
- voice: does it sound like a lively football pundit - a real metaphor or simile and a line of dry wit woven into the facts - rather than a flat summary?
- clarity: is it easy to read, well organised, free of padding?
- consistency: does it agree with the earlier conversation? (5 if there is none)

Reply with JSON only: {{"answers_question": n, "voice": n, "clarity": n, "consistency": n, "note": "<one short sentence>"}}"""
    response = call_gemini_with_key_retry(
        "gemini-2.5-flash-lite",
        lambda client: client.models.generate_content(
            model="gemini-2.5-flash-lite", contents=prompt, config=ragwithsearch.DECISION_CONFIG
        ),
    )
    verdict = ragwithsearch._best_effort_json(response.text)
    ratings = {}
    for key in RUBRIC:
        try:
            ratings[key] = max(1, min(5, int(verdict.get(key))))
        except (TypeError, ValueError):
            pass
    return ratings, verdict.get("note", "")


_app = None


def answer_case(case):
    """A real /api/chat answer, checked. Returns (checks, info, log)."""
    global _app
    if _app is None:
        with contextlib.redirect_stdout(io.StringIO()):
            from run import app
        _app = app
    expect = case.get("expect", {})
    payload = {
        "jailbreak": True,
        "conversation_id": "eval-run",
        "stage_statuses": True,
        "sources": True,
        "meta": {"content": {"conversation": case.get("history", []), "parts": [{"content": case["question"]}]}},
    }
    log, raw = io.StringIO(), ""
    with contextlib.redirect_stdout(log):
        start = time.time()
        response = _app.test_client().post("/api/chat", json=payload, buffered=False)
        first_words = None
        for chunk in response.response:
            raw += chunk.decode("utf-8", "replace") if isinstance(chunk, bytes) else chunk
            if first_words is None and HALFTIME_LINE.sub("", raw).strip():
                first_words = time.time() - start
        total = time.time() - start
        response.close()
    sources_line = SOURCES_LINE.search(raw)
    sources = json.loads(sources_line.group(1)) if sources_line else []
    text = re.sub(r"\n{3,}", "\n\n", SOURCES_LINE.sub("", HALFTIME_LINE.sub("", raw))).strip()
    # Every answer streams status lines now, so the path comes from the log.
    if "doesn't answer the question" in log.getvalue() or "flagged nothing relevant" in log.getvalue():
        path = "combined"
    elif "=== Phase 1 starting ===" in log.getvalue():
        path = "draft_update"
    else:
        path = "single"

    checks = []
    check(checks, "safety", "answer:status", response.status_code == 200 and text, f"HTTP {response.status_code}, {len(text)} chars")
    if "path_in" in expect:
        check(checks, "routing", "answer:path", path in expect["path_in"], f"got {path}")
    for needle in expect.get("answer_contains_all", []):
        check(checks, "accuracy", f"answer:has {needle!r}", contains(text, needle))
    if expect.get("answer_contains_any"):
        check(checks, "accuracy", "answer:has any", any(contains(text, n) for n in expect["answer_contains_any"]),
              f"any of {expect['answer_contains_any']}")
    for needle in expect.get("answer_excludes", []):
        check(checks, "accuracy", f"answer:no {needle!r}", not contains(text, needle))
    leaked = [s for s in LEAKS if s in text]
    check(checks, "safety", "answer:no leaked internals", not leaked, f"found {leaked}" if leaked else "")
    jargon = sorted(set(m.lower() for m in JARGON.findall(text)))
    check(checks, "safety", "answer:no source jargon", not jargon, f"found {jargon}" if jargon else "")
    other_sport = sorted(set(m.lower() for m in AMERICAN_FOOTBALL.findall(text)))
    check(checks, "safety", "answer:no american football", not other_sport, f"found {other_sport}" if other_sport else "")
    # Every answer ends with suggestions - including a draft the update couldn't add to.
    block = FOLLOW_UPS.split(text)
    items = [l for l in block[-1].splitlines() if FOLLOW_UP_ITEM.match(l)] if len(block) > 1 else []
    check(checks, "safety", "answer:ask-next block", 2 <= len(items) <= 3, f"{len(items)} items")
    off_site = [s["url"] for s in sources if not s.get("url", "").startswith(RUN_OF_PLAY_SITE)]
    check(checks, "safety", "answer:sources are Run of Play", not off_site, f"found {off_site}" if off_site else f"{len(sources)} links")
    if first_words is not None:
        budget = LATENCY_BUDGET[path]
        check(checks, "performance", "answer:first words", first_words <= budget,
              f"{first_words:.1f}s, budget {budget}s ({path})", score=max(0.0, min(1.0, 2 - first_words / budget)))

    note = ""
    if text:
        with contextlib.redirect_stdout(log):
            try:
                ratings, note = rubric(case, text)
            except Exception as exc:
                ratings, note = {}, f"judge failed: {exc}"
        for key, rating in ratings.items():
            if key == "consistency" and not case.get("history"):
                continue
            check(checks, "quality", f"rubric:{key}", rating >= 4, f"{rating}/5", score=(rating - 1) / 4)

    info = {"path": path, "first_words_s": round(first_words, 1) if first_words else None,
            "total_s": round(total, 1), "answer": text, "judge_note": note, "sources": sources}
    return checks, info, log.getvalue()


def pct(value):
    return "  -  " if value is None else f"{value * 100:5.1f}"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--answers", action="store_true", help="also generate answers for the \"core\" cases")
    parser.add_argument("--all-answers", action="store_true", help="also generate answers for every \"answer\" case")
    parser.add_argument("--only", help="comma-separated case ids")
    parser.add_argument("--domain", help="comma-separated domains")
    parser.add_argument("--cases", default=str(REPO / "evals" / "cases.json"))
    args = parser.parse_args()

    spec = json.loads(Path(args.cases).read_text())
    cases = spec["cases"]
    unknown = {c["domain"] for c in cases} - set(spec["domains"])
    if unknown:
        sys.exit(f"cases.json: domains not listed under 'domains': {sorted(unknown)}")
    if args.only:
        wanted = set(args.only.split(","))
        if missing := wanted - {c["id"] for c in cases}:
            sys.exit(f"--only: no such case ids: {sorted(missing)}")
        cases = [c for c in cases if c["id"] in wanted]
    if args.domain:
        wanted = set(args.domain.split(","))
        if missing := wanted - set(spec["domains"]):
            sys.exit(f"--domain: no such domains: {sorted(missing)}")
        cases = [c for c in cases if c["domain"] in wanted]

    print(f"Loading the Run of Play keyword index... ({len(cases)} cases, as of {spec.get('as_of')})")
    with contextlib.redirect_stdout(io.StringIO()):
        kb_search._refresh()
    kb_search._refreshing = True  # one snapshot for the whole run

    results, all_calls, started = [], [], time.time()
    for case in cases:
        answer_result = None
        if case.get("answer") and (args.all_answers or (args.answers and (args.only or case.get("core")))):
            Reused.reuse = False
            answer_result = answer_case(case)
            Reused.reuse = True
        checks, info, log_text = route_case(case)
        calls = usage_from(log_text)
        if answer_result:
            answer_checks, answer_info, answer_log = answer_result
            checks += answer_checks
            info["answer"] = answer_info
            calls += usage_from(answer_log)
        all_calls += calls
        failed = [c for c in checks if not c["ok"]]
        print(f"[{'PASS' if not failed else 'FAIL'}] {case['domain']:13} {case['id']}")
        for c in failed:
            print(f"       x {c['section']}/{c['check']}: {c['detail']}")
        results.append({"id": case["id"], "domain": case["domain"], "question": case["question"],
                        "checks": checks, "info": info, "cost": sum(c["cost"] for c in calls)})

    scores = scoring.score_run(results, {c["id"]: c["domain"] for c in spec["cases"]})
    overall = scores["all"]
    cost = sum(c["cost"] for c in all_calls)
    by_tier = {tier: sum(1 for c in all_calls if c["tier"] == tier) for tier in ("free", "paid")}

    print("\n" + "=" * 78)
    print(f"Overall score {pct(overall['overall'])} | cases {overall['cases_passed']}/{overall['cases']} | "
          f"checks {overall['checks_passed']}/{overall['checks']}")
    print("\nSection          score  checks")
    for sid, label, weight, _ in scoring.SECTIONS:
        s = overall["sections"].get(sid)
        print(f"  {label:15} {pct(s and s['score'])}  {s['checks'] if s else '-':>5}   (weight {weight})")
    print("\nDomain           score  cases")
    for domain, d in scores["domains"].items():
        print(f"  {domain:15} {pct(d['overall'])}  {d['cases_passed']}/{d['cases']}")
    print(f"\nGemini calls: {len(all_calls)} ({by_tier['free']} free key, {by_tier['paid']} paid key) - "
          f"estimated cost ${cost:.4f} (₹{cost * dashboard.USD_TO_INR:.2f}); {time.time() - started:.0f}s")

    REPORTS.mkdir(exist_ok=True)
    answers = args.answers or args.all_answers
    out = REPORTS / f"{datetime.now():%Y%m%d-%H%M%S}{'-answers' if answers else ''}.json"
    out.write_text(json.dumps({
        "as_of": spec.get("as_of"), "ran_at": datetime.now().isoformat(timespec="seconds"),
        "answers": answers, "core": bool(args.answers and not args.all_answers), "filtered": bool(args.only or args.domain),
        "cost_usd": cost, "duration_s": round(time.time() - started), "calls": all_calls,
        "scores": scores, "results": results,
    }, indent=2, ensure_ascii=False))
    page = dashboard.build(REPORTS, Path(args.cases))
    print(f"Report: {out.relative_to(REPO)}\nDashboard: {page.relative_to(REPO)}")
    return 0 if overall["cases_passed"] == overall["cases"] else 1


if __name__ == "__main__":
    sys.exit(main())
