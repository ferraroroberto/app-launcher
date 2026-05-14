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

## Phased execution for larger work
Multi-file refactors don't go in a single response. Break into phases of ≤5 files each. Complete phase 1, run verification, wait for my approval, then phase 2. Same rule for any task you'd estimate at >30 minutes of work.

## Verification (before declaring a task done)
Windows / PowerShell:
- Syntax: `& .\.venv\Scripts\python.exe -m py_compile <file>`
- Tests (if any exist): `& .\.venv\Scripts\python.exe -m pytest`
- Webapp boot check: `& .\.venv\Scripts\python.exe -m uvicorn app.webapp.server:app --host 127.0.0.1 --port 8445` then `curl http://127.0.0.1:8445/healthz`.

If no checker exists for a project, say so explicitly. Don't claim "tests pass" when there are no tests.

## Documentation discipline
The `docs/` folder is for **work that is already done** — retrospective changelogs, design records, reference material. Never put plans, roadmaps, TODOs, or "future work" docs in `docs/`. If you find yourself writing one, that content belongs in a GitHub issue instead.

For feature work and refactors (not trivial fixes):
- Update `README.md` if usage, config, or output changed
- If the project already has a `docs/` folder, add `docs/YYYY-MM-DD-short-description.md` with: what was done, files modified, validation run
- Don't create a `docs/` folder just to file a changelog entry on a one-off task

For one-line fixes and typos: skip the changelog.

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
Phone-first launcher hub for the rest of the home stack. Two surfaces: a **Claude Code** tab (one tap → fresh CMD window running `claude --remote-control …` for any project with a `.code-workspace` or `*-remote.bat` in the configured projects directory) and an **Apps** tab (one tap → spawn any registered Streamlit/FastAPI launcher anywhere on disk). Sister project to `photo-ocr` and `voice-transcriber` (same conventions, same auth model, same Cloudflare tunnel pattern), but for kicking off other processes instead of doing work itself. See `README.md` for setup, layout, and usage.

**Per-project overrides** (these take precedence over the template above):

- **Stack:** FastAPI + vanilla JS — **not** Streamlit. The Streamlit conventions section in the template does not apply here; do not introduce Streamlit.
- **Config & secrets:** there is no `.env`. Project config lives in `config/config.json` (committed template only) and runtime UI prefs + secrets in `config/webapp_config.json` (gitignored).
- The unified app registry lives in `config/apps.json` (gitignored, committed sample in `config/apps.sample.json`). Every row has a `kind` field: `claude-code` | `streamlit` | `webapp` | `tunnel`. The Claude Code tab is `kind == "claude-code"`; the Apps tab is everything else.
