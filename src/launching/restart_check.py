"""Proof that a ``tray.bat --restart`` replaced the webapp process.

A matching ``git_sha`` does not prove a restart: a restart that never ran
leaves the old process serving, and when that process already loaded ``HEAD``
the sha matches anyway. The webapp's ``pid`` changes only with a new process,
so the check records it before the restart (``snapshot``) and, afterwards
(``verify``), passes only when the answer carries a different ``pid``, that
pid runs from this checkout, and its ``git_sha`` matches ``HEAD``. The checkout
matters because a second clone at the same commit answers with the same sha on
the same port. Anything that cannot be established (no baseline, no pid in the
answer, an owner whose directory cannot be read, nothing answering) is its own
failure, never a pass.

Run from the repo root, around the restart::

    python -m src.launching.restart_check snapshot
    tray.bat --restart          # from PowerShell, not ``cmd.exe /c`` in Git Bash
    python -m src.launching.restart_check verify

``tray.bat --restart`` itself runs two of these around the shared lifecycle
helper, whose build check cannot tell the two checkouts apart: ``preflight``
refuses when another checkout holds the port, and ``owner`` passes only when
this checkout serves it afterwards.

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
from typing import Any, Callable, Dict, Optional, Tuple

import requests

from src import runtime_files
from src.build_info import resolve_git_sha
from src.clients._loopback_http import pooled_request
from src.launching.process_checkout import (
    FOREIGN,
    OURS,
    classify_pid,
    port_owner,
    process_checkout,
)

_log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_WEBAPP_DIR = _REPO_ROOT / "webapp"
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
    before: Dict[str, Any],
    after: Optional[Dict[str, Any]],
    head_sha: str,
    *,
    root: Path = _REPO_ROOT,
    checkout_of: Optional[Callable[[int], Optional[Path]]] = None,
) -> Tuple[bool, str]:
    """``(replaced, reason)``: whether ``after`` is a new process from ``root`` serving ``head_sha``.

    ``before`` is the snapshot (``{"pid": int | None}``): a ``None`` pid means
    no webapp was answering, so any answering process is new. ``checkout_of``
    defaults to :func:`process_checkout`, looked up when called.
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
    holder = classify_pid(pid, root, checkout_of or process_checkout)
    if holder.state == FOREIGN:
        return False, f"served by {holder.detail}, not this one {root}"
    if holder.state != OURS:
        return False, f"cannot confirm the checkout {root} serves: {holder.detail}"
    return True, f"new process pid {pid} (was {before_pid}) from {holder.checkout} serves git_sha {served}"


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


def preflight(port: int) -> int:
    """Before a restart: 1 when another checkout holds ``port``, so nothing is stopped for it."""
    holder = port_owner(port, _REPO_ROOT)
    if holder.state == FOREIGN:
        _log_line(
            f"preflight FAILED restart refused: {holder.detail}, not this one "
            f"{_REPO_ROOT}. Stop that copy first."
        )
        return 1
    if holder.state != OURS and holder.listening:
        _log_line(f"preflight NOT CONFIRMED {holder.detail}; the check after the restart decides")
        return 0
    _log_line(f"preflight OK {holder.detail}")
    return 0


def owner(port: int) -> int:
    """After a restart: 0 only when this checkout's process serves ``port``."""
    holder = port_owner(port, _REPO_ROOT)
    if holder.state != OURS:
        _log_line(f"owner FAILED {holder.detail}; not confirmed as served from {_REPO_ROOT}")
        return 1
    _log_line(f"owner OK {holder.detail}")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.launching.restart_check", description=__doc__)
    parser.add_argument("action", choices=("snapshot", "verify", "preflight", "owner"))
    parser.add_argument("--port", type=int, default=None, help="webapp port (default: the configured one)")
    parser.add_argument("--timeout", type=float, default=DEFAULT_VERIFY_TIMEOUT_S)
    args = parser.parse_args(argv)
    port = args.port
    if port is None:
        from src.webapp_config import load_webapp_config

        port = load_webapp_config().port
    if args.action == "verify":
        return verify(port, args.timeout)
    return {"snapshot": snapshot, "preflight": preflight, "owner": owner}[args.action](port)


if __name__ == "__main__":
    sys.exit(main())
