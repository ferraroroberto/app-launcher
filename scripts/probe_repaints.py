"""Count Claude Code's full-viewport repaints in a PTY session transcript.

The measurement behind issue #930. A Coding/PTY session's scrollback shows
the same paragraph two or three times; the copies are genuinely in the raw
PTY byte stream, and each one is preceded by a *full-viewport repaint
preamble* — cursor home, erase every viewport row, cursor home again::

    ESC[H  (ESC[2K ESC[1B) x N  ESC[H   <repainted frame>

``ESC[2K`` erases only the rows of the **viewport**. Anything that has
already scrolled into xterm's scrollback is out of its reach, so the
repaint *adds* a copy instead of replacing one. ``N`` is the viewport
height at that moment, which is why the histogram doubles as a record of
what row counts the PTY was driven at.

Two things this tool is for:

* **Reproducing #930's evidence.** ``--by-rows`` prints repaints per MB of
  output at each viewport height — the dose-response that shows the rate
  scales roughly with ``1/rows``.
* **Checking a session after a change.** Point it at a fresh transcript and
  compare against the resize breadcrumbs ``PtySession.resize()`` writes to
  ``webapp/session-host.log`` (``--resize-log``). Repaints far exceeding
  logged resizes means the repaints are *not* resize-driven — which is what
  #930's reopening found, and what rules out a resize-side fix.

This is a read-only diagnostic. Nothing in the serving path imports it: the
repaint is legitimate output from the agent and the terminal renders it
faithfully on purpose (see ``docs/launcher-owned-pty.md``).

Usage::

    python -m scripts.probe_repaints webapp/sessions/<sid>.transcript
    python -m scripts.probe_repaints --by-rows webapp/sessions/*.transcript
    python -m scripts.probe_repaints --resize-log webapp/session-host.log \
        webapp/sessions/<sid>.transcript
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Tuple

# ESC[H  then one or more (ESC[2K ESC[1B)  then ESC[H.
_PREAMBLE_RE = re.compile(rb"\x1b\[H(?:\x1b\[2K\x1b\[1B)+\x1b\[H")
# One erase-this-row-and-step-down unit inside that preamble.
_ERASE_UNIT_RE = re.compile(rb"\x1b\[2K\x1b\[1B")
# `PtySession.resize()`'s breadcrumb: "PTY <sid8> resize 40x51 -> 13x51".
_RESIZE_LOG_RE = re.compile(
    r"PTY\s+(?P<sid>[0-9a-f]{8})\s+resize\s+"
    r"(?P<from_rows>\d+)x(?P<from_cols>\d+)\s*->\s*"
    r"(?P<to_rows>\d+)x(?P<to_cols>\d+)"
)


class Repaint(NamedTuple):
    """One full-viewport repaint preamble found in a transcript."""

    offset: int
    """Byte offset of the preamble's first ESC."""

    rows: int
    """Viewport height it erased — the PTY's row count at that moment."""


def find_repaints(stream: bytes) -> List[Repaint]:
    """Return every full-viewport repaint preamble in ``stream``, in order."""
    return [
        Repaint(m.start(), len(_ERASE_UNIT_RE.findall(m.group(0))))
        for m in _PREAMBLE_RE.finditer(stream)
    ]


def rows_histogram(repaints: List[Repaint]) -> Dict[int, int]:
    """Map viewport height -> how many repaints happened at that height."""
    return dict(sorted(Counter(r.rows for r in repaints).items()))


def density_by_rows(
    stream_len: int, repaints: List[Repaint]
) -> Dict[int, Tuple[int, int]]:
    """Map viewport height -> (repaint count, bytes produced at that height).

    A transcript is a single stream with no size markers in it, so the run
    each repaint belongs to is attributed to *its own* erase-row count: the
    bytes since the previous repaint were produced while the viewport was
    that tall. The tail after the last repaint is attributed to the last
    height. Good enough for a rate comparison, which is all it is used for.
    """
    per: Dict[int, List[int]] = {}
    previous = 0
    for repaint in repaints:
        slot = per.setdefault(repaint.rows, [0, 0])
        slot[0] += 1
        slot[1] += repaint.offset - previous
        previous = repaint.offset
    if repaints:
        per[repaints[-1].rows][1] += stream_len - previous
    return {rows: (count, size) for rows, (count, size) in sorted(per.items())}


def logged_resizes(log_text: str, sid: str) -> List[Tuple[int, int, int, int]]:
    """Resize breadcrumbs for one session id, as (from_rows, from_cols, to_rows, to_cols).

    ``sid`` may be the full session id; the log records only its first 8
    characters, so it is truncated to match.
    """
    prefix = sid[:8]
    out = []
    for match in _RESIZE_LOG_RE.finditer(log_text):
        if match.group("sid") != prefix:
            continue
        out.append(
            (
                int(match.group("from_rows")),
                int(match.group("from_cols")),
                int(match.group("to_rows")),
                int(match.group("to_cols")),
            )
        )
    return out


def _per_mb(count: int, size: int) -> float:
    return count / max(size, 1) * 1e6


def _report(
    path: Path, by_rows: bool, resize_log: Optional[str]
) -> Tuple[int, int]:
    stream = path.read_bytes()
    repaints = find_repaints(stream)
    print(
        f"== {path.name}  {len(stream) / 1e6:.2f} MB  "
        f"repaints={len(repaints)}  rows={rows_histogram(repaints)}"
    )
    if resize_log is not None:
        resizes = logged_resizes(resize_log, path.stem)
        print(
            f"   logged resizes={len(resizes)}  "
            f"-> {len(repaints) - len(resizes)} repaints with no resize"
        )
    if by_rows:
        for rows, (count, size) in density_by_rows(len(stream), repaints).items():
            print(
                f"   rows={rows:>3}  repaints={count:>4}  "
                f"bytes={size / 1e6:>6.2f} MB  "
                f"-> {_per_mb(count, size):>7.1f} repaints/MB"
            )
    return len(repaints), len(stream)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "transcripts", nargs="+", type=Path, help="session .transcript files"
    )
    parser.add_argument(
        "--by-rows",
        action="store_true",
        help="break the rate down by viewport height (repaints per MB)",
    )
    parser.add_argument(
        "--resize-log",
        type=Path,
        default=None,
        help="session-host.log, to compare repaints against logged resizes",
    )
    args = parser.parse_args(argv)

    log_text = None
    if args.resize_log is not None:
        log_text = args.resize_log.read_text(encoding="utf-8", errors="replace")

    total_repaints = 0
    total_bytes = 0
    for path in args.transcripts:
        if not path.is_file():
            print(f"!! {path}: not a file", file=sys.stderr)
            continue
        count, size = _report(path, args.by_rows, log_text)
        total_repaints += count
        total_bytes += size

    if len(args.transcripts) > 1:
        print(
            f"\n== TOTAL  {total_bytes / 1e6:.2f} MB  repaints={total_repaints}"
            f"  -> {_per_mb(total_repaints, total_bytes):.1f} repaints/MB"
        )
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
