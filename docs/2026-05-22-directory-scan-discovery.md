# 2026-05-22 — Claude Code tab: directory-scan discovery (issue #44)

## What was done

The Claude Code tab no longer discovers projects from `.code-workspace`
files or orphan `*-remote.bat` files, and there is no scan step for it.
It now lists **every direct child directory** of the configured master
folder (`projects_dir`), minus a gitignore-style ignore list.

- New config key `projects_ignore` (list of patterns) — case-insensitive,
  `*`/`?` globs honoured, matched against the bare folder name. VCS /
  build dirs (`.git`, `.venv`, `venv`, `__pycache__`, `node_modules`,
  `.idea`, `.vscode`) are always skipped regardless.
- `claude-code` rows are no longer persisted in `config/apps.json` —
  they are recomputed live on every `/api/apps` call. Stale `claude-code`
  rows in an older `apps.json` are ignored. The Apps tab (bat-based
  rows) keeps its registry and scan flow unchanged.
- The **📜 Create BATs** feature is removed — it only ever served the
  retired bat/workspace system (`src/bat_generator.py`, the
  `/api/claude-code/generate` routes, and the generate dialog).
- The ignored-folders list is editable from the Settings panel
  (textarea next to "Projects dir"), saved via `/api/config`.

## Files modified

- `src/scanner.py` — removed `scan_claude_code_projects`,
  `ClaudeCodeProject`, the workspace/bat readers, `pretty_name_from_stem`;
  added `PROJECT_SCAN_SKIP_DIRS`, `ProjectDir`, `dir_ignored`,
  `scan_project_dirs`.
- `src/registry.py` — added `live_claude_code_entries`; `discover_new`
  now scans bats only.
- `src/webapp_config.py` — added the `projects_ignore` field (load/save).
- `app/webapp/routers/apps.py` — `/api/apps` merges live claude-code rows
  with registry bat rows; `launch_app` resolves claude-code rows against
  the live scan.
- `app/webapp/routers/config.py` — `projects_ignore` in GET + POST
  allow-list (coerced to a clean string list).
- `app/webapp/routers/claude_code.py` — dropped the two
  `/api/claude-code/generate` routes; `/api/claude-code/flags` kept.
- `src/bat_generator.py` — deleted.
- `app/webapp/static/` — `index.html` (ignore textarea, removed gen
  dialog + button, reworded empty state), `apps.js` (removed gen dialog
  code; no edit-mode actions on claude-code rows), `state.js`,
  `main.js`, `claude-options.js` (wire the ignore textarea).
- `config/webapp_config.sample.json`, `config/apps.sample.json` — updated.
- `README.md` — Claude Code tab, Config table, Layout, Files.

## Tests

- Deleted `tests/test_webapp_api_generate.py`.
- Added `tests/test_scanner_projects.py` (`dir_ignored`,
  `scan_project_dirs`).
- `tests/test_webapp_api_apps.py` — `TestClaudeCodeDiscovery`: live child
  dirs surface, stale registry rows ignored, ignore list honoured, VCS
  dirs skipped, launch resolves a live row.
- `tests/test_webapp_api_config.py` — `projects_ignore` round-trip.

## Validation

- `pwsh -File scripts/verify-before-ship.ps1` — byte-compile, non-e2e
  pytest, Playwright e2e on both projections. Exit 0.
