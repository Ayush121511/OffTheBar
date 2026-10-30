# Pipeline evals

`run_evals.py` runs the questions in `cases.json` through the real Off The Bar
pipeline, scores every step, and rebuilds a dashboard. Run it before a deploy
and after any change to retrieval, prompts or routing.

```bash
python evals/run_evals.py                      # routing sections only - ~$0.01 for all cases
python evals/run_evals.py --answers            # + real answers for cases marked "answer": true - ~$0.05 more
python evals/run_evals.py --domain latest-news --answers
python evals/run_evals.py --only clasico-result,false-nine --answers
python evals/dashboard.py                      # rebuild the dashboard from existing reports
```

Then open `evals/reports/dashboard.html` (self-contained, works offline). It
needs the same `.env` as the backend (Gemini, Pinecone, SearXNG). The exit code
is 0 only if every case passes.

## Domains - what kind of question

Every case belongs to one domain, listed with a description in `cases.json`:

| Domain | Covers |
|---|---|
| `latest-news` | current events, form, standings |
| `match-results` | how a specific recent match went |
| `awards` | awards and records, with and without a year |
| `transfers` | transfers, contracts, rumours |
| `history` | past tournaments, finals, careers |
| `rules-tactics` | laws of the game, tactical concepts |
| `gaming` | EA FC / FIFA / Ultimate Team |
| `conversation` | multi-turn: pronoun follow-ups, consistency with earlier answers |
| `robustness` | typos, slang, small talk |

## Sections - what part of the pipeline

Every check belongs to one section (`scoring.py`):

| Section | Weight | Checks | Stage |
|---|---|---|---|
| Understanding | 15 | search decision, search query keeps the question's names and invents no years, latest-news detection | routing |
| Retrieval | 15 | the expected Run of Play article is among the hits | routing |
| Routing | 15 | coverage verdict (`full` / `partial` / `none`) and the answer path it picks | routing |
| Answer accuracy | 20 | expected facts present, stale or contradicting claims absent | `--answers` |
| Answer quality | 15 | rubric from a flash-lite judge: answers the question, voice, clarity, consistency with the chat (1-5 each). The judge is given each case's confirmed facts, since it otherwise disbelieves news newer than its training | `--answers` |
| Safety & format | 10 | no leaked internals (`NO_NEW_UPDATES`, `[RUN OF PLAY ...`), no source jargon ("dispatches"), **no American football** (NFL, quarterbacks, touchdowns, "the other kind of football"...), a 2-3 item `Ask next:` block on every answer, drafts included | `--answers` |
| Performance | 10 | time to first words within the path's budget (single 15s, draft 8s, combined 25s) | `--answers` |

## Scoring

- Pass/fail checks score 1 or 0. A rubric rating of r scores (r-1)/4 and passes
  at 4+. Latency scores 1 within budget, falling linearly to 0 at twice the budget.
- A section's score is the mean of its checks.
- The overall score is the weighted mean of the sections that ran. A routing-only
  run is scored on Understanding, Retrieval and Routing alone, so compare
  routing runs with routing runs; the dashboard's trend shows the two kinds as
  separate lines.
- Domain scores are computed the same way over each domain's cases.

## Cost

Every Gemini call's tokens come from the `[OffTheBar Gemini]` log lines; calls on
a paid key are priced at list rates (`PRICES` in `run_evals.py`). Each run prints
and records its cost: about $0.007 for a routing run of 38 cases even when all
of it lands on the paid key, and about $0.05 more with the 14 answers.

## The dashboard

`evals/reports/dashboard.html`, rebuilt after every run from every report in
`evals/reports/`:

- overall score with the change since the previous run of the same kind, cases
  and checks passed, run cost, median time to first words
- score by section (bars), score trend across runs (line)
- domain × section heatmap
- every failed check, and a table of all cases - click a row for the question,
  search query, Run of Play articles, the answer, the judge's note and every check

A run selector and a domain filter at the top scope everything below them.

## Adding a case

```json
{
  "id": "vinicius-staying",
  "domain": "transfers",
  "question": "Is Vinicius Junior staying at Real Madrid?",
  "history": [],
  "answer": true,
  "expect": {
    "search_required": true,
    "query_includes": ["Vinicius"],
    "kb_title_any": ["Vini"],
    "answer_contains_any": ["2032", "extension"]
  },
  "notes": "Why the expectations are what they are."
}
```

Every `expect` key is optional; string checks are case-insensitive:
`search_required`, `latest_news`, `query_includes`, `query_excludes`,
`kb_title_any`, `coverage_in`, `path_in`, `answer_contains_all`,
`answer_contains_any`, `answer_excludes`. `history` turns are
`{"role": "user" | "model", "content": ...}`.

## Caveats

- **News moves on.** Cases about current events (dated by `as_of`) drift as Run
  of Play publishes and the news changes. When one fails, check the site and the
  news before blaming the code, then update the case.
- **Not fully deterministic.** The classifier, coverage check and rubric judge
  run at temperature 0, but answers and web search results vary, and Bing News
  occasionally returns nothing for a moment. A one-off failure is worth a look,
  not an alarm.
- Reports from before domains existed are in `evals/reports/legacy/` and aren't
  on the dashboard.
- The answer stage writes its turns to `chat_logs/eval-run.json`, like any chat.
