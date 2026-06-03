"""Raw stdin readback child for the #64 real-PTY integration test.

Run inside a real ConPTY by ``test_session_host_pty_realpty.py``. It puts
its console stdin into raw mode (no line buffering / echo processing that
would mangle a paste), signals readiness by touching ``<result>.ready``,
then reads from fd 0 until it has seen the sentinel terminator and writes
everything received before the sentinel to ``argv[1]``.

This is the bounded-pipe-faithful check the reopened #64 demanded: a
``MagicMock`` PtyProcess can never drop bytes, so only a real pseudoconsole
readback proves ``PtySession.write`` delivers a multi-KB payload intact.
"""
import os
import sys
import ctypes
from ctypes import wintypes

SENTINEL = b"<<<EOP>>>"

# Disable ENABLE_PROCESSED_INPUT(0x1) / LINE_INPUT(0x2) / ECHO_INPUT(0x4)
# and turn on ENABLE_VIRTUAL_TERMINAL_INPUT(0x200) so bytes arrive raw, the
# way a TUI in raw mode (Claude Code) receives them.
k32 = ctypes.windll.kernel32
h = k32.GetStdHandle(-10)  # STD_INPUT_HANDLE
mode = wintypes.DWORD()
k32.GetConsoleMode(h, ctypes.byref(mode))
k32.SetConsoleMode(h, (mode.value & ~0x1 & ~0x2 & ~0x4) | 0x0200)

result = sys.argv[1]
open(result + ".ready", "w").close()

buf = bytearray()
while True:
    chunk = os.read(0, 4096)
    if not chunk:
        break
    buf += chunk
    if SENTINEL in buf:
        buf = buf[: buf.index(SENTINEL)]
        break

with open(result, "wb") as f:
    f.write(buf)
