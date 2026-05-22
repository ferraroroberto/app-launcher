# Coding tab — third agent: GitHub Copilot CLI

**Issue:** #48 · **Depends on:** #45 · **Date:** 2026-05-22

## What was done

Added **GitHub Copilot CLI** (`copilot`) as the third agent in the
Coding tab, on top of the multi-agent structure landed by #45. Every
project tile now carries three launch buttons — Claude Code,
Antigravity, GitHub Copilot — all cwd'd into the project folder and all
hosted by the existing session-host PTY/remote machinery.

Because #45 had already generalised the launch path around an `agent`
argument and a `src/agents.py` registry, this was a small *additive*
change rather than another refactor: one new `AGENTS` entry flows the
agent through detection, `command_for`, the session-host spawn, and
`/api/agents` automatically.

- **Registry entry** — `copilot` → `copilot` added to `AGENTS`.
- **Launch flags** — the Copilot CLI picks its model in-session
  (`/model`) and has no model/effort flags. Its one launch-relevant
  switch is `--allow-all` (enable all tool permissions without
  prompting). Exposed as a single opt-in **Skip permission prompts**
  toggle, off by default.
- **Authentication is out of scope for the launcher** — Copilot CLI
  signs in with `/login` in-session and needs a Copilot subscription;
  the launcher only resolves the binary on `PATH` and spawns it. No
  auth probe was added.
- **Sessions list** — the agent-icon renderer in `sessions.js`, which
  hardcoded a claude/antigravity choice, was generalised to resolve the
  icon + label against the agent registry (`state.agents`), so future
  agents need no change there.

## Files modified

- `src/agents.py` — new `copilot` `AGENTS` entry; docstring updated.
- `src/webapp_config.py` — `copilot_skip_permissions` field (load/save)
  + `build_copilot_flags`.
- `app/webapp/routers/config.py` — `copilot` block in `GET /api/config`;
  `copilot_skip_permissions` added to the `POST` allow-list.
- `app/webapp/routers/apps.py` — `launch_app` flag selection switched
  to a per-agent builder dispatch (`claude` / `antigravity` / `copilot`).
- `config/webapp_config.sample.json` — `copilot_skip_permissions` default.
- `app/webapp/static/icons/copilot.svg` *(new)* — icon glyph.
- `app/webapp/static/index.html` — GitHub Copilot subsection in the
  Coding options card.
- `app/webapp/static/state.js` — `copilot` in the agents fallback; new
  element refs.
- `app/webapp/static/claude-options.js` — `renderCopilotSubsection` +
  toggle wiring.
- `app/webapp/static/sessions.js` — registry-driven agent icon (no
  longer hardcoded per agent).
- `app/webapp/static/apps.js` — launch toast agent tag generalised to
  any non-Claude agent. (The Coding-tile renderer already iterated
  `state.agents`, so it picked up the third button with no change.)
- `README.md` — third Coding agent, Copilot CLI install + in-session
  auth note + the toggle.

## Tests

- `tests/test_agents.py` — `command_for("copilot")`.
- `tests/test_session_host_agent.py` — `create_remote` spawns
  `cmd /c copilot` for `agent="copilot"`.
- `tests/test_webapp_api_apps.py` — launch with `agent="copilot"`
  (bare + `--allow-all` toggle); not-installed rejection.
- `tests/test_webapp_api_basics.py` — `/api/agents` lists `copilot`.
- `tests/test_webapp_api_config.py` — `copilot` block shape + toggle
  round-trip.

## Follow-up — per-agent session stop

Mobile testing surfaced a stop bug: stopping a Copilot session left the
`copilot` process running and the session stuck `alive=True`.
`PtySession.stop()` hardcoded typing `/quit` — Claude Code's exit
command. Copilot's interactive exit is `/exit`, so the typed command
did nothing.

Fix:

- `src/agents.py` — `Agent` gained a `quit_command` field
  (`claude`/`antigravity` → `/quit`, `copilot` → `/exit`) plus a
  `quit_command_for` helper that falls back to the default agent.
- `src/session_host.py` — `PtySession.stop()`:
  - **Stop** (window stays) types the agent's *own* quit command.
  - **Stop & Close** now **force-terminates the ConPTY** outright
    instead of relying on a typed command landing — agent-agnostic,
    guaranteed.
- `app/webapp/static/sessions.js` — stop confirmation dialogs no longer
  say "Claude Code" / "Claude will exit cleanly" (wrong for other
  agents).
- Tests: `test_agents.py` (`quit_command_for`),
  `test_session_host_pty_stop.py` (per-agent quit command, force-kill
  on close).

## Follow-up — Copilot model picker + session-title cleanup

A second round of mobile testing produced two tweaks:

- **Copilot model picker.** `copilot help config` showed the CLI does
  accept a `--model` flag (≈15 models — Claude + GPT families). Added a
  `copilot_model` config field (`VALID_COPILOT_MODELS`), wired into
  `build_copilot_flags` as `--model <id>`, surfaced as a `<select>` in
  the GitHub Copilot subsection of the Coding options card. Empty =
  "Default" (no `--model`; the CLI uses its own setting). Earlier docs
  wrongly said Copilot had no model flag — corrected.
- **Session-title glyph stripped.** Coding agents prefix their live
  terminal title with a brand glyph (Claude's green ✳). The per-session
  agent icon already identifies the agent, so `sessionTitle()` in
  `sessions.js` now strips a leading non-alphanumeric run; reused by the
  terminal overlay header.

## Validation

- `pwsh -File scripts/verify-before-ship.ps1` — byte-compile + non-e2e
  pytest + Playwright e2e (Chromium + WebKit/iPhone).
- Webapp restarted on `:8445`; new build confirmed via `GET /api/version`.

## Notes

- **WinGet id is `GitHub.Copilot`** (`winget install -e --id
  GitHub.Copilot`). Also on npm (`@github/copilot`, needs Node.js 22+).
- Both launcher processes resolve `copilot` from `PATH` at startup, so
  after installing the CLI the **whole tray** must be restarted — same
  caveat as `agy`.
