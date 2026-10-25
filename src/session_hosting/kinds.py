"""Session kinds and stop modes shared by PTY and detached sessions."""

# Stop modes accepted by SessionManager.stop / PtySession.stop.
STOP_INTERRUPT = "interrupt"  # Ctrl+C into the PTY
STOP_QUIT = "quit"            # type "/quit" — Claude Code's clean exit
STOP_KILL = "kill"            # force-terminate the ConPTY

# Graceful-stop grace window: how long STOP_QUIT waits for the agent to exit
# on its own quit command before force-terminating as a fallback.
# A clean /quit exits in ~0.7 s empirically; 5 s is generous headroom and
# stays under the session-client stop timeout.
_STOP_GRACE_SECONDS = 5.0

# Session kinds. "pty" is a launcher-owned ConPTY streamed to the phone;
# "remote" is a detached console window the launcher only tracks (no PTY,
# no scrollback, no WebSocket — the Claude cloud app drives it).
KIND_PTY = "pty"
KIND_REMOTE = "remote"
