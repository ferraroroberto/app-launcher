"""Deterministic PTY child for the #610 concurrent-paint load probe.

Stands in for a real coding agent in ``test_session_host_concurrent_paint``:
prints one unique, per-session sentinel as fast as it can, then idles until
the ConPTY is closed. Nothing else — the probe measures how long the
session-host takes to deliver that first paint to an attached WS client under
a five-session spawn burst, so any startup cost of the child itself would
only blur the signal (the real ``claude`` CLI takes 3-5 s to paint).

Run as ``python _pty_paint_child.py <tag>``.
"""

from __future__ import annotations

import sys
import time


def main() -> None:
    tag = sys.argv[1] if len(sys.argv) > 1 else "notag"
    sys.stdout.write(f"<<<PAINT:{tag}>>>\n")
    sys.stdout.flush()
    while True:
        time.sleep(0.25)


if __name__ == "__main__":
    main()
