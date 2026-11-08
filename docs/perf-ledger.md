# Phone-speed results on the Apps tab

`/perf-review` (fleet-config) measures each app's throttled phone load from this PC and appends one record per run to a local ledger. The Apps tab shows the latest result and a cold-ready trend of every measured app in one Performance card, with the apps that have no result listed under their reason. Nothing about it appears on the Board, and no GitHub issue is read or written: results are measurements, and an issue exists only while a budget is broken.

## Where the data comes from

The ledger is `perf-review/ledger.jsonl` under the hooks state dir (`~/.claude/hooks/state`, or `CLAUDE_HOOKS_STATE_DIR`). Its schema is documented in fleet-config's `skills/perf-review/SKILL.md` ("The ledger"). `src/perf_ledger/` only reads it:

- a reader keeps the last line per `(app, run_id)` and skips a line that does not parse;
- the latest run of an app is its greatest `run_id`;
- the trend is the cold-ready value (`cold.ready_ms`) of the last 12 runs that measured it, oldest first.

## The endpoint

`GET /api/apps/perf` is read-only and sits behind the same auth as every `/api` route.

| Field | Meaning |
|---|---|
| `ledger` | `ok`, `missing` (no file) or `unreadable` (the file cannot be read) |
| `apps` | per ledger app: `state`, `run_id`, `ts`, `build`, `seeded`, `cold_ready_ms` / `warm_ready_ms` / `warm_bytes_kb` (`{measured, budget, status}` or `null`), `fails`, `trend`, `issues_url` |
| `rows` | registry row id → ledger app |

A registry row belongs to the ledger app named by the nearest folder above its bat that the ledger knows (`photo-ocr\tray.bat` → `photo-ocr`). Tunnel rows and Coding rows are never measured. `issues_url` is a search for the repo's open `perf-review` issues, built from a template and set only when the latest run is over budget, so a request makes no GitHub call.

## What the card shows

The card is collapsed by default; its summary says how many apps are measured and how many are over budget. Open, it opens with two sentences on what is measured (the throttled phone-profile load, cold and warm, plus endpoint timing, against the budgets). Then one row per measured app, then the apps without a result.

| Situation | Where it appears |
|---|---|
| latest run within budget | a row: plain "Within budget", the age, cold / warm ready and warm transfer against budget, the sparkline |
| a check over budget | a row: a danger "Over budget" chip, the failing checks named, that figure in the danger colour, an **Issues** link |
| a pass seeded from an old issue log | a row: "Passed, no timings yet", no figures were recorded |
| no ledger record for the app, or no ledger file | "Never measured" |
| a run that measured nothing | "Last run measured nothing" |
| a verdict this page does not recognise | "Result not recognised" |
| a tunnel row | "A tunnel, not a web app of its own" |
| an unreadable ledger, or a failed read with nothing earlier to show | no rows: an "Unknown" summary and a Retry line |

Several registry rows of one repo (its tray and its web app) share one measured row, named by the first of them. Only a record whose own verdict is `pass` reads as within budget; every other state is its own words, never folded into it. A failed read after a good one keeps the last good result and its age. The page asks at most once a minute, and again when the Apps tab opens. App rows themselves carry nothing about performance.

## Tests

The reader and the route run against a fixture ledger in a temp state dir (`tests/conftest.py` and the e2e autoboot both set `CLAUDE_HOOKS_STATE_DIR`, so a test never reads this machine's real results): `tests/unit/perf_ledger/`, `tests/api/webapp/test_webapp_api_apps_perf.py`, `tests/e2e/apps/test_apps_perf_ledger.py`.
