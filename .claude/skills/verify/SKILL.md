---
name: verify
description: Boot this webapp and drive a real browser against it to visually confirm a change, instead of only running tests.
---

# Verifying app-launcher's webapp in a real browser

This repo has no standing `verify`/`run` skill before this one — cold-start
recipe that worked, captured so the next session skips it.

## Fastest path: reuse the e2e fixtures, don't hand-roll a browser session

`tests/e2e/conftest.py` already gives you a real Playwright browser against a
disposable, auto-booted webapp — no tray needed, no manual auth. Fastest way
to *see* a change, not just assert on it:

1. Find (or write) the e2e test closest to the surface you touched — e.g.
   `tests/e2e/test_board_tab.py` for anything under the Board tab.
2. Temporarily add `authed_page.screenshot(path="scratch_verify_X.png")`
   calls at the points you want to see (before/after an action, each theme).
3. Run just that test:
   ```
   LAUNCHER_E2E_AUTOBOOT=1 .venv/Scripts/python.exe -m pytest tests/e2e/<file>::<test> -q --browser chromium
   ```
   (bash: set the env var inline; PowerShell: `$env:LAUNCHER_E2E_AUTOBOOT=1`).
   Screenshots land in the repo root — move them to the scratchpad dir, then
   `git checkout -- <test file>` to drop the temporary screenshot lines
   (never commit them, per CLAUDE.md's screenshot-privacy rule).
4. Toggle theme mid-test with
   `authed_page.evaluate("document.documentElement.dataset.theme = 'dark'")`
   — no reload needed, CSS reacts live.

This is real rendering of the actual changed JS/CSS against a real webapp
process (autoboot spins up `app.webapp.server:app` + a disposable
session-host) — not a DOM-assertion-only test run.

## Alternative: the live tray webapp

If you want to see a change against your actual running instance instead of
a disposable one: `tray.bat --restart` (see the repo CLAUDE.md's restart
contract), then hit `https://127.0.0.1:8445` directly — loopback bypasses
the bearer-token gate. Terminal-grade surfaces (Board drawer reply/dispatch,
live PTY) additionally need a WebAuthn passkey unless `webauthn_rp_id` is
unset in `config/webapp_config.json`, so the e2e-fixture path above (which
route-mocks past all of that) is usually less friction for a UI-only check.

## Gotchas

- `LAUNCHER_E2E_AUTOBOOT=1` is required for any ad-hoc `pytest tests/e2e/...`
  invocation outside `scripts/run-e2e.ps1` — a bare run without it (and
  without `LAUNCHER_E2E_LIVE=1`) exits with a guard message rather than
  hitting the live phone-facing webapp by accident.
- `--browser webkit` is the iPhone-shaped projection; `chromium` is faster
  for a quick visual pass. Run both when the diff touches layout/CSS.
