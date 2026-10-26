"""Spawning, port-racing and leak-scanning for the disposable servers."""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, IO, List, Optional, Tuple

import psutil
import pytest
import requests

from tests.e2e._process_teardown import terminate_tree as _terminate
from tests.e2e.fixtures.env import REPO_ROOT, UPLOAD_MARKER, XDIST_WORKER
from tests.e2e.stub_session import STUB_BANNER as _STUB_BANNER

logger = logging.getLogger(__name__)


def free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def port_listening(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def spawn(
    cmd: List[str],
    log: IO[str],
    extra_env: Optional[dict] = None,
) -> subprocess.Popen:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    if extra_env:
        env.update(extra_env)
    kwargs: dict = dict(
        cwd=str(REPO_ROOT),
        stdout=log,
        stderr=subprocess.STDOUT,
        env=env,
    )
    if sys.platform == "win32":
        # New process group so we can deliver CTRL_BREAK for a clean stop;
        # no window so the test run doesn't flash consoles.
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        )
    return subprocess.Popen(cmd, **kwargs)




# How many fresh ports a disposable server gets before autoboot gives up. A
# retry only follows a lost port race, never a slow boot.
_BOOT_PORT_ATTEMPTS = 3


def autoboot_log_name(stem: str) -> str:
    """``webapp/<stem>.log``, suffixed with the xdist worker id when there is
    one, so parallel workers never truncate each other's boot log."""
    return f"{stem}-{XDIST_WORKER}.log" if XDIST_WORKER else f"{stem}.log"



def _owns_listener(proc: subprocess.Popen, port: int) -> bool:
    """True when ``proc`` itself, or a descendant, listens on ``port``.

    A descendant counts because the venv's ``python.exe`` is a launcher stub
    that runs the real interpreter as its child. Anything else holding the
    port — another worker's server that won the race for it — does not.
    """
    try:
        family = {proc.pid} | {
            child.pid for child in psutil.Process(proc.pid).children(recursive=True)
        }
    except psutil.Error:
        return False
    return any(
        conn.status == psutil.CONN_LISTEN and conn.laddr and conn.laddr.port == port
        and conn.pid in family
        for conn in psutil.net_connections(kind="tcp")
    )


def boot_on_free_port(
    what: str,
    log_name: str,
    spawn: Callable[[int], subprocess.Popen],
    ready: Callable[[int], bool],
    timeout: float,
) -> Tuple[subprocess.Popen, int]:
    """Spawn ``what`` on a free port and wait until *it* answers there.

    ``free_tcp_port`` releases the port before the child binds it, so another
    process can take it first — rare with one pytest process, a real race
    with several workers booting at once (#1231). ``ready`` alone cannot tell
    that apart from success: a port that listens, or a /healthz that answers,
    may be the other process. So readiness also requires the listener to be
    ours, and a lost race (the child exits, or someone else holds its port)
    retries on a fresh port. A child still alive at ``timeout`` with its port
    held by itself or by nobody is a slow or broken boot, not a race, and
    fails at once with its own message.
    """
    for attempt in range(1, _BOOT_PORT_ATTEMPTS + 1):
        port = free_tcp_port()
        proc = spawn(port)
        deadline = time.time() + timeout
        while time.time() < deadline and proc.poll() is None:
            if ready(port) and _owns_listener(proc, port):
                return proc, port
            time.sleep(0.3)
        alive = proc.poll() is None
        if alive and (_owns_listener(proc, port) or not port_listening(port)):
            _terminate(proc)
            pytest.fail(
                f"autoboot: {what} did not come up on :{port} within "
                f"{timeout:.0f}s — see webapp/{log_name}"
            )
        _terminate(proc)
        logger.warning(
            "⚠️ autoboot: %s lost the race for :%d (attempt %d/%d, %s) — retrying on a new port",
            what, port, attempt, _BOOT_PORT_ATTEMPTS,
            "another process holds it" if alive else "it exited",
        )
    pytest.fail(
        f"autoboot: {what} could not get a port of its own in {_BOOT_PORT_ATTEMPTS} "
        f"attempts — each time it exited or another process held the port; "
        f"see webapp/{log_name}"
    )


def healthz_ok(base: str) -> bool:
    try:
        return requests.get(f"{base}/healthz", timeout=2, verify=False).status_code == 200
    except requests.RequestException:
        return False


def leaked_stub_sessions(
    sessions_dir: Path, since: float, *, limit: int = 5
) -> List[Path]:
    """Harness-written session files that landed in the *real* sessions dir.

    A file counts only when it is newer than ``since`` **and** carries the
    `[e2e-stub]` banner, so a genuine session written by the live tray while
    the gate runs is never mistaken for a leak. Stops after ``limit`` hits:
    the result names the breach, it is not a census — the caller must not
    report its length as a total. Returns an empty list when the directory
    doesn't exist or can't be scanned; a scan that cannot run proves nothing,
    and this check only ever accuses on positive evidence.
    """
    hits: List[Path] = []
    try:
        with os.scandir(sessions_dir) as entries:
            for entry in entries:
                if not entry.name.endswith(".transcript"):
                    continue
                try:
                    if entry.stat().st_mtime < since:
                        continue
                    with open(
                        entry.path, "r", encoding="utf-8", errors="replace"
                    ) as fh:
                        head = fh.read(4096)
                except OSError:
                    continue
                if _STUB_BANNER in head:
                    hits.append(Path(entry.path))
                    if len(hits) >= limit:
                        break
    except OSError:
        return hits
    return hits


def leaked_stub_uploads(
    uploads_dir: Path, since: float, *, limit: int = 5
) -> List[Path]:
    """Harness-written uploads that landed in the *real* `.launcher-tmp`.

    Marker-based like :func:`leaked_stub_sessions`, not a plain before/after
    count: the live tray shares this checkout, so a photo the user attaches
    from the phone while the gate runs writes here legitimately. A file counts
    only when it is newer than ``since`` **and** its name carries
    ``UPLOAD_MARKER``, which only this suite's uploads do. Stops after
    ``limit`` hits — the result names the breach, it is not a census. Returns
    an empty list when the directory doesn't exist or can't be scanned: a scan
    that cannot run proves nothing, and this check only accuses on positive
    evidence.
    """
    hits: List[Path] = []
    try:
        with os.scandir(uploads_dir) as entries:
            for entry in entries:
                if UPLOAD_MARKER not in entry.name:
                    continue
                try:
                    if entry.stat().st_mtime < since:
                        continue
                except OSError:
                    continue
                hits.append(Path(entry.path))
                if len(hits) >= limit:
                    break
    except OSError:
        return hits
    return hits
