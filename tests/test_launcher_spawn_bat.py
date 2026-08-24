"""``spawn_bat`` picks its console flag per launch (issue #790).

The Apps/Trays rows offer two explicit launch modes — ⚡ opens a real CMD
window (unchanged behaviour), 🚫👁 runs the identical command line with no
window on screen. This pins the three things that are easy to break and
invisible when broken:

- the flags are the *right* ones, and are never OR'd together (they are
  mutually exclusive — ``CREATE_NEW_CONSOLE | CREATE_NO_WINDOW`` is not a
  hidden console, it is undefined behaviour);
- the command line and cwd stay identical between modes, so a stealth
  launch is the same spawn, not a second code path that can drift;
- ``cmd /k`` and the direct-child shape survive. ``app_runtime`` pairs the
  returned PID with ``listening_port_for_pid_tree``, which walks the
  *descendant* tree — that only works while ``cmd.exe`` stays a live child.
  Swapping in ``start`` or ``/c`` here would break port discovery for
  Running apps without failing any other test.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from src import launcher
from src.subprocess_flags import NO_WINDOW


class _FakeProc:
    pid = 4242


@pytest.fixture
def bat(tmp_path: Path) -> Path:
    path = tmp_path / "run.bat"
    path.write_text("@echo off\r\n", encoding="utf-8")
    return path


@pytest.fixture
def spawns(monkeypatch) -> list[dict]:
    captured: list[dict] = []

    def _fake_popen(cmd, **kwargs):
        captured.append({"cmd": cmd, **kwargs})
        return _FakeProc()

    monkeypatch.setattr(launcher.subprocess, "Popen", _fake_popen)
    return captured


def test_missing_bat_raises_before_spawning(tmp_path: Path, spawns) -> None:
    with pytest.raises(OSError):
        launcher.spawn_bat(tmp_path / "nope.bat")
    assert spawns == []


def test_visible_is_the_default(bat: Path, spawns) -> None:
    assert launcher.spawn_bat(bat) == 4242
    assert spawns[0]["creationflags"] == (
        subprocess.CREATE_NEW_CONSOLE if sys.platform == "win32" else 0
    )


@pytest.mark.skipif(sys.platform != "win32", reason="Windows console flags")
def test_stealth_swaps_in_no_window_and_never_ors(bat: Path, spawns) -> None:
    launcher.spawn_bat(bat, stealth=True)
    flags = spawns[0]["creationflags"]
    assert flags == NO_WINDOW
    assert not flags & subprocess.CREATE_NEW_CONSOLE, (
        "CREATE_NEW_CONSOLE and CREATE_NO_WINDOW are mutually exclusive"
    )


def test_both_modes_spawn_the_same_command_line(bat: Path, spawns) -> None:
    launcher.spawn_bat(bat)
    launcher.spawn_bat(bat, stealth=True)
    visible, stealth = spawns
    assert visible["cmd"] == stealth["cmd"] == ["cmd", "/k", str(bat)]
    assert visible["cwd"] == stealth["cwd"] == str(bat.parent)
    assert visible["shell"] is False and stealth["shell"] is False


def test_stealth_keeps_cmd_a_direct_child(bat: Path, spawns) -> None:
    """`start` / DETACHED_PROCESS would orphan the tree and break the
    port-discovery walk Running apps depends on."""
    launcher.spawn_bat(bat, stealth=True)
    spawn = spawns[0]
    assert "start" not in spawn["cmd"]
    detached = getattr(subprocess, "DETACHED_PROCESS", 0)
    if detached:
        assert not spawn["creationflags"] & detached
