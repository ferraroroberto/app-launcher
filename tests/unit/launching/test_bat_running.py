"""src.launching.bat_running: is what a bat starts already running?

The boot autostart walk uses it so a tray restart never starts a second copy
of a mini app. Real child processes carry the command lines, so the match is
proved against the process table, not a stub.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path, PurePosixPath

import pytest

from src.launching import bat_running
from src.subprocess_flags import NO_WINDOW

_SLEEP_SCRIPT = "import time\ntime.sleep(60)\n"


@pytest.fixture
def sleeper(tmp_path: Path):
    """A live python process whose command line names ``worker.py`` by path."""
    script = tmp_path / "worker.py"
    script.write_text(_SLEEP_SCRIPT, encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, str(script)], cwd=str(tmp_path), creationflags=NO_WINDOW
    )
    try:
        yield proc
    finally:
        proc.kill()
        proc.wait(timeout=10)


def _bat(tmp_path: Path, body: str, name: str = "mini.bat") -> Path:
    bat = tmp_path / name
    bat.write_text(body, encoding="utf-8")
    return bat


@pytest.mark.windows_only
def test_target_paths_resolve_dp0_relative_and_skip_interpreters(tmp_path: Path):
    bat = _bat(
        tmp_path,
        '@echo off\r\nstart "" pythonw "%~dp0worker.py" --flag\r\n'
        "python sub\\other.py\r\ncmd /c timeout.exe 5\r\n",
    )
    targets = bat_running.target_paths(bat)
    assert bat_running._norm(str(tmp_path / "worker.py")) in targets
    assert bat_running._norm(str(tmp_path / "sub" / "other.py")) in targets
    # An interpreter or system program says nothing about this bat.
    assert not any(t.endswith("timeout.exe") or t.endswith("pythonw.exe") for t in targets)


def test_target_with_an_unresolvable_variable_is_left_out(tmp_path: Path):
    bat = _bat(tmp_path, "python %TOOLS%\\run.py\r\n")
    assert bat_running.target_paths(bat) == set()


def test_finds_a_process_running_the_script_the_bat_names(tmp_path: Path, sleeper):
    bat = _bat(tmp_path, '@echo off\r\npython "%~dp0worker.py"\r\n')
    assert bat_running.find_running_target(bat) == sleeper.pid


def test_finds_a_script_named_relative_to_the_process_cwd(tmp_path: Path):
    script = tmp_path / "rel_worker.py"
    script.write_text(_SLEEP_SCRIPT, encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "rel_worker.py"], cwd=str(tmp_path), creationflags=NO_WINDOW
    )
    try:
        bat = _bat(tmp_path, "python rel_worker.py\r\n")
        assert bat_running.find_running_target(bat) == proc.pid
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_nothing_running_is_none(tmp_path: Path):
    bat = _bat(tmp_path, '@echo off\r\npython "%~dp0never_started_worker.py"\r\n')
    assert bat_running.find_running_target(bat) is None


def test_a_same_named_script_elsewhere_does_not_match(tmp_path: Path, sleeper):
    other = tmp_path / "other"
    other.mkdir()
    bat = _bat(other, '@echo off\r\npython "%~dp0worker.py"\r\n')
    assert bat_running.find_running_target(bat) is None


def test_an_unreadable_process_table_is_unknown_not_not_running(tmp_path: Path, monkeypatch):
    bat = _bat(tmp_path, "python worker.py\r\n")

    def _boom(*_a, **_k):
        raise OSError("no table")

    monkeypatch.setattr(bat_running.psutil, "process_iter", _boom)
    with pytest.raises(RuntimeError):
        bat_running.find_running_target(bat)


def test_missing_psutil_is_unknown(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(bat_running, "psutil", None)
    with pytest.raises(RuntimeError):
        bat_running.find_running_target(_bat(tmp_path, "echo hi\r\n"))


def test_a_backslash_token_is_matched_against_generic_programs_on_posix(
    tmp_path: Path, monkeypatch
):
    # POSIX reads ``tools\pythonw.exe`` as one file name, so its stem keeps the
    # folder and the generic-program filter misses it. Running the stem through
    # the POSIX path class proves the separator is normalised on any OS.
    monkeypatch.setattr(bat_running, "Path", PurePosixPath)
    bat = _bat(tmp_path, "start tools\\pythonw.exe\r\npython sub\\other.py\r\n")
    targets = bat_running.target_paths(bat)
    monkeypatch.undo()
    assert not any(t.endswith("pythonw.exe") for t in targets)
    assert bat_running._norm(str(tmp_path / "sub" / "other.py")) in targets


def test_a_backslash_token_is_joined_with_forward_slashes(tmp_path: Path, monkeypatch):
    joined: list[str] = []
    original = bat_running.os.path.join

    def record(*parts):
        joined.extend(str(part) for part in parts)
        return original(*parts)

    monkeypatch.setattr(bat_running.os.path, "join", record)
    bat = _bat(tmp_path, "python sub\\other.py\r\n")
    bat_running.target_paths(bat)
    monkeypatch.undo()
    assert "sub/other.py" in joined
    assert "sub\\other.py" not in joined
