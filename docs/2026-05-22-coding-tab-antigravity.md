# Coding tab — dual launch (Claude Code + Antigravity CLI)

**Issue:** #45 · **Date:** 2026-05-22

## What was done

Turned the single-agent "Claude Code" tab into a two-agent **Coding**
tab. Each project tile now launches either **Claude Code** (`claude`) or
the **Antigravity CLI** (`agy`) — Google's Go-based terminal agent that
replaced Gemini CLI — both cwd'd into the project folder and both hosted
by the existing session-host PTY/remote machinery.

- **Tab renamed** "Claude Code" → "Coding" (visible label only; the
  internal `tabClaude` / `paneClaude` / `state.tab='claude'` ids were
  kept to limit churn and keep the e2e selectors stable).
- **Tile redesign** — a Coding tile shows the **bare on-disk folder
  name** (no prettification, no kind-pill, no path meta line) plus one
  icon button per coding agent, side by side.
- **Antigravity CLI integration** — verified the command is `agy`. It
  launches interactively with no flags. Detection resolves the command
  on `PATH`; an agent's launch button is disabled with a hover hint when
  its CLI isn't installed.
- **Generalised the spawned command** — `src/session_host.py` no longer
  hardcodes `cmd /c claude {flags}`; an `agent` argument is threaded
  through `session_client` → `launcher.spawn_claude_session` →
  `/api/apps/{id}/launch` and the session-host `/sessions` endpoint.
- **Running-sessions list** marks each session with its agent's icon.

### Coding options card (follow-up, same branch)

The "Claude options" card was reworked into a collapsible **Coding
options** card (a `<details>`, collapsed by default) with two
subsections:

- **Claude Code** — model / effort / verbose / debug, unchanged. The
  always-on `--remote-control` / `--dangerously-skip-permissions`
  switches still apply.
- **Antigravity** — `agy --help` confirms the CLI has *no* model /
  effort / verbose flags (its model is picked with `/model` in-session),
  so the subsection offers the only two launch-relevant switches it
  does have: **Skip permission prompts** (`--dangerously-skip-permissions`)
  and **Sandbox** (`--sandbox`), both opt-in, both off by default.

The ☁️ Detached toggle moved into the card's always-visible `<summary>`
row so it stays reachable when the panel is collapsed; a click there is
stopped from also toggling the `<details>`.

## Files modified

- `src/agents.py` *(new)* — agent registry (`claude`→`claude`,
  `antigravity`→`agy`), `command_for`, `is_installed`, `detect_agents`.
- `src/session_host.py` — `create` / `create_remote` take an `agent`;
  `PtySession` / `RemoteSession` store it; `to_api()` surfaces it.
- `app/session_host/server.py` — `/sessions` accepts + validates `agent`.
- `src/session_client.py`, `src/launcher.py` — thread `agent` through.
- `src/scanner.py` — `ProjectDir.name` is now the raw on-disk folder
  name (dropped `pretty_folder_name` for these rows).
- `app/webapp/routers/apps.py` — `launch_app` honours `agent`, picks
  per-agent flags (`build_claude_flags` vs `build_antigravity_flags`),
  rejects an uninstalled non-Claude agent.
- `app/webapp/routers/misc.py` — new `GET /api/agents`.
- `src/webapp_config.py` — `antigravity_skip_permissions` /
  `antigravity_sandbox` fields + `build_antigravity_flags`.
- `app/webapp/routers/config.py` — `antigravity` block in `GET
  /api/config`; the two toggles added to the `POST` allow-list.
- `app/webapp/static/` — `apps.js` (Coding-tile renderer + dual launch),
  `sessions.js` (agent icon), `claude-options.js` (Antigravity
  subsection), `main.js` / `state.js` (`/api/agents` fetch + new
  elements), `tabs.js`, `index.html` (tab label + collapsible Coding
  options card), `styles.css`, and new `icons/claude.svg` +
  `icons/antigravity.svg`.
- `config/webapp_config.sample.json` — the two Antigravity toggles.
- `README.md` — Coding tab, dual launch, Antigravity install + options.

## Tests

- `tests/test_agents.py` *(new)* — `command_for`, `is_installed`,
  `detect_agents`.
- `tests/test_session_host_agent.py` *(new)* — `create_remote` spawns
  the per-agent command; `to_api()` carries `agent`.
- `tests/test_webapp_api_apps.py` — raw-folder-name assertions; launch
  with `agent` (Claude default, Antigravity), unknown-agent and
  not-installed rejections.
- `tests/test_webapp_api_basics.py` — `/api/agents` shape.
- `tests/test_scanner_projects.py` — raw-folder-name assertion.
- `tests/test_webapp_api_config.py` — `antigravity` block shape +
  toggle round-trip.
- `tests/e2e/test_smoke.py` — `test_coding_options_populated` expands
  the collapsed Coding options `<details>` before asserting.

## Validation

- `pwsh -File scripts/verify-before-ship.ps1` — byte-compile +
  non-e2e pytest + Playwright e2e (Chromium + WebKit/iPhone). Exit 0.
- Webapp restarted on `:8445`; new build confirmed via `GET /api/version`.

## Notes

- Claude Code's launch path is left **unguarded server-side** (exactly
  as before #45) — it's the launcher's core agent. Only non-Claude
  agents get the server-side install check, as defence-in-depth behind
  the already-disabled UI button.
- The icon assets are original SVG glyphs, swappable on disk.
- **Installing `agy`:** `irm https://antigravity.google/cli/install.ps1 | iex`
  (the official installer — drops a checksum-verified `agy.exe` in
  `%LOCALAPPDATA%\agy\bin\` and adds it to the User PATH). The winget
  `Google.Antigravity` package is the Antigravity *IDE*, not the CLI.
- Both launcher processes resolve `agy` from `PATH` at startup, so after
  installing the CLI the **whole tray** must be restarted — a `:8445`
  webapp-only restart updates detection but leaves the `:8446`
  session-host (which spawns `agy`) on the stale `PATH`.
