# Project Instructions

Canonical instructions for AI coding agents working in this repository. Claude Code reads this file directly as project memory. Other agents (Cursor, Codex, etc.) reach it via the one-line `AGENTS.md` pointer.

## Plan mode is the default
Every non-trivial request starts in plan mode. Non-trivial = anything beyond a one-line fix, a typo, or a question I can answer without touching code.

In plan mode:
- Do NOT edit files, run destructive commands, or commit anything
- Investigate the codebase as needed (read files, search, run read-only commands)
- Resolve ambiguity through questions before proposing a plan
- Present the plan only when you're confident it reflects what I actually want
- Stay in plan mode across rejections — if I push back, revise and re-present, don't bail out to execution

Recommended setting in `.claude/settings.json`:
```json
{ "permissions": { "defaultMode": "plan" } }
```

Exit plan mode only after I explicitly approve. Approval transitions straight to execution in the same turn.

## Asking questions
Ask whenever a decision would be expensive to undo or genuinely ambiguous. One sharp question beats three filler ones. Use multi-choice (2-4 options) when the choice space is bounded — much faster for me to answer than prose.

**Always ask before assuming** any of these:
- File or module location for new code
- Data shape or schema
- Page placement (new page vs. section in existing page)
- Data source (upload, local file, DB via secrets)
- Error and empty-state handling
- Whether to add tests, and at what level

**Don't ask about** things you can determine by reading the code, things I've already specified, or process meta-questions like "is the plan ready?" — that's what plan approval is for.

If multiple reasonable approaches exist, present them as options with tradeoffs. Don't pick silently.

## Before editing
- Re-read any file before modifying it. Don't trust memory across long sessions.
- For files >500 LOC, read in chunks; don't assume you've seen the whole file.
- When renaming a symbol, search separately for: direct calls, type references, string literals, dynamic imports, re-exports, and tests.

## General conventions
- **Project layout** is documented in this repo's `README.md`. Read the README first.
- **Config & secrets:** project config in `config/config.json` (committed template only — real file gitignored). Runtime UI prefs + secrets (`auth_token`, `auth_password`) in `config/webapp_config.json` (gitignored). There is no `.env`.
- **Logging:** use the language's logging facility. In Python that's `logging`, not `print()`. Emojis are welcome in log messages: ℹ️ ⚠️ ❌ ✅
- **Naming:** snake_case for files/functions (Python), PascalCase for classes, UPPER_CASE for constants.
- **Imports:** stdlib → third-party → local.
- **Versioning policy:** follow the existing style in `requirements.txt` — `>=` for lower bounds, no exact pins unless something specifically needs one.
- **Virtual environment:** use the existing `.venv` in this folder. Never create `venv`. Never activate — invoke via `& .\.venv\Scripts\python.exe ...` on Windows, `./.venv/bin/python ...` on POSIX.
- **No hardcoded paths or credentials.**
- **Type hints** on all public Python functions. Use `Optional[T]`, never bare `None` returns.
- Implement only what was asked. No nice-to-haves.

## Execution: scope up front, then carry it through
- Front-load the questions. Settle scope, ambiguity, and hard-to-undo decisions *before* starting — that is the main control point.
- Once scope is agreed, execute end-to-end to a verified, shippable state. Don't stop for per-phase approval; "large" is not "stop".
- Checkpoint on risk, not size. Pause mid-task only for what the agreed scope didn't cover: a real ambiguity, an unforeseen decision, or a finding that contradicts the plan.
- Verify every unit before calling it done (see Verification).

## Chaining connected work
- Issues are split for tracking but are often sequential. After finishing and verifying a unit, check the related open issues.
- If the next step is a natural continuation, state it and proceed — new branch off freshly-merged `main`. Pause for approval only when it's risky, ambiguous, or materially bigger than discussed.
- One branch per coherent unit. Keep commits and branches separable so any piece reviews and reverts on its own; don't sprawl one branch across unrelated issues.

## Verification (before declaring a task done)
Windows / PowerShell:
- Syntax: `& .\.venv\Scripts\python.exe -m py_compile <file>`
- Tests (if any exist): `& .\.venv\Scripts\python.exe -m pytest`
- Webapp boot check: `& .\.venv\Scripts\python.exe -m uvicorn app.webapp.server:app --host 127.0.0.1 --port 8445` then `curl http://127.0.0.1:8445/healthz`.

For any change touching `app/webapp/`, `src/launcher.py`, or `src/session_host*.py`, the pre-ship gate is mandatory before the task is declared done:

```
pwsh -File scripts/verify-before-ship.ps1
```

It byte-compiles, runs the non-e2e pytest suite, then the Playwright e2e suite (Chromium + WebKit/iPhone) against a disposable webapp + session-host it boots itself on a free port — a tray on `:8445` may be running or not. It must exit 0. Don't substitute a bare `pytest` run: that silently skips the e2e suite when no tray is up.

If no checker exists for a project, say so explicitly. Don't claim "tests pass" when there are no tests.

## Documentation discipline
The `docs/` folder is for **durable reference material** a future reader (you, or a cold LLM) will actually re-open — design records, architecture overviews, integration guides, shared playbooks. Filenames describe the topic, not a date.

Never put in `docs/`:
- Plans, roadmaps, TODOs, "future work" → those are GitHub issues.
- Dated per-PR changelog files (`docs/YYYY-MM-DD-*.md`) → the issue + the PR that closes it + `git log` already capture what was done, files modified, and validation run. Don't write a third copy.

For feature work and refactors:
- Update `README.md` if usage, config, or output changed.
- If the change introduces a durable concept worth re-reading (a new integration, a non-obvious architectural decision, a shared pattern), add a topic-named doc — `docs/<topic>.md`, not `docs/YYYY-MM-DD-<topic>.md`.

For one-line fixes and typos: just commit.

## Planning future work
Plans, roadmaps, and proposed features live as **GitHub issues** on this repo, not as files in the tree. One issue per topic (group closely-related items; split when in doubt). Issues should be self-contained enough to hand off to an LLM or a human cold.

When work for an issue is finished, close the issue properly:
- If merged via PR, reference the issue in the PR body (`Closes #N`) so GitHub auto-closes it on merge.
- If completed via direct commits, close the issue manually and paste the relevant commit SHA(s) in a closing comment.

## Git
Never auto-commit or push, never stage files without being asked. When a task is done, prepare a relevant commit message, ready to copy for the user. Never add `Co-Authored-By: Claude` (or any other LLM/AI attribution trailer) to commit messages.

```bash
git add <files>
git commit -m "type: short description

- detail 1
- detail 2"
```

I run it in my own terminal.

## Senior-dev check
Before finishing, ask: "What would a senior, perfectionist dev reject in review?" If the answer points at duplicated state, inconsistent patterns, or broken architecture *within the file you're already editing*, fix it. Don't expand scope to unrelated files.

---

## This repository
Phone-first launcher hub for the rest of the home stack. Four tabs: **Coding** (one tap → launcher-owned ConPTY or detached console, across four agents — Claude Code, Codex CLI, Antigravity CLI, GitHub Copilot CLI — for any project directory under the configured projects folder; the list is the directory listing, no marker files needed), **Apps** (one tap → spawn any registered Streamlit/FastAPI launcher anywhere on disk), **Jobs** (fire one-shot Python scripts or scheduled jobs), and **Life OS** (invoke a `life-os` productivity skill and browse its knowledge). Sister project to `photo-ocr` and `voice-transcriber` (same conventions, same auth model, same Cloudflare tunnel pattern), but for kicking off other processes instead of doing work itself. See `README.md` for setup, layout, and usage.

**Per-project overrides** (these take precedence over the template above):

- **Stack:** FastAPI + vanilla JS — **not** Streamlit. The Streamlit conventions section in the template does not apply here; do not introduce Streamlit.
- **Config & secrets:** there is no `.env`. Project config lives in `config/config.json` (committed template only) and runtime UI prefs + secrets in `config/webapp_config.json` (gitignored).
- The unified app registry lives in `config/apps.json` (gitignored, committed sample in `config/apps.sample.json`). Every row in `apps.json` has a `kind` field: `streamlit` | `webapp` | `tunnel` — these back the **Apps** tab. The **Coding** and **Life OS** tabs need no registry; they list directories live. The **Jobs** tab is backed by `config/jobs.json`.
- **Verification:** during a dev loop, run the e2e suite with the tray up — `.\scripts\run-e2e.ps1` (both projections) or `--browser chromium` for a faster pass. It runs `tests/e2e/test_smoke.py` plus a regression test per closed iPhone bite (cache hygiene, index revalidation, WS reconnect #28, paste button #29, paste framing #64/#111, pywinpty loopback hidden, Edge mirror window #20, WebKit viewport #31, terminal fling-scroll #23, git-status flags #115, off-main status popover #139, Life tile inline row #124, keyboard-aware terminal overlay #135, compose-bar voice dictation #165, live streamed dictation partials #168). Byte-loss at the PTY write boundary has a non-browser real-PTY guard, `tests/test_session_host_pty_realpty.py` (in the `not smoke` suite). Before declaring any webapp/launcher/session-host change done, run the full pre-ship gate `pwsh -File scripts/verify-before-ship.ps1` (boots its own disposable webapp + session-host — no tray needed). See README "Playwright smoke + regression tests" and "Verifying changes before ship" for detail. The non-browser suite alone is `pytest tests -m "not smoke" -v`.
- **Restart and verify before hand-off:** the running webapp has no hot-reload — code edits do nothing until the `:8445` process is restarted. After the pre-ship gate passes, restart the webapp so I can immediately test on the phone, *unless I said not to*. The canonical restart is **`tray.bat --restart`** — orphan-proof reclaim-then-start: it kills the tray subtree (tray + webapp `:8445` + cloudflared), reclaims `:8445` (webapp) by PID scoped to this repo's `.venv` (CommandLine-matched), then starts fresh. **The `:8446` session-host is detach-compliant (project-scaffolding#35):** it is spawned **detached** — re-parented out of the tray subtree via `cmd /c start`, because `taskkill /T` walks the parent-child PID tree and `DETACHED_PROCESS`/`CREATE_NEW_PROCESS_GROUP` do **not** escape it (verified empirically) — it is **excluded from the reclaim sweep**, and the fresh tray **re-adopts** it on start. So `tray.bat --restart` now **preserves the user's open Coding / PTY sessions**, including a Claude Code session hosted on `:8446` — it is safe to run from inside such a session. Run `--restart`, don't hand-roll the kill. Then **confirm the new build is live** with a bounded poll of `GET /api/version` (hard timeout + attempt cap, fail loud) — `git_sha` should match `HEAD` and `asset_hash` should have changed — and report that build line. Don't hand off "done" with a stale process still serving.

## CI expectations
- Workflow `.github/workflows/e2e.yml`, job `verify-before-ship`, on every PR (and on push to `main`). **Advisory, not required** (no branch protection) — the local gate (`scripts/verify-before-ship.ps1`) is the contract.
- Typical green: **~3 min**. Investigate at **>6 min**; treat as wedged at **>12 min**.
- Flaky leg: the **PTY-input-delivery** e2e tests (compose/paste/keys/reconnect, both Chromium and WebKit/iPhone projections) intermittently time out on the slower hosted runner — see #58. Mitigated (#184): CI sets `E2E_LOG_POLL_DEADLINE_MS=20000` (20 s vs the 5 s local default) for input-delivery headroom, the job has `timeout-minutes: 15` so a wedge self-caps to a fast red instead of running to GitHub's 6 h ceiling, and `pytest-timeout` (`timeout=120`, thread method) aborts any single hung test with a stack dump. A wedge/timeout is still a flake, not the diff.
- Self-naming hangs (#186): the e2e suite caps the **default Playwright action + navigation timeout** at 15 s (`tests/e2e/conftest.py`, `E2E_DEFAULT_TIMEOUT_MS`-tunable) — Playwright's own default is 30 s. So a single auto-waiting `.click()`/`goto`/`wait_for_selector` with no explicit `timeout=` whose target never settles fails fast with a `TimeoutError` that *names the locator*, instead of stacking opaque 30 s waits toward the 120 s `pytest-timeout` black box. `expect()` assertions keep their own 5 s default. A red from this is still a flake on a loaded runner — but now a diagnosable, named one.
- CI's only signal beyond the local gate is the **e2e suite** (skipped locally). Its e2e surface = `app/webapp/`, the session-host / PTY layer (`src/session_host*.py`, `src/launcher.py`), `tests/e2e/`, and static assets. A diff touching **none** of these gains nothing from CI.
