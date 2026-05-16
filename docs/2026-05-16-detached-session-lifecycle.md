# Detached Session Lifecycle Design

**Date:** 2026-05-16  
**Status:** Implemented  
**Issue:** #19 (Two-button stop: Stop vs Stop & Close)

## Problem

Launcher supports two kinds of Claude Code sessions:

1. **Attached (PtySession)**: Process runs in a ConPTY owned by the launcher, output streamed to the webapp via WebSocket. Can be gracefully stopped (Ctrl+C) or closed.
2. **Detached (RemoteSession)**: Process spawned in a new console window (`CREATE_NEW_CONSOLE`) that outlives the launcher. The launcher only tracks the PID and can kill it.

For attached sessions, we can:
- **Stop** (⏹️): Send Ctrl+C → claude exits cleanly → window stays in xterm viewport
- **Stop & Close** (⏏️): Close the mirror page → session removed from list

For detached sessions, we wanted:
- **Stop** (⏹️): Send Ctrl+C → claude exits → cmd.exe window stays open showing prompt
- **Stop & Close** (⏏️): Kill process tree → window closes

## Why Graceful Stop Is Hard (Not Impossible)

Gracefully stopping a detached process is *technically possible* on Windows, but each path has real friction:

1. **Direct `GenerateConsoleCtrlEvent` doesn't work as-is.** `CTRL_C_EVENT` only targets group 0 (the caller's own console). The cross-process call we'd want — `GenerateConsoleCtrlEvent(0, target_pid)` — is rejected because the caller doesn't share a console with the target.
2. **The working pattern requires console juggling.** The standard workaround is `FreeConsole()` → `AttachConsole(target_pid)` → `SetConsoleCtrlHandler(None, True)` → `GenerateConsoleCtrlEvent(0, 0)` → restore. This works (used by Sysinternals, windows-kill, etc.) but mutates global console state in a long-lived web-server process, which is risky and needs a lock to prevent races.
3. **Stdin pipes don't survive `CREATE_NEW_CONSOLE`.** Even if we passed `stdin=PIPE` when spawning, the new console allocates its own standard handles and our pipe is orphaned. So typing `/quit` via stdin requires *not* using `CREATE_NEW_CONSOLE` — which means no visible window for the user.
4. **`taskkill` without `/F`** still terminates the process tree; it isn't a "soft interrupt."

## Decision: Single Button for Detached Sessions

**Don't implement graceful stop — not worth the cost.**

- **For attached (PtySession)**: Show both ⏹️ (Stop) and ⏏️ (Stop & Close) buttons.
- **For detached (RemoteSession)**: Show only ⏏️ (Stop) button, which kills the tree and closes the window.

The AttachConsole approach (#2 above) would buy a marginal UX win — the cmd.exe window stays open with a prompt instead of disappearing. The cost is ~30 lines of ctypes plus a global lock plus a real risk of subtle console-handle bugs in a long-running uvicorn process. We decided the win wasn't worth the risk profile. Revisit if a user-facing reason emerges.

## Code Changes

- **app/webapp/static/app.js**: Conditionally render Stop button only for attached sessions.
- **src/session_host.py**: Simplify `RemoteSession.stop()` to always close the window (ignore `close_window` param).

## Validation Checklist

**Attached (PtySession):**
- [ ] Start a session in xterm (Claude Code in the phone app)
- [ ] Click ⏹️ (Stop): Process stops, terminal stays in viewport
- [ ] Click ⏏️ (Stop & Close): Session removed from list, mirror page closes

**Detached (RemoteSession):**
- [ ] Start a detached app (e.g., LLM local hub)
- [ ] Verify only ⏏️ button is shown (no ⏹️)
- [ ] Click ⏏️: Window closes, process terminates, session removed from list

---

**References**: Windows Console subsystem limits; ConPTY vs CREATE_NEW_CONSOLE trade-offs.
