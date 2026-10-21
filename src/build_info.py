"""Process build identity — the git SHA this process actually loaded (#199, #246, #247).

Captured once, at import time, so it reflects the code running in *this*
process rather than live git state: a process started three days ago still
reports the SHA it booted with, even if ``HEAD`` has since moved. That is the
whole point — the "Restart/deploy coverage" convention in this repo's
``CLAUDE.md`` compares a process's captured identity against the repo's
current ``HEAD`` to decide whether a merged change actually shipped, and a
helper that re-resolved live would make every process report "fresh" no
matter what it is actually running.

**Retries a failed resolution inside the capture, never after it.** A single
transient ``git`` failure at import used to freeze ``"unknown"`` for a
process's entire life (observed: 8 days, ``app-launcher#825``). The retry
happens within :func:`build_identity` itself, in the first second of start,
so the "captured at my own start" semantic holds; a later re-resolve would
let a stale process report a newer ``HEAD`` as a confident "fresh" — strictly
worse than ``"unknown"``. A genuinely broken resolution (no ``git``, not a
repo) still yields ``"unknown"`` rather than a guess.

Vendor-verbatim: consuming apps copy this file byte-identical into their own
``src/`` (like ``src/no_window.py``, ``src/pooled_http.py``) — the project
root is the only call-site argument, so the copy never forks. Self-contained
on purpose (no import of this repo's own ``src.no_window``): a byte-identical
copy dropped into another repo's tree must resolve on its own.
"""

from __future__ import annotations

import datetime as _dt
import subprocess
import sys
import time as _time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: ``subprocess.CREATE_NO_WINDOW`` on Windows, ``0`` (a no-op) elsewhere — a
#: console-less parent (a tray, a scheduled task) would otherwise flash a
#: console window for every ``git`` spawn. Derived locally rather than
#: imported from ``src.no_window`` — see module docstring.
_NO_WINDOW: int = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

_GIT_TIMEOUT_SECONDS = 5.0


def resolve_git_sha(project_root: Path = PROJECT_ROOT) -> str:
    """Short git SHA of ``project_root``'s checkout.

    Falls back to ``"unknown"`` if git isn't on ``PATH``, ``project_root``
    isn't a repo, or the call times out — all of which happen in test
    environments and none of which should crash startup.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(project_root), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if result.returncode != 0:
        return "unknown"
    sha = result.stdout.strip()
    return sha or "unknown"


CAPTURE_ATTEMPTS = 3
CAPTURE_BACKOFF_SECONDS = 0.3


def build_identity(
    project_root: Path = PROJECT_ROOT,
    *,
    attempts: int = CAPTURE_ATTEMPTS,
    backoff_seconds: float = CAPTURE_BACKOFF_SECONDS,
) -> dict[str, str]:
    """``{"git_sha", "built_at"}`` for ``project_root``, computed now.

    Call once at process/module import to capture "what this process
    loaded"; call again later (fresh, uncached) to get the live, current
    value for a staleness comparison. See the module docstring for why the
    retry lives inside this call rather than as a later re-resolve.

    Only the failing path pays the backoff — a successful first attempt
    returns immediately, which is every normal start.
    """
    sha = resolve_git_sha(project_root)
    for attempt in range(1, max(1, attempts)):
        if sha != "unknown":
            break
        _time.sleep(backoff_seconds * attempt)
        sha = resolve_git_sha(project_root)
    return {
        "git_sha": sha,
        "built_at": _dt.datetime.now().replace(microsecond=0).isoformat(),
    }
