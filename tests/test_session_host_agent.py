"""SessionManager spawns the per-agent command (issue #45).

``create_remote`` is used for the spawn-command checks because it goes
through a single ``subprocess.run`` (easily stubbed) and starts no reader
thread. Since issue #130 the detached console is launched *orphaned* via a
transient PowerShell ``Start-Process`` (so a ``tray.bat --restart`` cannot
cascade into it), and the per-agent command appears inside that PowerShell
``-Command`` string. ``PtySession.to_api`` is exercised directly for the
``agent`` field.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from src.session_host import PtySession, RemoteSession, SessionManager


class _FakeCompleted:
    """Stand-in for the ``Start-Process -PassThru`` call result."""

    def __init__(self, stdout: str = "4321\n", stderr: str = "") -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = 0


def _capture_run(captured: dict):
    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        return _FakeCompleted()

    return fake_run


def test_create_remote_uses_antigravity_command(tmp_path, monkeypatch):
    captured: dict = {}

    from src import session_host
    monkeypatch.setattr(session_host.subprocess, "run", _capture_run(captured))

    mgr = SessionManager()
    session = mgr.create_remote(str(tmp_path), "proj", "", "antigravity")

    assert "/c agy" in captured["argv"][-1]
    assert session.agent == "antigravity"
    assert session.to_api()["agent"] == "antigravity"


def test_create_remote_uses_copilot_command(tmp_path, monkeypatch):
    captured: dict = {}

    from src import session_host
    monkeypatch.setattr(session_host.subprocess, "run", _capture_run(captured))

    mgr = SessionManager()
    session = mgr.create_remote(str(tmp_path), "proj", "", "copilot")

    assert "/c copilot" in captured["argv"][-1]
    assert session.agent == "copilot"
    assert session.to_api()["agent"] == "copilot"


def test_create_remote_defaults_to_claude(tmp_path, monkeypatch):
    captured: dict = {}

    from src import session_host
    monkeypatch.setattr(session_host.subprocess, "run", _capture_run(captured))

    mgr = SessionManager()
    session = mgr.create_remote(str(tmp_path), "proj", "")

    assert "/c claude" in captured["argv"][-1]
    assert session.agent == "claude"


def test_create_remote_orphans_console_via_start_process(tmp_path, monkeypatch):
    """#130: the detached console must be spawned *orphaned* (PowerShell
    ``Start-Process``), never as a ``CREATE_NEW_CONSOLE`` child of the host —
    otherwise a ``taskkill /T`` on the tray subtree cascades into it and the
    session that was meant to outlive a restart dies."""
    captured: dict = {}
    from src import session_host

    def boom_popen(*args, **kwargs):
        raise AssertionError("create_remote must not Popen the console directly")

    monkeypatch.setattr(session_host.subprocess, "Popen", boom_popen)
    monkeypatch.setattr(session_host.subprocess, "run", _capture_run(captured))

    mgr = SessionManager()
    session = mgr.create_remote(str(tmp_path), "proj", "--foo")

    argv = captured["argv"]
    assert argv[0].lower().endswith("powershell.exe")
    ps_command = argv[-1]
    assert "Start-Process" in ps_command and "-PassThru" in ps_command
    assert "/c claude --foo" in ps_command
    assert session._pid == 4321


def test_create_remote_raises_when_no_pid(tmp_path, monkeypatch):
    """A spawn that prints no PID surfaces a clear error instead of a session
    tracking a bogus process."""
    from src import session_host
    monkeypatch.setattr(
        session_host.subprocess,
        "run",
        lambda *a, **k: _FakeCompleted(stdout="", stderr="boom"),
    )

    mgr = SessionManager()
    with pytest.raises(RuntimeError):
        mgr.create_remote(str(tmp_path), "proj", "")


def test_remote_stop_taskkills_by_pid(monkeypatch):
    """An explicit Stop still reaches the orphaned console by its own PID."""
    from src import session_host
    calls: dict = {}

    monkeypatch.setattr(session_host, "_pid_alive", lambda pid: True)

    def fake_run(argv, **kwargs):
        calls["argv"] = argv
        return _FakeCompleted()

    monkeypatch.setattr(session_host.subprocess, "run", fake_run)

    session = RemoteSession(
        session_id="sid",
        project_dir=r"C:\stub",
        name="proj",
        flags="",
        started_at=time.time(),
        pid=9999,
    )
    session.stop()

    assert calls["argv"] == ["taskkill", "/PID", "9999", "/T", "/F"]


def _fake_pty_process(capture: dict):
    """A stand-in for ``winpty.PtyProcess`` whose ``spawn`` records the
    dimensions and returns a dead pty (so the reader loop exits at once)."""

    class _FakePty:
        def isalive(self):
            return False

    class _FakePtyProcess:
        @staticmethod
        def spawn(command, cwd=None, dimensions=None):
            capture["command"] = command
            capture["dimensions"] = dimensions
            return _FakePty()

    return _FakePtyProcess


def test_create_spawns_pty_at_given_dimensions(tmp_path, monkeypatch):
    captured: dict = {}
    from src import session_host

    monkeypatch.setattr(session_host, "PtyProcess", _fake_pty_process(captured))
    # Skip the reader thread + transcript file — we only assert spawn args.
    monkeypatch.setattr(session_host.PtySession, "start_reader", lambda self: None)

    mgr = SessionManager()
    mgr.attach_loop(MagicMock())
    session = mgr.create(str(tmp_path), "proj", "", "codex", rows=55, cols=42)

    assert captured["dimensions"] == (55, 42)
    assert session.rows == 55 and session.cols == 42
    assert session.to_api()["rows"] == 55 and session.to_api()["cols"] == 42


def test_create_defaults_and_clamps_dimensions(tmp_path, monkeypatch):
    captured: dict = {}
    from src import session_host

    monkeypatch.setattr(session_host, "PtyProcess", _fake_pty_process(captured))
    monkeypatch.setattr(session_host.PtySession, "start_reader", lambda self: None)

    mgr = SessionManager()
    mgr.attach_loop(MagicMock())

    # Omitted → legacy 40×120.
    mgr.create(str(tmp_path), "proj", "")
    assert captured["dimensions"] == (40, 120)

    # Out-of-range values clamp to the same 1..1000 bounds as resize().
    mgr.create(str(tmp_path), "proj", "", rows=99999, cols=0)
    assert captured["dimensions"] == (1000, 1)


def test_pty_session_to_api_carries_agent():
    session = PtySession(
        session_id="sid-test",
        project_dir=r"C:\stub",
        name="proj",
        flags="",
        started_at=time.time(),
        _loop=MagicMock(),
        _pty=MagicMock(),
        agent="antigravity",
    )
    assert session.to_api()["agent"] == "antigravity"
