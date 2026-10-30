"""
Scoring for eval runs - shared by run_evals.py (console summary) and
dashboard.py (HTML), so both always show the same numbers.

Every check in a report belongs to one section and has a score in [0, 1]
(pass/fail checks score 1 or 0; rubric ratings and latency score in between).
A section's score is the mean of its checks; the overall score is the
weighted mean of the sections that ran, so a routing-only run is scored on
the routing sections alone. Domains are scored the same way over their own
cases.
"""
from typing import Dict, Iterable, List, Optional

# id, label, weight, what it measures
SECTIONS = [
    ("understanding", "Understanding", 15, "Search decision, search queries, latest-news detection"),
    ("retrieval", "Retrieval", 15, "Run of Play articles returned; web results association football only"),
    ("routing", "Routing", 15, "Coverage verdict and the answer path it picks"),
    ("accuracy", "Answer accuracy", 20, "Expected facts present; stale or contradicting claims absent"),
    ("quality", "Answer quality", 15, "Rubric (flash-lite judge): answers the question, voice, clarity, consistency with the chat"),
    ("safety", "Safety & format", 10, "No leaked internals or source jargon; the Ask next block"),
    ("performance", "Performance", 10, "Time to first words within the path's budget"),
]
SECTION_IDS = [s[0] for s in SECTIONS]
WEIGHTS = {s[0]: s[2] for s in SECTIONS}

_ROUTING_CHECKS = {"classify": "understanding", "query": "understanding", "latest": "understanding",
                   "retrieve": "retrieval", "coverage": "routing", "path": "routing"}


def section_for(check: Dict) -> str:
    """The check's section - stored on new checks, derived from the name for
    reports written before sections existed."""
    if check.get("section"):
        return check["section"]
    name = check["check"]
    if name in _ROUTING_CHECKS:
        return _ROUTING_CHECKS[name]
    if name == "answer:path":
        return "routing"
    if name.startswith(("answer:has", "answer:no '")):
        return "accuracy"
    return "safety"


def check_score(check: Dict) -> float:
    return float(check["score"]) if "score" in check else (1.0 if check["ok"] else 0.0)


def _summarise(results: Iterable[Dict]) -> Dict:
    sums: Dict[str, List[float]] = {}
    passed = total = cases_passed = cases = 0
    for row in results:
        cases += 1
        cases_passed += all(c["ok"] for c in row["checks"])
        for check in row["checks"]:
            sums.setdefault(section_for(check), []).append(check_score(check))
            passed += bool(check["ok"])
            total += 1
    sections = {
        sid: {"score": sum(v) / len(v), "checks": len(v)}
        for sid in SECTION_IDS if (v := sums.get(sid))
    }
    weight = sum(WEIGHTS[sid] for sid in sections)
    overall: Optional[float] = (
        sum(WEIGHTS[sid] * s["score"] for sid, s in sections.items()) / weight if weight else None
    )
    return {"overall": overall, "sections": sections, "cases": cases, "cases_passed": cases_passed,
            "checks": total, "checks_passed": passed}


def score_run(results: List[Dict], domain_of: Dict[str, str]) -> Dict:
    """Scores for a whole run and for each domain."""
    def domain(row):
        return row.get("domain") or domain_of.get(row["id"], "other")

    domains = sorted({domain(r) for r in results})
    return {
        "all": _summarise(results),
        "domains": {d: _summarise([r for r in results if domain(r) == d]) for d in domains},
    }
