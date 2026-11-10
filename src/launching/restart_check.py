"""Proof that a ``tray.bat --restart`` replaced the webapp process.

A matching ``git_sha`` does not prove a restart: a restart that never ran
leaves the old process serving, and when that process already loaded ``HEAD``
the sha matches anyway. The webapp's ``pid`` changes only with a new process,
so the check records it before the restart (``snapshot``) and, afterwards
(``verify``), passes only when the answer carries a different ``pid`` and a
``git_sha`` that matches ``HEAD``. Anything that cannot be established
(no baseline, no pid in the answer, nothing answering) is its own failure,
never a pass.

Run from the repo root, around the restart::

    python -m src.launching.restart_check snapshot
    tray.bat --restart          # from PowerShell, not ``cmd.exe /c`` in Git Bash
    python -m src.launching.restart_check verify

Every step appends a line to ``webapp/restart.log``.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import requests

from src import runtime_files
from src.build_info import resolve_git_sha
from src.clients._loopback_http import pooled_request

_log = logging.getLogger(__name__)

_WEBAPP_DIR = Path(__file__).resolve().parent.parent.parent / "webapp"
RESTART_LOG = _WEBAPP_DIR / "restart.log"
BASELINE_FILE = _WEBAPP_DIR / "restart-baseline.json"
DEFAULT_VERIFY_TIMEOUT_S = 60.0
_PROBE_TIMEOUT_S = 3.0
_POLL_INTERVAL_S = 1.0


def fetch_version(port: int) -> Optional[Dict[str, Any]]:
    """``GET /api/version`` on the loopback webapp, or ``None`` when nothing answers."""
    for scheme in ("https", "http"):
        try:
            resp = pooled_request(
                "GET", f"{scheme}://127.0.0.1:{port}/api/version",
                timeout=_PROBE_TIMEOUT_S, verify=False,
            )
            if resp.status_code == 200:
                body = resp.json()
                return body if isinstance(body, dict) else None
        except (requests.RequestException, ValueError) as exc:
            _log.debug("version probe %s failed: %s", scheme, exc)
    return None


def decide(
    before: Dict[str, Any], after: Optional[Dict[str, Any]], head_sha: str
) -> Tuple[bool, str]:
    """``(replaced, reason)``: whether ``after`` is a new process serving ``head_sha``.

    ``before`` is the snapshot (``{"pid": int | None}``): a ``None`` pid means
    no webapp was answering, so any answering process is new.
    """
    if after is None:
        return False, "no webapp answering on the port"
    served = str(after.get("git_sha") or "")
    if not served or served == "unknown":
        return False, f"the webapp reports no usable git_sha ({served or 'empty'})"
    if not (head_sha.startswith(served) or served.startswith(head_sha)):
        return False, f"webapp serves git_sha {served}, expected HEAD {head_sha}"
    pid = after.get("pid")
    if not isinstance(pid, int):
        return False, "the webapp reports no pid, so a restart cannot be confirmed"
    before_pid = before.get("pid")
    if before_pid is not None and pid == before_pid:
        return False, f"same process still serving (pid {pid}): the restart did not replace it"
    return True, f"new process pid {pid} (was {before_pid}) serves git_sha {served}"


def _log_line(message: str) -> None:
    line = f"{datetime.now().isoformat(timespec='seconds')} restart-check {message}"
    RESTART_LOG.parent.mkdir(parents=True, exist_ok=True)
    with RESTART_LOG.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    print(line)


def snapshot(port: int) -> int:
    body = fetch_version(port)
    pid = body.get("pid") if body else None
    runtime_files.make_dirs(BASELINE_FILE.parent)
    runtime_files.save_text(
        BASELINE_FILE,
        json.dumps({"port": port, "pid": pid if isinstance(pid, int) else None}),
    )
    state = f"pid {pid}" if isinstance(pid, int) else "nothing answering (or no pid reported)"
    _log_line(f"snapshot :{port} {state}")
    return 0


def verify(port: int, timeout_s: float = DEFAULT_VERIFY_TIMEOUT_S) -> int:
    try:
        before = json.loads(BASELINE_FILE.read_text(encoding="utf-8"))
        if not isinstance(before, dict) or before.get("port") != port:
            raise ValueError("baseline is for another port")
    except (OSError, ValueError):
        _log_line("verify NOT CONFIRMED: no baseline for this port, run `snapshot` before the restart")
        return 2
    head = resolve_git_sha()
    deadline = time.monotonic() + timeout_s
    reason = "no attempt made"
    while True:
        ok, reason = decide(before, fetch_version(port), head)
        if ok:
            _log_line(f"verify OK :{port} {reason}")
            return 0
        if time.monotonic() >= deadline:
            break
        time.sleep(_POLL_INTERVAL_S)
    _log_line(f"verify FAILED :{port} {reason}")
    return 1


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.launching.restart_check", description=__doc__)
    parser.add_argument("action", choices=("snapshot", "verify"))
    parser.add_argument("--port", type=int, default=None, help="webapp port (default: the configured one)")
    parser.add_argument("--timeout", type=float, default=DEFAULT_VERIFY_TIMEOUT_S)
    args = parser.parse_args(argv)
    port = args.port
    if port is None:
        from src.webapp_config import load_webapp_config

        port = load_webapp_config().port
    if args.action == "snapshot":
        return snapshot(port)
    return verify(port, args.timeout)


if __name__ == "__main__":
    sys.exit(main())
