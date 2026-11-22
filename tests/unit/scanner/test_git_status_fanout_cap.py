"""The git-status scan never has more than two git processes alive at once.

Six synthetic repos, every ``git`` spawn faked with an injected delay that
records how many are in flight; the real scan path (`_scan_git_status`) fans
them out. No real git runs.
"""

from __future__ import annotations

import asyncio
import subprocess
import threading
import time

from app.webapp.routers import claude_code
from src import git_utils


def test_the_scan_caps_live_git_processes_at_two(tmp_path, monkeypatch):
    for n in range(6):
        (tmp_path / f"repo{n}" / ".git").mkdir(parents=True)
    lock = threading.Lock()
    live = {"now": 0, "max": 0, "spawns": 0}

    def fake_run(argv, **_kw):
        with lock:
            live["now"] += 1
            live["spawns"] += 1
            live["max"] = max(live["max"], live["now"])
        time.sleep(0.02)  # the injected delay that lets spawns overlap
        with lock:
            live["now"] -= 1
        out = "# branch.head main\n" if "status" in argv else ""
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    monkeypatch.setattr(git_utils.subprocess, "run", fake_run)
    monkeypatch.setattr(claude_code, "_registered_app_dirs", lambda: ())
    payload = asyncio.run(claude_code._scan_git_status(str(tmp_path), (), {}))

    assert len(payload["projects"]) == 6
    assert live["spawns"] >= 6
    assert live["max"] == 2, f"{live['max']} git processes alive at once; the cap is two"
    assert claude_code.GIT_STATUS_MAX_CONCURRENT_GIT == 2
