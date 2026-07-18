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
from src.vt_snapshot import VtSnapshot


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

    assert "&& agy" in captured["argv"][-1]
    assert session.agent == "antigravity"
    assert session.to_api()["agent"] == "antigravity"


def test_create_remote_uses_copilot_command(tmp_path, monkeypatch):
    captured: dict = {}

    from src import session_host
    monkeypatch.setattr(session_host.subprocess, "run", _capture_run(captured))

    mgr = SessionManager()
    session = mgr.create_remote(str(tmp_path), "proj", "", "copilot")

    assert "&& copilot" in captured["argv"][-1]
    assert session.agent == "copilot"
    assert session.to_api()["agent"] == "copilot"


def test_create_remote_defaults_to_claude(tmp_path, monkeypatch):
    captured: dict = {}

    from src import session_host
    monkeypatch.setattr(session_host.subprocess, "run", _capture_run(captured))

    mgr = SessionManager()
    session = mgr.create_remote(str(tmp_path), "proj", "")

    assert "&& claude" in captured["argv"][-1]
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
    assert 'set "APP_LAUNCHER_SESSION_ID=' in ps_command
    assert 'set "APP_LAUNCHER_AGENT=claude"' in ps_command
    assert "&& claude --foo" in ps_command
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
        def spawn(command, cwd=None, dimensions=None, env=None):
            capture["command"] = command
            capture["dimensions"] = dimensions
            capture["env"] = env
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
    # APP_LAUNCHER_SESSION_ID/AGENT ride the child's real environment (#537),
    # not a `set "VAR=val" && ...` chain baked into the command string —
    # PtyProcess.spawn() re-tokenizes a str command via shlex.split() then
    # rebuilds it with subprocess.list2cmdline(), which backslash-escapes
    # embedded quotes and silently broke that chain.
    assert captured["env"]["APP_LAUNCHER_SESSION_ID"] == session.session_id
    assert captured["env"]["APP_LAUNCHER_AGENT"] == "codex"
    assert "APP_LAUNCHER_SESSION_ID" not in captured["command"]
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


def test_create_attaches_vt_snapshot_for_fullscreen_agent(tmp_path, monkeypatch):
    """Codex (fullscreen) gets a headless VT mirror so a (re)connect can be
    served a current-frame snapshot instead of a resize-triggered
    re-emission (issue #432); Claude (inline, raw-ring replay) needs none."""
    captured: dict = {}
    from src import session_host

    monkeypatch.setattr(session_host, "PtyProcess", _fake_pty_process(captured))
    monkeypatch.setattr(session_host.PtySession, "start_reader", lambda self: None)

    mgr = SessionManager()
    mgr.attach_loop(MagicMock())

    codex_session = mgr.create(str(tmp_path), "proj", "", "codex", rows=30, cols=90)
    assert isinstance(codex_session._vt, VtSnapshot)
    assert codex_session._vt._screen.lines == 30
    assert codex_session._vt._screen.columns == 90

    claude_session = mgr.create(str(tmp_path), "proj", "", "claude")
    assert claude_session._vt is None


def test_create_threads_history_lines_override_to_vt_snapshot(tmp_path, monkeypatch):
    """The Settings-tab-configurable scrollback depth (issue #435
    follow-up) reaches the VT mirror's own bounded history cap; omitted
    falls back to VtSnapshot's own default (pinned separately)."""
    captured: dict = {}
    from src import session_host

    monkeypatch.setattr(session_host, "PtyProcess", _fake_pty_process(captured))
    monkeypatch.setattr(session_host.PtySession, "start_reader", lambda self: None)

    mgr = SessionManager()
    mgr.attach_loop(MagicMock())

    session = mgr.create(
        str(tmp_path), "proj", "", "codex", rows=30, cols=90, history_lines=42,
    )
    assert session._vt._screen.history.size == 42
    assert session._vt._screen.history.top.maxlen == 42

    default_session = mgr.create(str(tmp_path), "proj", "", "codex", rows=30, cols=90)
    assert default_session._vt._screen.history.size != 42


def test_resize_forwards_to_vt_snapshot():
    session = PtySession(
        session_id="sid-test",
        project_dir=r"C:\stub",
        name="proj",
        flags="",
        started_at=time.time(),
        _loop=MagicMock(),
        _pty=MagicMock(),
        agent="codex",
        _vt=VtSnapshot(40, 120),
    )
    session.resize(20, 60)
    assert session._vt._screen.lines == 20
    assert session._vt._screen.columns == 60


def test_snapshot_frame_none_without_vt():
    session = PtySession(
        session_id="sid-test",
        project_dir=r"C:\stub",
        name="proj",
        flags="",
        started_at=time.time(),
        _loop=MagicMock(),
        _pty=MagicMock(),
        agent="claude",
    )
    assert session.snapshot_frame() is None


def test_snapshot_frame_renders_current_vt_screen():
    session = PtySession(
        session_id="sid-test",
        project_dir=r"C:\stub",
        name="proj",
        flags="",
        started_at=time.time(),
        _loop=MagicMock(),
        _pty=MagicMock(),
        agent="codex",
        _vt=VtSnapshot(10, 40),
    )
    session._vt.feed("current frame text")
    assert "current frame text" in session.snapshot_frame()


class _OneShotPty:
    """A fake PTY that yields one chunk, then EOFs — enough to drive
    ``_read_loop`` synchronously without a background thread."""

    def __init__(self, chunk: str) -> None:
        self._chunk = chunk
        self._sent = False

    def read(self, _n):
        if not self._sent:
            self._sent = True
            return self._chunk
        raise EOFError

    def isalive(self):
        return False


def test_read_loop_feeds_vt_snapshot():
    """The reader thread's chunk pipeline must feed the VT mirror alongside
    the raw scrollback ring — that's the only source of truth a snapshot
    render has for the agent's current frame."""
    session = PtySession(
        session_id="sid-test",
        project_dir=r"C:\stub",
        name="proj",
        flags="",
        started_at=time.time(),
        _loop=MagicMock(),
        _pty=_OneShotPty("agent output here\r\n"),
        agent="codex",
        _vt=VtSnapshot(24, 80),
    )
    session._read_loop()
    assert "agent output here" in session.snapshot_frame()


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


def test_create_threads_label_to_api(tmp_path, monkeypatch):
    """The role tag (#245, e.g. the fleet chief) rides create() → to_api()
    so callers can find a purpose-built session deterministically; omitted
    stays the empty string."""
    captured: dict = {}
    from src import session_host

    monkeypatch.setattr(session_host, "PtyProcess", _fake_pty_process(captured))
    monkeypatch.setattr(session_host.PtySession, "start_reader", lambda self: None)

    mgr = SessionManager()
    mgr.attach_loop(MagicMock())

    chief = mgr.create(str(tmp_path), "chief", "", "claude", label="chief")
    assert chief.to_api()["label"] == "chief"

    plain = mgr.create(str(tmp_path), "proj", "", "claude")
    assert plain.to_api()["label"] == ""


def test_pty_session_to_api_reports_output_chars():
    """The board's dispatch readiness probe (#302) reads ``output_chars`` —
    0 before the agent paints anything, the scrollback ring's length after."""
    session = PtySession(
        session_id="sid-test",
        project_dir=r"C:\stub",
        name="proj",
        flags="",
        started_at=time.time(),
        _loop=MagicMock(),
        _pty=MagicMock(),
    )
    assert session.to_api()["output_chars"] == 0
    session._ring = "hello from the agent"
    assert session.to_api()["output_chars"] == len("hello from the agent")
