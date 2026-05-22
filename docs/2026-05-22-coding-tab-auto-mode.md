# Coding tab — Auto mode / Skip permissions selector for Claude Code

**Issue:** #54 · **Date:** 2026-05-22

## What was done

The `claude` CLI launch used to hardcode `--dangerously-skip-permissions`
as an always-on flag — no UI, no choice. The CLI now offers a safer
**`auto`** permission mode (`--permission-mode auto`): it still runs
without prompts, but a separate classifier model reviews each action and
blocks the genuinely dangerous ones (production deploys, force-push,
push to `main`, mass deletion, `curl | bash`, destroying pre-session
files), and it nudges Claude to keep working without stopping for
clarifying questions.

The Claude Code subsection of the **⚙️ Coding options** card now has a
**Permission** segmented control with two mutually-exclusive options:

- **Auto mode** (`--permission-mode auto`) — the new default.
- **Skip permissions** (`--dangerously-skip-permissions`) — the legacy
  no-safety-net bypass, kept as an escape hatch. The official docs
  confirm it is exactly equivalent to `--permission-mode
  bypassPermissions`, so no third option was added.

`--remote-control` stays the only always-on Claude flag.

## Files modified

- `src/webapp_config.py` — `VALID_CLAUDE_PERMISSION_MODES` +
  `DEFAULT_CLAUDE_PERMISSION_MODE`; `claude_permission_mode` field
  (load/save/validate); `--dangerously-skip-permissions` removed from
  `ALWAYS_ON_CLAUDE_FLAGS`; `build_claude_flags` emits the selected
  permission flag.
- `app/webapp/routers/config.py` — `permission_mode` +
  `permission_modes_available` in the `claude` block of
  `GET /api/config`; `claude_permission_mode` added to the `POST`
  allow-list.
- `app/webapp/routers/claude_code.py` — same two keys on
  `GET /api/claude-code/flags`.
- `app/webapp/static/index.html` — Permission `.opt-row` in the Claude
  Code subsection.
- `app/webapp/static/state.js` — `claudePermission` element ref.
- `app/webapp/static/claude-options.js` — Permission segmented control
  rendered + wired in `renderClaudeSubsection`.
- `config/webapp_config.sample.json` — `claude_permission_mode` default
  + updated `_comment_claude_flags`.
- `README.md` — Coding-options subsection list, the always-on-flags
  note, the security callout, and the config table row.

## Tests

- `tests/test_webapp_api_basics.py` — `/api/claude-code/flags` exposes
  `permission_mode`; skip-permissions is no longer always-on; default
  `computed_flags` carries `--permission-mode auto`.
- `tests/test_webapp_api_config.py` — `claude` block carries the two new
  keys; `claude_permission_mode` patch round-trip (`skip` →
  `--dangerously-skip-permissions`); invalid value → 400.
- `tests/e2e/test_smoke.py` — `#claudePermission` renders its 2 buttons.

## Validation

- `pwsh -File scripts/verify-before-ship.ps1` — byte-compile + non-e2e
  pytest + Playwright e2e (Chromium + WebKit/iPhone).
- Webapp restarted on `:8445`; new build confirmed via `GET /api/version`.

## Notes

- **Out of scope (deliberate):** auto mode requires Opus 4.6/4.7 or
  Sonnet 4.6 on the Anthropic API. If the Model selector is set to
  Haiku, the `claude` CLI reports auto mode unavailable itself —
  cross-wiring the Model and Permission controls was left out per
  "implement only what was asked."
- Whether `claude --remote-control --permission-mode auto` is honored at
  runtime is not verifiable from code; it needs a live launch. If the
  host rejects or silently downgrades auto mode, the fix is a one-line
  default flip to `"skip"` — "Skip permissions" stays one tap away.
