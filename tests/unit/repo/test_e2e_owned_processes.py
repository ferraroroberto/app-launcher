"""A crashed e2e worker takes its disposable servers with it (issue #2054).

pytest-timeout ends a stuck xdist worker with ``os._exit``, so the autoboot
fixture's teardown never runs. Its session-host (the venv launcher, the real
interpreter, and whatever that spawned) then outlived the gate and pinned the
checkout. These drive ``tests.e2e.fixtures.processes.spawn`` from a real
stand-in worker process and kill that worker hard, the way a timeout does.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import List

import psutil
import pytest

from tests.e2e._owned_processes import main, record_owned, sweep_ledger
from tests.e2e.fixtures.env import REPO_ROOT

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object")

# A server stand-in started through the fixture's own spawn(): it spawns one
# long-lived child the way the session-host spawns a PTY.
_SERVER = (
    "import subprocess, sys, time\n"
    "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
    "time.sleep(120)\n"
)

# The worker: boots the stand-in, waits until its whole tree exists (venv
# launcher -> interpreter -> its child -> that child's interpreter), prints
# every pid, then idles until it is killed.
_WORKER = (
    "import os, sys, time, psutil\n"
    "from tests.e2e.fixtures.processes import spawn\n"
    f"proc = spawn([sys.executable, '-c', {_SERVER!r}], open(os.devnull, 'w'))\n"
    "deadline = time.time() + 15\n"
    "while time.time() < deadline:\n"
    "    kids = psutil.Process(proc.pid).children(recursive=True)\n"
    "    if len(kids) >= 3:\n"
    "        break\n"
    "    time.sleep(0.2)\n"
    "print(' '.join(str(p) for p in [proc.pid] + [k.pid for k in kids]), flush=True)\n"
    "time.sleep(120)\n"
)

_FLAGS = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _kill_quietly(pids: List[int]) -> None:
    procs = []
    for pid in pids:
        try:
            proc = psutil.Process(pid)
            proc.kill()
            procs.append(proc)
        except psutil.Error:
            pass
    psutil.wait_procs(procs, timeout=5)


def _alive_after(pids: List[int], seconds: float) -> List[int]:
    deadline = time.time() + seconds
    alive = list(pids)
    while alive and time.time() < deadline:
        time.sleep(0.2)
        alive = [pid for pid in alive if psutil.pid_exists(pid)]
    return alive


def test_killing_the_worker_stops_its_server_tree() -> None:
    worker = subprocess.Popen(
        [sys.executable, "-c", _WORKER], cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE, text=True, creationflags=_FLAGS,
    )
    server_tree: List[int] = []
    try:
        server_tree = [int(pid) for pid in worker.stdout.readline().split()]
        assert len(server_tree) >= 4, f"stand-in server never built its tree: {server_tree}"
        worker.kill()
        worker.wait(timeout=5)
        alive = _alive_after(server_tree, 5)
        assert not alive, f"server processes outlived their worker: {alive}"
    finally:
        _kill_quietly(server_tree + [worker.pid])


def _sleeper() -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"], creationflags=_FLAGS,
    )


def test_sweep_kills_recorded_processes_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ledger = tmp_path / "pids.jsonl"
    monkeypatch.setenv("LAUNCHER_E2E_PID_LEDGER", str(ledger))
    recorded, unrecorded = _sleeper(), _sleeper()
    try:
        time.sleep(1)  # the venv launcher starts its interpreter as a child
        record_owned(recorded)
        # A recycled pid: same number, different creation time, never ours.
        with ledger.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"pid": unrecorded.pid, "create_time": 1.0}) + "\n")
        reaped, survivors = sweep_ledger(ledger)
        assert survivors == []
        assert len(reaped) >= 2  # the launcher and its interpreter
        assert recorded.wait(timeout=5) is not None
        assert unrecorded.poll() is None
    finally:
        _kill_quietly([recorded.pid, unrecorded.pid])


def test_sweep_without_a_ledger_is_unknown(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    assert main([str(tmp_path / "missing.jsonl")]) == 3
    assert capsys.readouterr().out.startswith("ORPHANS=unknown")
