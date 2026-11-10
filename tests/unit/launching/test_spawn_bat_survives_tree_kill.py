"""A launched app survives a tree-kill of the launcher that started it.

``tray.bat --restart`` ends the launcher with ``taskkill /T /F`` on the tray,
which walks the parent-child PID tree. An app spawned as a child of the webapp
died with it, and so did whatever it was in the middle of. This drives the
real ``spawn_bat`` from a stand-in launcher process, tree-kills that process
the way the restart does, and checks the app's long-running child keeps
writing its heartbeat. Windows only; uses a throwaway bat and child, never a
registered app.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from src.diagnostics import kill_process_tree
from src.subprocess_flags import NO_WINDOW

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows process tree")

_REPO_ROOT = Path(__file__).resolve().parents[3]
_STANDIN = """
import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from src.launching.launcher import spawn_bat
print(spawn_bat(Path(sys.argv[2]), stealth=True), flush=True)
time.sleep(120)
"""
_HEARTBEAT = """
import sys, time
while True:
    with open(sys.argv[1], "a") as f:
        f.write("beat\\n")
    time.sleep(0.2)
"""


def _beats(path: Path) -> int:
    try:
        return len(path.read_text().splitlines())
    except OSError:
        return 0


def _wait_for(check, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.2)
    return False


def test_launched_app_child_survives_a_tree_kill_of_its_launcher(tmp_path: Path) -> None:
    heartbeat = tmp_path / "hb.txt"
    (tmp_path / "beat.py").write_text(_HEARTBEAT, encoding="utf-8")
    (tmp_path / "standin.py").write_text(_STANDIN, encoding="utf-8")
    bat = tmp_path / "app.bat"
    bat.write_text(
        f'@echo off\r\n"{sys.executable}" "{tmp_path / "beat.py"}" "{heartbeat}"\r\n',
        encoding="utf-8",
    )
    standin = subprocess.Popen(
        [sys.executable, str(tmp_path / "standin.py"), str(_REPO_ROOT), str(bat)],
        stdout=subprocess.PIPE, text=True, creationflags=NO_WINDOW,
    )
    app_pid = None
    try:
        app_pid = int(standin.stdout.readline().strip())
        assert _wait_for(lambda: _beats(heartbeat) >= 2, 15), "the app never started"

        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(standin.pid)],
            capture_output=True, timeout=30, creationflags=NO_WINDOW,
        )
        standin.wait(timeout=10)
        after_kill = _beats(heartbeat)
        assert _wait_for(lambda: _beats(heartbeat) >= after_kill + 3, 5), (
            "the launched app's child died with its launcher's process tree"
        )
    finally:
        if standin.poll() is None:
            standin.kill()
        if app_pid:
            kill_process_tree(app_pid)
