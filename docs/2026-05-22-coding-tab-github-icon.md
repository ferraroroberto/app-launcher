# Coding tab: GitHub repo icon on each project tile

**Issue:** #56

## What was done

Each Coding-tab project tile now carries a trailing **GitHub icon**, sitting
after the per-agent launch buttons. Tapping it opens the project's GitHub
repository in a new browser tab — it spawns no process and creates no session.

The repo URL is derived from the project's `origin` git remote by reading
`<project_dir>/.git/config` directly (no `git` subprocess). The SCP-style SSH
(`git@github.com:owner/repo.git`), HTTPS (`https://github.com/owner/repo.git`),
and `ssh://` remote forms all normalise to a browsable
`https://github.com/<owner>/<repo>` URL. A project with no `.git/config`, no
`origin` remote, or a non-GitHub host gets the icon rendered **disabled** with a
"No GitHub remote" hover hint — matching how an unavailable agent button looks,
so the tile layout stays uniform.

`.git/config` is parsed with `configparser.ConfigParser(strict=False)`: git's
config format allows a key to repeat within a section (multivar), and tools like
VS Code write duplicate entries (e.g. `vscode-merge-base` under `[branch …]`).
configparser's default strict mode raises `DuplicateOptionError` on those, which
would otherwise have silently disabled the icon for any project VS Code had
touched.

## Files modified

- `src/scanner.py` — new `github_repo_url()` + `_normalise_github_url()` helpers.
- `src/registry.py` — `AppEntry.repo_url` field (emitted only when set);
  `live_claude_code_entries()` populates it per project.
- `app/webapp/static/apps.js` — `renderCodingList()` appends the GitHub icon
  button after the agent buttons.
- `app/webapp/static/icons/github.svg` — new icon.
- `tests/test_scanner_projects.py` — `TestGithubRepoUrl` covers the SSH/HTTPS/
  `ssh://` forms, `.git` stripping, non-GitHub host, no-origin, and no-`.git`.
- `README.md` — Coding-tile description updated.

## Out of scope

Non-GitHub git hosts (GitLab, Bitbucket) — treated as "no repo". Deep links to a
specific branch, issue, or PR — the icon opens the repo root.

## Validation

- `pytest tests -m "not smoke"` — 132 passed.
- `pwsh -File scripts/verify-before-ship.ps1` — green (132 non-e2e + 47 e2e).
- Webapp restarted on `:8445`; new build confirmed via `GET /api/version`.
