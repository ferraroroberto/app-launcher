# Fix flaky e2e gate on the windows-latest CI runner

**Issue:** #58 — CI `e2e` gate flaky on the `windows-latest` runner; PTY-input
tests time out.

## What was done

The CI `e2e` workflow intermittently red-lit on PTY-input-delivery tests while
the identical local `verify-before-ship.ps1` passed clean — a flake forced
PR #57 to merge with `--admin`. Root cause: the GitHub-hosted Windows runner
does the keystroke → ConPTY → session-host → log round-trip slower than a dev
box, so the tests' hardcoded wait budgets expired before the input landed.

Two budget classes were too tight:

1. **Session-log poll loop** — a hardcoded `deadline_ms = 5_000` + 200 ms poll,
   duplicated inline in three test files and as the `_wait_for_log` default in
   a fourth.
2. **Playwright UI waits** — hardcoded `timeout=10_000` on the terminal-overlay
   mount waits and the compose-bar image-upload `to_have_value` assertion.

Both are now centralised, env-aware, and tolerant of a slow runner. The local
defaults are unchanged, so a local gate run stays fast.

### Changes

- **`tests/e2e/conftest.py`**
  - `_env_int(name, default)` — reads a positive int from the environment,
    falling back to the default on a missing/malformed/non-positive value (a
    bad env var must never *shorten* a budget).
  - `wait_for_session_log` fixture — returns a callable
    `(page, sid, needle, deadline_ms=None) -> bool` that polls
    `webapp/sessions/<sid>.log`. Deadline defaults to
    `LAUNCHER_E2E_LOG_DEADLINE_MS` (`5000` ms). Single source of truth for the
    input-delivery wait that was copied into four files.
  - `e2e_ui_timeout` fixture — int ms for Playwright UI waits, from
    `LAUNCHER_E2E_UI_TIMEOUT_MS` (`10000` ms).
- **`tests/e2e/test_compose_bar.py`, `test_paste_button.py`,
  `test_terminal_reconnect.py`, `test_keys_popover.py`** — dropped the
  per-file `_read_session_log` / poll loop / `_wait_for_log` copies; use the
  shared fixtures. Hardcoded `timeout=10_000` UI waits replaced with
  `e2e_ui_timeout`. Assertion messages rewritten so a timeout reads as a
  timeout — the misleading "issue #NN regressed" wording is gone.
- **`.github/workflows/e2e.yml`** — the "Run pre-ship gate" step now sets
  `LAUNCHER_E2E_LOG_DEADLINE_MS=20000` and `LAUNCHER_E2E_UI_TIMEOUT_MS=30000`,
  giving the slow runner headroom. `verify-before-ship.ps1` is untouched — the
  env vars flow through to the pytest process.
- **`README.md`** — documented the two env tunables; corrected a stale line
  that claimed the terminal regression tests "skip cleanly" on CI. They do not:
  the PTY session launches regardless of whether `claude` is on the runner's
  PATH, the session log records input bytes either way, so the tests run on CI
  — which is exactly why a timing flake showed up there.

## Files modified

- `tests/e2e/conftest.py`
- `tests/e2e/test_compose_bar.py`
- `tests/e2e/test_paste_button.py`
- `tests/e2e/test_terminal_reconnect.py`
- `tests/e2e/test_keys_popover.py`
- `.github/workflows/e2e.yml`
- `README.md`

## Validation

- `& .\.venv\Scripts\python.exe -m py_compile` on every modified `.py` file.
- `pwsh -File scripts/verify-before-ship.ps1` — full pre-ship gate, exit 0.
- True confirmation is a green CI `e2e` check on the branch push, since the
  flake only reproduces on the hosted runner.

## Out of scope

No change to runtime PTY / session-host / WebSocket code — this was a
test-harness timing issue, not a product defect.
