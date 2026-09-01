"""Raw stdin readback child for the #64 real-PTY integration test.

Run inside a real ConPTY by ``test_session_host_pty_realpty.py``. It puts
its console stdin into raw mode (no line buffering / echo processing that
would mangle a paste), signals readiness by touching ``<result>.ready``,
then reads from fd 0 until it has seen the sentinel terminator and writes
everything received before the sentinel to the destination.

This is the bounded-pipe-faithful check the reopened #64 demanded: a
``MagicMock`` PtyProcess can never drop bytes, so only a real pseudoconsole
readback proves ``PtySession.write`` delivers a multi-KB payload intact.

**The destination arrives in the environment, never in argv (#822).**
pywinpty's string-command spawn runs the command through
``shlex.split(cmd, posix=False)`` and re-joins it with ``list2cmdline``, so a
positional path is at the mercy of that tokenising: a single space splits it
and this child then writes to a truncated prefix of the intended path. When
that prefix resolved to this file, ``open(result, "wb")`` silently truncated
the helper's own source and the run still reported green. An environment
variable is not tokenised, so the value arrives byte-exact.

``_reject_unsafe_destination`` is the belt-and-braces half: even if a
destination somehow arrives wrong, refusing to write a ``.py`` file or
anything inside the checkout turns silent corruption into a loud failure
(the parent's readiness wait times out and asserts).
"""
import ctypes
import os
import sys
from ctypes import wintypes
from pathlib import Path

SENTINEL = b"<<<EOP>>>"
RESULT_ENV = "PTY_READBACK_RESULT"

# This file lives in <repo>/tests/, so parents[1] is the checkout root.
REPO_ROOT = Path(__file__).resolve().parents[1]


def _reject_unsafe_destination(raw: str) -> Path:
    """Return the destination, or exit non-zero if writing it would clobber source.

    Two refusals, both cheap and both about the same failure: a destination
    that is not a scratch file. Writing a ``.py`` is never legitimate here,
    and neither is writing anywhere inside the checkout — the readback target
    is always a pytest ``tmp_path``.

    The in-checkout refusal is the one that matters most, because the parent
    spawns this child with ``cwd`` set to ``tests/``: any *relative* fragment
    left behind by a tokenising slip resolves straight into the checkout, which
    is precisely how a mangled argument turned into a truncated source file.
    ``Path.resolve()`` before the check, so a relative fragment is judged by
    where it would actually land rather than by how it is spelled.
    """
    dest = Path(raw).resolve()
    if dest.suffix.lower() == ".py":
        sys.exit(f"refusing to write a .py destination: {dest}")
    try:
        dest.relative_to(REPO_ROOT)
    except ValueError:
        return dest  # outside the checkout — the expected case
    sys.exit(f"refusing to write inside the checkout: {dest}")


raw_result = os.environ.get(RESULT_ENV)
if not raw_result:
    sys.exit(f"{RESULT_ENV} is unset: the parent must pass the destination in the environment")
result = _reject_unsafe_destination(raw_result)

# Disable ENABLE_PROCESSED_INPUT(0x1) / LINE_INPUT(0x2) / ECHO_INPUT(0x4)
# and turn on ENABLE_VIRTUAL_TERMINAL_INPUT(0x200) so bytes arrive raw, the
# way a TUI in raw mode (Claude Code) receives them.
k32 = ctypes.windll.kernel32
h = k32.GetStdHandle(-10)  # STD_INPUT_HANDLE
mode = wintypes.DWORD()
k32.GetConsoleMode(h, ctypes.byref(mode))
k32.SetConsoleMode(h, (mode.value & ~0x1 & ~0x2 & ~0x4) | 0x0200)

open(str(result) + ".ready", "w").close()

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
