# Restart and liveness verification

The hand-off procedure for making a verified change actually live, and for
proving it is. `CLAUDE.md` (`## This repository`) carries the one-line recipe
and points here; the `:8446` session-host's machine-parsed declaration (path
list, update command, liveness signal) stays in `CLAUDE.md`'s `## session-host`
block, which this doc does not restate.

## Restart the `:8445` webapp before hand-off

The running webapp has no hot-reload — code edits do nothing until the `:8445`
process is restarted.

1. After the pre-ship gate (`scripts/verify-before-ship.ps1`) passes, restart
   the webapp so the user can immediately test on the phone, *unless they said
   not to*.
2. The canonical restart is **`tray.bat --restart`** — orphan-proof
   reclaim-then-start: it kills the tray subtree (tray + webapp `:8445` +
   cloudflared), reclaims `:8445` by PID scoped to this repo's `.venv`
   (CommandLine-matched), then starts fresh. Run `--restart`, don't hand-roll
   the kill.
3. **`:8446` session-host is excluded from that reclaim and survives**
   (mechanism: `CLAUDE.md`'s `## session-host`), so `--restart` preserves open
   Coding/PTY sessions — including one hosted on `:8446` itself — is safe to
   run from inside such a session, and never restarts `:8446`.
4. Confirm the new build is live with a bounded poll of `GET /api/version`
   (hard timeout + attempt cap, fail loud) — `git_sha` should match `HEAD` and
   `asset_hash` should have changed — and report that build line.
5. Don't hand off "done" with a stale process still serving.

## A session-host change is not live until `:8446` itself restarts (#615)

A diff touching `src/session_host.py` or `app/session_host/` can merge, pass a
fully green gate, and go through a correct `tray.bat --restart` while the
running session-host keeps executing days-old code, silently (#611).

**Before reporting a session-host-path change as shipped**, read
`GET /api/version`'s `session_host` block —
`{"reachable", "git_sha", "started_at", "stale", "stale_relevant"}`:

- `git_sha` is the *loaded* SHA (captured once at that process's own start, not
  live git state).
- `stale` means it differs from the repo's resolved `origin/main` tip,
  re-resolved fresh on every call — **not** the primary checkout's current
  branch, which can transiently sit on an unrelated feature branch while a
  worker occupies the tree, under-reporting `stale_relevant` as `false` (#641).
- `stale_relevant` scopes that to whether a declared session-host path was
  actually touched between the loaded SHA and that resolved ref.
- Both read `null` (never a confident false) when either SHA, or the diff
  itself, can't be resolved.

**Gate on `stale_relevant`, not raw `stale`** — `true` means **report the
change as merged but not yet live**, never as shipped; `false` with
`stale: true` means nothing to restart for.

**Do not restart `:8446` to "fix" this** as a side effect of finishing an
issue: it kills every live PTY on the machine, including any standing fleet
chief. The one supported restart path is the `update command` in `CLAUDE.md`'s
`## session-host` block.
