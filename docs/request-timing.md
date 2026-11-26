# Request timing, budgets and the health snapshot

A slow answer is either slow work or fast work that waited for a free thread. Every `/api/` response says which, in `Server-Timing`:

```
queue;dur=1.2;desc="default+hot-poll", worker;dur=31.5;desc="default+hot-poll", app;dur=63.3
```

- `queue` is the time the request's work waited for a pool thread, `worker` the time it ran on one, `app` the server's whole time to the response headers (the request's `total_ms`), all in milliseconds. `desc` names the pool: `default` (the event loop's own), `hot-poll`, `session-reads`, `git-status`, `git-sync`, the routes' named pools (`board-files`, `session-files`, `session-host-calls`, `route-outbound`, `audit-writes`, `jobs-files`, `job-spawns`, `app-probes`, `life-os-files`, `service-calls`, `misc-files`, one per kind of blocking work, in `app/webapp/route_pools.py`), the background ticks' own one-thread pools (`jobs-snapshot-tick`, `state-reconcile-tick`, `machine-critical-tick`, `auto-restart-tick`, `system-load-tick`), the Screen view's `desktop-screen`, or `none` for a request that used no thread.
- Parallel steps add up, so `queue + worker` can exceed `app`. A shared single-flight build is charged to the request that started it. The header carries what had finished when the response began; the log lines below carry the final figures.
- The event loop's default executor and the named pools are `TimedExecutor`s from the vendored `src/request_timing.py`. A background tick (no request in flight) is not timed.

The vendored `RequestTimingMiddleware` writes these through the `launcher.slowreq` logger; `request_timing_options()` in `app/webapp/observability.py` wires it. `webapp/slow-requests.log` repeats the split as `queue_ms= worker_ms= total_ms= pool=` on every slow-request line (past `LAUNCHER_SLOW_REQUEST_S`, default 3 s) and on every budget line.

## Budgets

Each request class (declared in `src/request_health/__init__.py`) has a **soft** and a **hard** limit, in seconds:

| Class | Request | Soft | Hard |
|---|---|---|---|
| `transcript-tick` | `GET .../transcript?after=` | 0.5 | 1.5 |
| `transcript-open` | `GET .../transcript` | 1 | 1 |
| `session-list` | `GET /api/claude-code/sessions` | 0.5 | 1.5 |
| `board` | `GET /api/board` | 0.5 | 1.5 |
| `send` | `POST .../input` | 5 | 10 |
| `git-status` | `GET /api/claude-code/git-status` | 5 | 10 |
| `shell` | `GET /` | 0.25 | 3 |
| `jobs-list` | `GET /api/jobs` | 0.25 | 3 |

Past soft, the log gets a `⏱️ over budget` line; past hard, a `🚨 budget breach` line. To change a class, set it in `request_budgets` in `config/webapp_config.json`, for example `{"board": [1.0, 3.0]}`; the other classes keep their defaults, an entry that is not `[soft_s, hard_s]` with `0 < soft_s <= hard_s` is ignored with a warning, and a save applies live.

`GET /api/request-stats` (`?window_s=300`) reads the same counters: requests in flight now, the highest in-flight count in the window, the verdicts per class since boot, the budgets in force and the age of the last git-status scan. It reads counters and a cache and starts no work.

## Benchmark

`.\.venv\Scripts\python.exe -m scripts.bench_requests` times the Board, the session list, a transcript open, a live transcript tick and git status against a running webapp (`--base https://127.0.0.1:8445` by default, `-n` samples each) and reports p50, p95 and max, the median queue and worker time the server reported, and the highest in-flight count during the run. It sends GETs only, one at a time. Send writes into a session, so it is measured only with `--send-session <sid>` on a throwaway session. Each run is saved as JSON under `webapp/bench/`, so a run before and a run after a change compare directly. A webapp that predates `/api/request-stats` reports the in-flight figure as unknown.

## How the page polls

One open page asks for each read once at a time. Every poll runs on `startPoll()` in `app/webapp/static/_vendored/poll/poll.js`, which schedules the next tick when the last read has settled, so a slow server stretches the gap instead of stacking requests. The session list and the Board reads are wrapped in `singleFlight()`, so a caller that asks while one is still out (a tab tap, a poll tick, a return to the foreground) shares it; a caller that just wrote uses `.fresh()`, which waits for the read already out and shares the next. A hidden tab or a locked phone arms no tick; coming back reads each poll once, then resumes the normal spacing. `GET /api/apps` re-reads `apps.json` and re-scans the projects folder only when either changes, or once its cached rows are a minute old. `tests/e2e/shell/test_poll_single_flight.py` holds each read open at the route and checks that no second one starts, and that a hidden minute sends nothing.

## Health snapshot

`.\.venv\Scripts\python.exe scripts/launcher.py health` writes `webapp/health/health-<date>-<time>.txt`: who listens on the webapp and session-host ports, available memory, the top CPU processes over one second, the running agent processes, the lane marks and session rows, requests in flight and verdicts per class, the git-status cache age and the last 20 lines of the slow-request log, with the configured secrets blanked. It changes nothing: no process is signalled and no scan is started. A part it cannot establish (the webapp down, no psutil, an unreadable socket table) reads `unknown (<why>)`, never an idle or zero figure.
