# Why detached sessions get a single "Stop" (no graceful stop)

A design decision worth re-reading before anyone tries to add a graceful "Ctrl+C, leave the window open" stop to detached sessions. Short version: it was considered and deliberately not built — the cost outweighs the marginal UX win.

## The two session kinds

The launcher runs Claude/agent sessions in two shapes:

- **Attached (`PtySession`)** — the process runs in a ConPTY owned by the session-host, output streamed to the phone over a WebSocket. It has a real stdin/PTY, so it can be **gracefully stopped** (send `Ctrl+C`, the agent exits cleanly, the terminal stays in the viewport) *or* stopped-and-closed.
- **Detached (`RemoteSession`)** — the process runs in its own console window, orphaned out of the session-host's process tree (see `SessionManager.create_remote`). The launcher keeps **only the PID**; there is no PTY, no stdin pipe, no WebSocket. Remote control comes from the Claude cloud app, not the launcher.

Because a detached session has no stdin/PTY the launcher can reach, the only stop it can perform is a kill.

## The decision

**Detached sessions expose a single Stop (⏏️) button that kills the console subtree** (`taskkill /PID <pid> /T /F` over the orphaned console — still reachable by its own PID). Attached sessions keep both ⏹️ (Stop, graceful) and ⏏️ (Stop & Close). `RemoteSession.stop()` accepts the `close_window`/`mode` parameters only for interface parity with `PtySession`; for a detached session they are inert — there is no PTY to send `/quit` to and nothing to keep open.

## Why graceful stop for a detached session isn't worth it

Gracefully interrupting a detached console (so the window stays open at a fresh prompt instead of vanishing) is *technically possible* on Windows, but every path has real friction:

1. **Cross-process `GenerateConsoleCtrlEvent` is rejected.** `CTRL_C_EVENT` only targets process-group 0 — the caller's own console. `GenerateConsoleCtrlEvent(0, target_pid)` fails because the caller doesn't share a console with the target.
2. **The working pattern mutates global console state.** The standard workaround is `FreeConsole()` → `AttachConsole(target_pid)` → `SetConsoleCtrlHandler(None, True)` → `GenerateConsoleCtrlEvent(0, 0)` → restore. It works (Sysinternals, windows-kill use it) but it mutates global console state inside a long-lived web-server process and needs a lock to prevent races.
3. **Stdin pipes don't survive a detached console.** Even passing `stdin=PIPE` at spawn doesn't help — the new console allocates its own standard handles and the pipe is orphaned. Sending `/quit` over stdin would require *not* detaching the console, which means no visible window — defeating the point of the mode.
4. **`taskkill` without `/F` still tears down the tree.** It isn't a "soft interrupt," so it doesn't buy a graceful exit anyway.

The `AttachConsole` route (#2) would buy only a marginal UX win — the window lingering at a prompt instead of closing — at the cost of ~30 lines of ctypes, a global lock, and a real risk of subtle console-handle bugs in a long-running uvicorn process. Not worth it. Revisit only if a concrete user-facing reason emerges.
