"""
Evaluate the Off The Bar pipeline against evals/cases.json.

    python evals/run_evals.py                  # routing stages only - flash-lite on the free key, ~$0
    python evals/run_evals.py --answers        # also generate the answers of cases marked "answer": true
    python evals/run_evals.py --only clasico-result,world-cup-2010 --answers

Routing stages, per case:
  classify  - does the question need a web search (classify_and_build_query, 1 flash-lite call)
  query     - the web search query it writes keeps the question's names and no invented years
  latest    - is it a latest-news question (_is_latest_news_question, free)
  retrieve  - which Run of Play articles the hybrid search returns (local + Pinecone, free)
  coverage  - full / partial / none for those articles (judge_kb_coverage, 1 flash-lite call)
  path      - single answer / draft + update / combined, derived from the above
Answer stage (--answers): a real /api/chat answer through the Flask app, checked for
the expected facts, stale phrases, leaked internals and the "Ask next:" block. It
uses gemini-2.5-flash (1-3 calls per case), billed on the paid key once the free
key's quota is used up.

Every Gemini call's tokens are parsed from the [OffTheBar Gemini] log lines and
priced (paid-key calls only), so each run reports what it cost. A JSON report is
written to evals/reports/.
"""
import argparse
import contextlib
import io
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
os.chdir(REPO)
sys.path.insert(0, str(REPO))

with contextlib.redirect_stdout(io.StringIO()):
    import config_env  # noqa: F401  (loads .env)
    import kb_search
    import ragwithsearch
    from server.backend import _build_llm_query

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
FOLLOW_UPS = re.compile(r"^[ \t]*(?:\*\*|__)?Ask next:?(?:\*\*|__)?[ \t]*$", re.I | re.M)
FOLLOW_UP_ITEM = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+\S")
# Things a user must never see (case-sensitive: "halftime" is ordinary football).
LEAKS = ["NO_NEW_UPDATES", "NO_RELEVANT_CONTEXT", "[RUN OF PLAY", "RUN OF PLAY ARCHIVE", "Reference material", "HALFTIME!!!"]
# Internal words the persona shouldn't use about its sources ("dispatched a penalty" is fine).
JARGON = re.compile(r"\bdispatch(?:es)?\b|\breference material\b|\bcontext (?:given|provided)\b", re.I)


def usage_from(log_text):
    calls = []
    for model, tier, inp, out, think in USAGE_LINE.findall(log_text):
        inp, out, think = int(inp), int(out), int(think)
        price_in, price_out = PRICES.get(model, (0.0, 0.0))
        cost = (inp * price_in + (out + think) * price_out) / 1e6 if tier == "paid" else 0.0
        calls.append({"model": model, "tier": tier, "input": inp, "output": out, "thinking": think, "cost": cost})
    return calls


def check(results, name, ok, detail=""):
    results.append({"check": name, "ok": bool(ok), "detail": detail})


def contains(text, needle):
    return needle.lower() in text.lower()


def route_case(case):
    """Routing stages. Returns (checks, info, log)."""
    expect = case.get("expect", {})
    history = case.get("history", [])
    question = case["question"]
    checks, info = [], {}
    log = io.StringIO()
    with contextlib.redirect_stdout(log):
        context, search_required, formatted_query = ragwithsearch.ask_from_llm("", _build_llm_query(history, question))
        latest = ragwithsearch._is_latest_news_question(question, formatted_query)
        coverage = ragwithsearch.judge_kb_coverage(question, formatted_query, context) if search_required else None
    titles = re.findall(r"^Article: (.+)$", context, re.M)
    # Same choice as server/backend.py (minus Phase 1's own NO_RELEVANT_CONTEXT
    # backup, which only the answer stage can see).
    path = "single" if not search_required or coverage == "full" else ("combined" if coverage == "none" else "draft_update")
    info.update(search_required=search_required, query=formatted_query, latest_news=latest,
                titles=titles, coverage=coverage, path=path)

    if "search_required" in expect:
        check(checks, "classify", search_required == expect["search_required"],
              f"got {search_required}, query {formatted_query!r}")
    for needle in expect.get("query_includes", []):
        check(checks, "query", contains(formatted_query, needle), f"{needle!r} missing from {formatted_query!r}")
    for needle in expect.get("query_excludes", []):
        check(checks, "query", not contains(formatted_query, needle), f"{needle!r} in {formatted_query!r}")
    if "latest_news" in expect:
        check(checks, "latest", latest == expect["latest_news"], f"got {latest}")
    if "kb_title_any" in expect:
        hit = any(contains(t, n) for t in titles for n in expect["kb_title_any"])
        check(checks, "retrieve", hit, f"top titles: {[t[:50] for t in titles]}")
    if "coverage_in" in expect and coverage is not None:
        check(checks, "coverage", coverage in expect["coverage_in"], f"got {coverage}")
    if "path_in" in expect:
        check(checks, "path", info["path"] in expect["path_in"], f"got {info['path']}")
    return checks, info, log.getvalue()


_app = None


def answer_case(case):
    """Full answer through /api/chat. Returns (checks, info, log)."""
    global _app
    if _app is None:
        with contextlib.redirect_stdout(io.StringIO()):
            from run import app
        _app = app
    expect = case.get("expect", {})
    payload = {
        "jailbreak": True,
        "conversation_id": "eval-run",
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
    log_text = log.getvalue()
    text = re.sub(r"\n{3,}", "\n\n", HALFTIME_LINE.sub("", raw)).strip()
    if "doesn't answer the question" in log_text or "flagged nothing relevant" in log_text:
        path = "combined"
    elif HALFTIME_LINE.search(raw):
        path = "draft_update"
    else:
        path = "single"

    checks = []
    check(checks, "answer:status", response.status_code == 200 and text, f"HTTP {response.status_code}, {len(text)} chars")
    if "path_in" in expect:
        check(checks, "answer:path", path in expect["path_in"], f"got {path}")
    for needle in expect.get("answer_contains_all", []):
        check(checks, f"answer:has {needle!r}", contains(text, needle))
    if expect.get("answer_contains_any"):
        check(checks, "answer:has any", any(contains(text, n) for n in expect["answer_contains_any"]),
              f"any of {expect['answer_contains_any']}")
    for needle in expect.get("answer_excludes", []):
        check(checks, f"answer:no {needle!r}", not contains(text, needle))
    leaked = [s for s in LEAKS if s in text]
    check(checks, "answer:no leaked internals", not leaked, f"found {leaked}" if leaked else "")
    jargon = sorted(set(m.lower() for m in JARGON.findall(text)))
    check(checks, "answer:no internal jargon", not jargon, f"found {jargon}" if jargon else "")
    ended_with_update = path != "draft_update" or bool(HALFTIME_LINE.split(raw)[-1].strip())
    if ended_with_update:
        block = FOLLOW_UPS.split(text)
        items = [l for l in block[-1].splitlines() if FOLLOW_UP_ITEM.match(l)] if len(block) > 1 else []
        check(checks, "answer:ask-next block", 2 <= len(items) <= 3, f"{len(items)} items")
    info = {"path": path, "first_words_s": round(first_words, 1) if first_words else None,
            "total_s": round(total, 1), "answer": text}
    return checks, info, log_text


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--answers", action="store_true", help="also generate answers for cases marked \"answer\": true (paid flash calls)")
    parser.add_argument("--only", help="comma-separated case ids")
    parser.add_argument("--cases", default=str(REPO / "evals" / "cases.json"))
    args = parser.parse_args()

    spec = json.loads(Path(args.cases).read_text())
    cases = spec["cases"]
    if args.only:
        wanted = set(args.only.split(","))
        cases = [c for c in cases if c["id"] in wanted]

    print(f"Loading the Run of Play keyword index... (cases as of {spec.get('as_of')})")
    with contextlib.redirect_stdout(io.StringIO()):
        kb_search._refresh()
    kb_search._refreshing = True  # one snapshot for the whole run

    report, all_calls, started = [], [], time.time()
    for case in cases:
        checks, info, log_text = route_case(case)
        calls = usage_from(log_text)
        if args.answers and case.get("answer"):
            answer_checks, answer_info, answer_log = answer_case(case)
            checks += answer_checks
            info["answer"] = answer_info
            calls += usage_from(answer_log)
        all_calls += calls
        failed = [c for c in checks if not c["ok"]]
        mark = "PASS" if not failed else "FAIL"
        print(f"\n[{mark}] {case['id']}: {case['question']}")
        print(f"       search={info['search_required']} latest={info['latest_news']} coverage={info['coverage']} "
              f"path={info['path']} query={info['query']!r}")
        if "answer" in info:
            a = info["answer"]
            print(f"       answer: path={a['path']} first words {a['first_words_s']}s, done {a['total_s']}s")
        for c in checks:
            if not c["ok"]:
                print(f"       x {c['check']}: {c['detail']}")
        report.append({"id": case["id"], "question": case["question"], "checks": checks, "info": info,
                       "cost": sum(c["cost"] for c in calls)})

    # Summary per stage
    stages = {}
    for row in report:
        for c in row["checks"]:
            stage = c["check"].split(":")[0] if not c["check"].startswith("answer:") else "answer"
            passed, total = stages.get(stage, (0, 0))
            stages[stage] = (passed + c["ok"], total + 1)
    cases_passed = sum(all(c["ok"] for c in r["checks"]) for r in report)
    cost = sum(c["cost"] for c in all_calls)
    by_tier = {tier: sum(1 for c in all_calls if c["tier"] == tier) for tier in ("free", "paid")}
    print("\n" + "=" * 72)
    print(f"Cases passed: {cases_passed}/{len(report)}")
    for stage in ("classify", "query", "latest", "retrieve", "coverage", "path", "answer"):
        if stage in stages:
            passed, total = stages[stage]
            print(f"  {stage:9} {passed}/{total}")
    print(f"Gemini calls: {len(all_calls)} ({by_tier['free']} free key, {by_tier['paid']} paid key) - "
          f"estimated cost ${cost:.4f}; {time.time() - started:.0f}s")

    out_dir = REPO / "evals" / "reports"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"{datetime.now():%Y%m%d-%H%M%S}{'-answers' if args.answers else ''}.json"
    out.write_text(json.dumps({"as_of": spec.get("as_of"), "ran_at": datetime.now().isoformat(),
                               "answers": args.answers, "cases_passed": cases_passed, "cases": len(report),
                               "stages": stages, "cost_usd": cost, "calls": all_calls, "results": report},
                              indent=2, ensure_ascii=False))
    print(f"Report: {out.relative_to(REPO)}")
    return 0 if cases_passed == len(report) else 1


if __name__ == "__main__":
    sys.exit(main())
