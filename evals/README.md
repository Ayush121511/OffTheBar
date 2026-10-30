# Pipeline evals

`run_evals.py` runs the questions in `cases.json` through the real pipeline and
checks each step against what the case expects. It's meant to be run before a
deploy and after any change to retrieval, prompts or routing.

```bash
python evals/run_evals.py                      # routing only: ~2 flash-lite calls per case, well under a cent
python evals/run_evals.py --answers            # + full answers for cases marked "answer": true
python evals/run_evals.py --only clasico-result,world-cup-2010 --answers
```

It needs the same `.env` as the backend (Gemini, Pinecone, SearXNG). Exit code
is 0 only if every case passes, and a JSON report of each run goes to
`evals/reports/` (gitignored), for comparing runs.

## What it checks

| Stage | What | Cost |
|---|---|---|
| `classify` | whether the question needs a web search (`classify_and_build_query`) | 1 flash-lite call |
| `query` | the web search query keeps the question's names and invents no years | (same call) |
| `latest` | whether it counts as a latest-news question (`_is_latest_news_question`) | free |
| `retrieve` | which Run of Play articles the hybrid search returns (by title) | free |
| `coverage` | how much of the answer those articles hold: `full` / `partial` / `none` (`judge_kb_coverage`) | 1 flash-lite call |
| `path` | single answer / draft + update / combined, as `server/backend.py` would pick | free |
| `answer` | with `--answers`: a real `/api/chat` answer, checked for expected facts, stale phrases, leaked internals (`NO_NEW_UPDATES`, `[RUN OF PLAY ...`, ...), source jargon ("dispatches") and the `Ask next:` block | 1-3 gemini-2.5-flash calls |

Every Gemini call's token usage is read from the `[OffTheBar Gemini]` log
lines, and calls on a paid key are priced, so each run prints what it cost.
A routing run of the 20 cases costs about half a cent even when it all lands
on the paid key; the answer stage adds about a cent per answer.

## Adding a case

Each case in `cases.json` has an `id`, a `question`, optional `history`
(earlier turns, `{"role": "user"|"model", "content": ...}`), `"answer": true` to
include it in the answer stage, and an `expect` object where every key is
optional:

- `search_required`, `latest_news`: true/false
- `query_includes`, `query_excludes`: substrings of the classifier's search query
- `kb_title_any`: at least one retrieved Run of Play article title contains one of these
- `coverage_in`, `path_in`: lists of acceptable values
- `answer_contains_all`, `answer_contains_any`, `answer_excludes`: substrings of the answer

All string checks are case-insensitive.

## Caveats

- **News moves on.** Cases about current events (dated by `as_of`) will drift as
  Run of Play publishes and the news changes. When one fails, check the site and
  the news before blaming the code, and update the case.
- **The models aren't fully deterministic.** The classifier and coverage check
  run at temperature 0, but answers don't. A case that fails once and then
  passes is worth a look, not an alarm.
- The answer stage writes its turns to `chat_logs/eval-run.json`, like any chat.
