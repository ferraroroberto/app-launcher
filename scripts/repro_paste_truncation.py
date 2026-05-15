r"""Reproduce issue #4 — long paste truncated by pywinpty short-write.

Boots a cmd.exe inside a ConPTY (same path PtySession uses) and shows that
``pty.write()`` returns fewer bytes than requested for a long payload. The
production wrapper at ``src/session_host.py:189-195`` ignores that return
value and silently drops the unwritten remainder — that is the bug.

Run with:  & .\.venv\Scripts\python.exe -m scripts.repro_paste_truncation
"""

from __future__ import annotations

import sys
import time

from winpty import PtyProcess


def main() -> int:
    payloads = [200, 1000, 4000, 10000, 50000]
    pty = PtyProcess.spawn("cmd.exe /q /k @echo off", dimensions=(40, 200))
    time.sleep(1.0)

    print(f"{'size':>8} | {'returned':>10} | short?")
    print("-" * 36)
    reproduced = False
    for size in payloads:
        data = "x" * size
        n = pty.write(data)
        short = n is None or n < size
        if short:
            reproduced = True
        print(f"{size:>8} | {str(n):>10} | {short}")
        time.sleep(0.2)

    try:
        pty.terminate(force=True)
    except Exception:
        pass

    if reproduced:
        print("\nVERDICT: REPRODUCED — pywinpty short-writes; wrapper drops remainder.")
        return 1
    print("\nVERDICT: NOT REPRODUCED on this run.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
