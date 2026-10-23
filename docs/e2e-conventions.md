# E2E browser suite conventions

Reference for `scripts/verify-before-ship.ps1`'s browser phase, which runs only on the dev box (CI runs no browser suite). `CLAUDE.md` carries the one-line rules; the reasoning behind each lives here. Dated flake measurements live in the issue tracker and git history, not here.

## Flaky legs

A timeout in these is a flake, not the diff: rerun once.

- **PTY-input-delivery tests** (compose/keys, paste/reconnect) intermittently time out under load. `E2E_LOG_POLL_DEADLINE_MS` tunes the poll deadline, and `pytest-timeout` (`timeout=120`, thread method) aborts a hang with a stack dump.
- **`test_kill_from_terminal_view_stops_and_returns_to_list[webkit]`** waits for `#terminalOverlay` to hide: `stopSession()` awaits the host's full 5s-grace-then-force stop window plus two network hops. `E2E_STOP_OVERLAY_HIDE_MS` tunes the `to_be_hidden` budget.
- **`test_reconnect_replay_does_not_duplicate_scrollback[chromium]`** is **load**-sensitive, not input-delivery: it waits for a typed marker to echo back through a real Claude Code composer, so it degrades with how many other live agent PTYs the box hosts. A red here means **check host load**, not the diff and not "rerun and hope". `E2E_REAL_AGENT_ECHO_MS` tunes the wait and the timeout names the real-agent cold boot. Read the traceback and check *which* wait it names before widening anything; widening helps the margin but cannot make a real agent's cold boot deterministic.

## Mock non-deterministic boot fetches before `goto()`; don't widen a timeout

A late boot response rebuilds the DOM out from under Playwright's click, and no timeout makes its arrival deterministic. `renderBoard()` keeps an open drawer's node (pinned by `test_board_drawer_survives_a_board_poll_mid_interaction`), and a git-status refresh does not re-render the Board at all, but other boot fetches (`/api/apps`, `/api/agents`) still rebuild cards when they land. Mock them before navigation — the pattern `test_backlog_issue_in_progress_shows_a_chip_and_blocks_both_starts` uses.

## Drive a poll with the fake clock, wait on the request, never sleep on a guess

The launcher's polls are plain `setTimeout`/`setInterval`: install `tests/e2e/_fake_clock.py` before `goto()`, `advance()` it, and `wait_for_interval()` first when boot arms the poll after its first fetch (a negative check against a poll that does not exist yet passes whatever the app does). To wait for a request a mocked route records, use `conftest.py::wait_until`; to prove something did *not* happen, `flush_requests` (a sentinel fetch queued behind the page's own) instead of a sleep. A sleep or jump that stands in for "the app got past the point of the wrong action" needs a mutation run: break the app and watch it go red.

## Never assert on a raw `evaluate()` / `bounding_box()` read of a polled-re-render surface

Mocking the boot fetches is necessary but not sufficient: `renderBoard()` rebuilds every card except an open drawer's and `fetchBoard()` re-renders every `BOARD_POLL_MS` (5s) while the Board tab is up, drawer open or not, so any board test running longer than 5s eventually reads across a rebuild. Playwright resolves a locator to a handle *then* reads from it, so a rebuild in between returns an artifact instead of raising — WebKit gives `''` from `getComputedStyle` (shorthand **and** longhand), `None` from `bounding_box()`, `0` for `scrollWidth`/`clientWidth`.

Auto-retrying `expect()` assertions re-resolve and are immune: prefer `expect(...).to_have_css(...)` over a raw `getComputedStyle` read. Where a raw geometry read is unavoidable, wrap it in `tests/e2e/conftest.py::stable_read`. A script evaluated on a rebuilt node goes through `stable_eval` instead: a rebuild between resolve and run hands the script a *detached* node (all boxes 0, `closest()` finds nothing) — a wrong number or `TypeError`, not an artifact `stable_read` can skip. Measure several related boxes (a row and its kebab) in one `stable_eval` call, never one read each: a layout shift between two reads is not an artifact either.

## One shared budget for opening the terminal overlay

`E2E_OVERLAY_OPEN_MS`, in `tests/e2e/conftest.py`. About 19 modules open the terminal before asserting, and each had a hardcoded `timeout=10_000` — tighter than the 15s default, for the suite's slowest operation — the gate's dominant failure mode. **Any new test that opens the terminal must use this constant, never a literal**; the per-leg vars above each widen exactly one test.

## Self-naming hangs

The suite caps the **default Playwright action + navigation timeout** at 15s (`tests/e2e/conftest.py`, `E2E_DEFAULT_TIMEOUT_MS`-tunable) vs Playwright's 30s default, so a single auto-waiting `.click()`/`goto`/`wait_for_selector` with no explicit `timeout=` fails fast with a `TimeoutError` that *names the locator*, instead of stacking opaque 30s waits toward the 120s `pytest-timeout` black box. `expect()` assertions keep their own 5s default. A red here is still a flake on a loaded box — but a diagnosable, named one.
