"""``spawn_bat``'s command shape and the PID it hands back.

A launched app must outlive a launcher restart, which tree-kills the tray's
whole subtree (``taskkill /T``). So the app's ``cmd /k`` is re-parented through
``cmd /c start`` and the PID returned is that detached ``cmd``'s, found again
by its parent link. This pins what is easy to break and invisible when broken:

- both modes go through ``start`` (only re-parenting escapes ``/T``); stealth
  adds ``/b`` so no window opens, visible lets ``start`` open the console;
- the transient hop is hidden and never ``DETACHED_PROCESS`` (which does not
  escape ``/T`` and would only mislead);
- the PID is the detached ``cmd.exe``, not the exited hop, so Running apps can
  still walk its descendant tree to find the app's port; ``None`` when it
  cannot be found.

The real-process proof that the app survives a tree-kill of its launcher is
``test_spawn_bat_survives_tree_kill.py``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from src.launching import launcher
from src.subprocess_flags import NO_WINDOW


class _FakeProc:
    pid = 4242

    def wait(self, timeout=None):
        return 0


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


@pytest.fixture
def lookups(monkeypatch) -> list[tuple]:
    """Answer the detached-child lookup with pid 777, recording the query."""
    calls: list[tuple] = []

    def _find(parent_pid, name, created_after):
        calls.append((parent_pid, name, created_after))
        return 777

    monkeypatch.setattr(launcher, "find_child_process", _find)
    return calls


def test_missing_bat_raises_before_spawning(tmp_path: Path, spawns) -> None:
    with pytest.raises(OSError):
        launcher.spawn_bat(tmp_path / "nope.bat")
    assert spawns == []


def test_visible_reparents_through_start(bat: Path, spawns, lookups) -> None:
    launcher.spawn_bat(bat)
    assert spawns[0]["cmd"] == ["cmd", "/c", "start", "", "cmd", "/k", str(bat)]


def test_stealth_adds_b_so_no_window_opens(bat: Path, spawns, lookups) -> None:
    launcher.spawn_bat(bat, stealth=True)
    assert spawns[0]["cmd"] == ["cmd", "/c", "start", "", "/b", "cmd", "/k", str(bat)]


def test_both_modes_hide_the_hop_and_share_cwd(bat: Path, spawns, lookups) -> None:
    launcher.spawn_bat(bat)
    launcher.spawn_bat(bat, stealth=True)
    detached = getattr(subprocess, "DETACHED_PROCESS", 0)
    for spawn in spawns:
        assert spawn["creationflags"] == NO_WINDOW
        assert not (detached and spawn["creationflags"] & detached)
        assert spawn["cwd"] == str(bat.parent)
        assert spawn["shell"] is False


def test_returns_the_detached_cmd_not_the_hop(bat: Path, spawns, lookups) -> None:
    assert launcher.spawn_bat(bat) == 777
    parent_pid, name, _ = lookups[0]
    assert (parent_pid, name) == (_FakeProc.pid, "cmd.exe")


def test_none_when_the_detached_cmd_is_not_found(bat: Path, spawns, monkeypatch) -> None:
    monkeypatch.setattr(launcher, "find_child_process", lambda *a: None)
    assert launcher.spawn_bat(bat) is None


def test_a_wedged_hop_still_looks_for_the_child(bat: Path, monkeypatch, lookups) -> None:
    class _SlowProc(_FakeProc):
        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired("cmd", timeout)

    monkeypatch.setattr(launcher.subprocess, "Popen", lambda cmd, **kw: _SlowProc())
    assert launcher.spawn_bat(bat) == 777
