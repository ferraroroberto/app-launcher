"""SessionManager spawns the per-agent command (issue #45).

``create_remote`` is used for the spawn-command checks because it goes
through ``subprocess.Popen`` (easily stubbed) and starts no reader
thread. ``PtySession.to_api`` is exercised directly for the ``agent``
field.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

from src.session_host import PtySession, SessionManager


class _FakeProc:
    pid = 4321

    def poll(self):
        return None


def test_create_remote_uses_antigravity_command(tmp_path, monkeypatch):
    captured: dict = {}

    def fake_popen(command, **kwargs):
        captured["command"] = command
        return _FakeProc()

    from src import session_host
    monkeypatch.setattr(session_host.subprocess, "Popen", fake_popen)

    mgr = SessionManager()
    session = mgr.create_remote(str(tmp_path), "proj", "", "antigravity")

    assert captured["command"].startswith("cmd /c agy")
    assert session.agent == "antigravity"
    assert session.to_api()["agent"] == "antigravity"


def test_create_remote_uses_copilot_command(tmp_path, monkeypatch):
    captured: dict = {}

    def fake_popen(command, **kwargs):
        captured["command"] = command
        return _FakeProc()

    from src import session_host
    monkeypatch.setattr(session_host.subprocess, "Popen", fake_popen)

    mgr = SessionManager()
    session = mgr.create_remote(str(tmp_path), "proj", "", "copilot")

    assert captured["command"].startswith("cmd /c copilot")
    assert session.agent == "copilot"
    assert session.to_api()["agent"] == "copilot"


def test_create_remote_defaults_to_claude(tmp_path, monkeypatch):
    captured: dict = {}

    def fake_popen(command, **kwargs):
        captured["command"] = command
        return _FakeProc()

    from src import session_host
    monkeypatch.setattr(session_host.subprocess, "Popen", fake_popen)

    mgr = SessionManager()
    session = mgr.create_remote(str(tmp_path), "proj", "")

    assert captured["command"].startswith("cmd /c claude")
    assert session.agent == "claude"


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
