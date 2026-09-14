"""``RemoteSession.submit_input`` — the detached-session send path (#967).

A detached session is a console the launcher tracks by PID only, so a
follow-up message is typed into it by a separate console-less helper
(``python -m src.console_input``). These tests pin the contract between
the session and that helper — argv shape, text on stdin, ``CREATE_NO_WINDOW``
— and the verdict vocabulary: ``delivered`` is ``"unconfirmed"`` on
success, never ``True``; the breadcrumb logs a size, never the text; an
unprobed agent is refused before anything is spawned; and
``PtySession.submit_input`` is not touched by any of it.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time

import pytest

from src import session_host
from src.session_host import RemoteInputOutcome, RemoteSession
from src.session_host_input import (
    DELIVERED_UNCONFIRMED,
    INPUT_CONSOLE_FAILED,
    INPUT_DROPPED,
    INPUT_NOOP,
    INPUT_UNVERIFIED,
    SUBMIT_NOT_REQUESTED,
    SUBMIT_NOT_SUBMITTED,
    SUBMIT_UNCONFIRMED,
)
from src.subprocess_flags import NO_WINDOW


class _Completed:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _remote(agent: str = "claude", alive: bool = True, monkeypatch=None) -> RemoteSession:
    session = RemoteSession(
        session_id="sid-remote-967",
        project_dir=r"E:\tmp",
        name="tmp",
        flags="",
        started_at=time.time(),
        pid=4321,
        agent=agent,
    )
    if monkeypatch is not None:
        monkeypatch.setattr(session_host, "is_pid_alive", lambda pid, started: alive)
    return session


def _capture_run(monkeypatch, completed: _Completed, calls: list) -> None:
    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return completed

    monkeypatch.setattr(session_host.subprocess, "run", fake_run)


def test_submit_spawns_helper_with_text_on_stdin_and_no_window(monkeypatch):
    calls: list = []
    _capture_run(monkeypatch, _Completed(stdout='{"ok": true, "units": 8, "submitted": true}\n'), calls)
    session = _remote(monkeypatch=monkeypatch)

    outcome = session.submit_input("continue", submit=True)

    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv == [sys.executable, "-m", "src.console_input", "4321"]
    # Text rides stdin — never argv, which is visible box-wide and
    # length-limited.
    assert kwargs["input"] == "continue"
    assert "continue" not in " ".join(argv)
    assert kwargs["creationflags"] & NO_WINDOW == NO_WINDOW
    assert kwargs["encoding"] == "utf-8"
    assert kwargs["cwd"] == str(session_host._REPO_ROOT)
    assert kwargs["timeout"] == session_host._CONSOLE_INPUT_TIMEOUT_S

    assert isinstance(outcome, RemoteInputOutcome)
    assert outcome.reason == INPUT_UNVERIFIED
    assert outcome.submitted is True
    assert outcome.units == 8


def test_success_verdict_is_unconfirmed_never_true(monkeypatch):
    _capture_run(monkeypatch, _Completed(stdout='{"ok": true, "units": 3, "submitted": true}'), [])
    session = _remote(monkeypatch=monkeypatch)

    api = session.submit_input("hey", submit=True).to_api()

    assert api["delivered"] == DELIVERED_UNCONFIRMED == "unconfirmed"
    assert api["delivered"] is not True
    assert api["submit_state"] == SUBMIT_UNCONFIRMED
    assert api["submitted"] is True
    assert api["submit_confirmed"] is None
    assert api["ingested"] is None
    assert api["reason"] == INPUT_UNVERIFIED
    assert api["units"] == 3
    # Same key set a PtySession verdict carries (plus units/error), so
    # every existing reader of last_input / the /input body keeps working.
    for key in ("delivered", "reason", "ingested", "submitted", "submit_confirmed",
                "submit_state", "waited_ms", "deferred"):
        assert key in api
    assert session.last_input == api
    assert session.to_api()["last_input"] == api


def test_no_submit_passes_flag_and_reports_not_requested(monkeypatch):
    calls: list = []
    _capture_run(monkeypatch, _Completed(stdout='{"ok": true, "units": 5, "submitted": false}'), calls)
    session = _remote(monkeypatch=monkeypatch)

    api = session.submit_input("draft", submit=False).to_api()

    assert calls[0][0][-1] == "--no-submit"
    assert api["submitted"] is False
    assert api["submit_state"] == SUBMIT_NOT_REQUESTED
    assert api["delivered"] == "unconfirmed"


def test_breadcrumb_logs_unit_count_never_the_text(monkeypatch, caplog):
    _capture_run(monkeypatch, _Completed(stdout='{"ok": true, "units": 26, "submitted": true}'), [])
    session = _remote(monkeypatch=monkeypatch)
    secret = "the-secret-steer-nobody-logs"

    with caplog.at_level("INFO", logger="src.session_host"):
        session.submit_input(secret, submit=True)

    text = caplog.text
    assert "26 units" in text
    assert "unconfirmed" in text
    assert secret not in text
    assert "secret" not in text


def test_unprobed_agent_is_refused_without_spawning(monkeypatch):
    calls: list = []
    _capture_run(monkeypatch, _Completed(stdout='{"ok": true}'), calls)
    session = _remote(agent="grok", monkeypatch=monkeypatch)

    outcome = session.submit_input("hello", submit=True)

    assert calls == []
    assert outcome.reason == INPUT_CONSOLE_FAILED
    assert "not probed" in outcome.error
    assert outcome.to_api()["delivered"] is False
    assert outcome.submit_state == SUBMIT_NOT_SUBMITTED


def test_dead_console_is_dropped_without_spawning(monkeypatch):
    calls: list = []
    _capture_run(monkeypatch, _Completed(stdout='{"ok": true}'), calls)
    session = _remote(alive=False, monkeypatch=monkeypatch)

    outcome = session.submit_input("hello", submit=True)

    assert calls == []
    assert outcome.reason == INPUT_DROPPED
    assert outcome.to_api()["delivered"] is False


def test_helper_failure_verdict_carries_its_error(monkeypatch):
    _capture_run(
        monkeypatch,
        _Completed(
            stdout='{"ok": false, "error": "AttachConsole(4321) failed: [5] Access is denied."}',
            returncode=1,
        ),
        [],
    )
    session = _remote(monkeypatch=monkeypatch)

    outcome = session.submit_input("hello", submit=True)

    assert outcome.reason == INPUT_CONSOLE_FAILED
    assert "AttachConsole(4321) failed" in outcome.error
    assert outcome.to_api()["delivered"] is False
    assert outcome.to_api()["error"] == outcome.error


def test_helper_crash_without_verdict_uses_stderr_tail(monkeypatch):
    _capture_run(
        monkeypatch,
        _Completed(stdout="", stderr="Traceback ...\nRuntimeError: boom", returncode=1),
        [],
    )
    session = _remote(monkeypatch=monkeypatch)

    outcome = session.submit_input("hello", submit=True)

    assert outcome.reason == INPUT_CONSOLE_FAILED
    assert outcome.error == "RuntimeError: boom"


def test_helper_timeout_is_a_console_failure(monkeypatch):
    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(session_host.subprocess, "run", fake_run)
    session = _remote(monkeypatch=monkeypatch)

    outcome = session.submit_input("hello", submit=True)

    assert outcome.reason == INPUT_CONSOLE_FAILED
    assert "timed out" in outcome.error


def test_blank_data_without_submit_is_a_noop(monkeypatch):
    calls: list = []
    _capture_run(monkeypatch, _Completed(), calls)
    session = _remote(monkeypatch=monkeypatch)

    outcome = session.submit_input("", submit=False)

    assert calls == []
    assert outcome.reason == INPUT_NOOP


def test_helper_module_stays_off_the_session_host_import_closure():
    # The whole point of shelling out (CLAUDE.md "## session-host"): a fix
    # to the helper must not need the :8446 restart. If session_host ever
    # imports it, tests/test_session_host_paths.py will also demand it be
    # declared — this pins the *intent* next to the code that relies on it.
    import ast
    from pathlib import Path

    tree = ast.parse(Path(session_host.__file__).read_text(encoding="utf-8"))
    imported = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    } | {
        alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert "src.console_input" not in imported
    assert session_host._CONSOLE_INPUT_MODULE == "src.console_input"


def test_pty_submit_input_is_untouched_by_the_remote_path():
    # Acceptance: nothing about PTY input changes. The PTY protocol still
    # comes from InputProtocol, and RemoteSession does not inherit it.
    from src.session_host import PtySession
    from src.session_host_input import InputProtocol

    assert PtySession.submit_input is InputProtocol.submit_input
    assert not issubclass(RemoteSession, InputProtocol)
    assert RemoteSession.submit_input is not InputProtocol.submit_input


@pytest.mark.parametrize("stdout", ["not json", "", "{}"])
def test_unparseable_helper_output_is_a_console_failure(monkeypatch, stdout):
    _capture_run(monkeypatch, _Completed(stdout=stdout, returncode=0), [])
    session = _remote(monkeypatch=monkeypatch)

    outcome = session.submit_input("hello", submit=True)

    assert outcome.reason == INPUT_CONSOLE_FAILED
    assert outcome.to_api()["delivered"] is False
    json.dumps(outcome.to_api())  # always serialisable for the route
