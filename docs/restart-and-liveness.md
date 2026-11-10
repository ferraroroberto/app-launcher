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
4. Run the restart from **PowerShell**, wrapped by the replacement check:

   ```
   .\.venv\Scripts\python.exe -m src.launching.restart_check snapshot
   tray.bat --restart
   .\.venv\Scripts\python.exe -m src.launching.restart_check verify
   ```

   `verify` polls `GET /api/version` (60 s by default) and exits 0 only when
   the answer carries a **different `pid`** than the snapshot **and** a
   `git_sha` matching `HEAD`. A matching `git_sha` alone proves nothing: a
   restart that never ran leaves the old process serving, and when that
   process already loaded `HEAD` the sha matches anyway. Exit 1 = the process
   was not replaced (or nothing answers); exit 2 = no baseline, so a restart
   cannot be confirmed. Neither is a pass. Report the `verify` line.
5. From Git Bash, `cmd.exe /c "tray.bat --restart"` never enters the script:
   MSYS rewrites `/c`, cmd opens an interactive prompt, and the call exits 0
   with no output and nothing restarted. Use PowerShell (or `cmd.exe //c`).
   `tray.bat` appends `entered` and `helper exit=<code>` lines to
   `webapp/restart.log`, as does `restart_check`, so a restart with no
   `entered` line was never run.
6. `built_at` is the webapp process's start time, captured at import; it moves
   only when a webapp process starts. It does not distinguish a tray restart
   from a webapp-only restart (the tray's Restart webapp item, a self-heal),
   so it is no proof that `tray.bat --restart` ran. `pid` is.
7. Don't hand off "done" with a stale process still serving.

## A session-host change is not live until `:8446` itself restarts

A diff touching `src/session_host.py` or `app/webapp/session_host/` can merge, pass a
fully green gate, and go through a correct `tray.bat --restart` while the
running session-host keeps executing days-old code, silently.

**Before reporting a session-host-path change as shipped**, read
`GET /api/version`'s `session_host` block —
`{"reachable", "git_sha", "started_at", "stale", "stale_relevant", "freshness"}`:

- `git_sha` is the *loaded* SHA (captured once at that process's own start, not
  live git state).
- `stale` means it differs from the repo's resolved `origin/main` tip,
  re-resolved fresh on every call — **not** the primary checkout's current
  branch, which can transiently sit on an unrelated feature branch while a
  worker occupies the tree, under-reporting `stale_relevant` as `false`.
- `stale_relevant` scopes that to whether a declared session-host path was
  actually touched between the loaded SHA and that resolved ref.
- Both read `null` (never a confident false) when either SHA, or the diff
  itself, can't be resolved.
- `freshness` folds the three into one declared state: `unreachable`
  (no answer from `:8446`), `unknown` (a SHA couldn't be resolved), `fresh`
  (loaded SHA equals the resolved ref), `stale-irrelevant` (differs, but no
  declared path was touched), `stale-relevant` (differs and a declared path
  was touched) and `stale-unknown` (differs, but the scoped diff couldn't run).
  The unknown states are never folded into `fresh`.

**Gate on `freshness`, not raw `stale`** — `stale-relevant` means **report the
change as merged but not yet live**, never as shipped; `stale-irrelevant`
means nothing to restart for; `unknown` and `stale-unknown` mean it could not
be confirmed either way.

**Do not restart `:8446` to "fix" this** as a side effect of finishing an
issue: it kills every live PTY on the machine, including any standing fleet
chief. The one supported restart path is the `update command` in `CLAUDE.md`'s
`## session-host` block.
