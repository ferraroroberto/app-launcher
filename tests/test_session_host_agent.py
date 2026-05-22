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
