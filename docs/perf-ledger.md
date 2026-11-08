# Phone-speed results on the Apps tab

`/perf-review` (fleet-config) measures each app's throttled phone load from this PC and appends one record per run to a local ledger. The Apps tab shows the latest result and a cold-ready trend on each app's row. Nothing about it appears on the Board, and no GitHub issue is read or written: results are measurements, and an issue exists only while a budget is broken.

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

## What a row shows

| Situation | Row |
|---|---|
| latest run within budget | plain "Within budget", the age, cold / warm ready and warm transfer against budget, the sparkline |
| a check over budget | a danger "Over budget" chip, the failing checks named, that figure in the danger colour, an **Issues** link |
| a pass seeded from an old issue log | "Passed, no timings yet": no figures were recorded |
| an unmeasured run, no ledger record for the app, or no ledger file | neutral "Not measured" chip |
| an unreadable ledger, an unrecognised verdict, or a failed read with nothing earlier to show | attention "Unknown" chip |

Only a record whose own verdict is `pass` reads as within budget; every other state is its own words, never folded into it. A failed read after a good one keeps the last good result and its age. The page asks at most once a minute, and again when the Apps tab opens.

## Tests

The reader and the route run against a fixture ledger in a temp state dir (`tests/conftest.py` and the e2e autoboot both set `CLAUDE_HOOKS_STATE_DIR`, so a test never reads this machine's real results): `tests/unit/perf_ledger/`, `tests/api/webapp/test_webapp_api_apps_perf.py`, `tests/e2e/apps/test_apps_perf_ledger.py`.
