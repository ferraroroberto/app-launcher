"""app.tray.tray — the detached session-host gets a real log target (#825).

The session-host is spawned re-parented out of the tray's subtree and has no
console of its own. It used to be spawned with ``stdout``/``stderr`` pointed at
``DEVNULL``, while ``app/cli/main.py`` configures logging with a bare
``logging.basicConfig()`` — default handler, stderr. So every session-host log
line was discarded, including the WARNING ``run_git`` emits explaining exactly
why a build identity failed to resolve. That is how the live session-host ran
for 8 days reporting ``git_sha: "unknown"`` with the reason unrecoverable.

Same shape as ``project-scaffolding``'s tray-watchdog breadcrumb rule: a
``pythonw`` child with no real log target silently drops its own diagnostics.

Neither method touches ``self``, so they're exercised off the class directly —
see ``test_tray_session_host_port.py`` for why.
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock

import pytest

from app.tray import tray as tray_mod


@pytest.fixture
def _spawned(monkeypatch, tmp_path):
    """Drive ``_start_session_host`` with the port free, capturing the Popen kwargs."""
    log_path = tmp_path / "webapp" / "session-host.log"
    monkeypatch.setattr(tray_mod, "SESSION_HOST_LOG", log_path)
    monkeypatch.setattr(tray_mod.registered_trays, "port_listening", lambda _p: False)
    popen = MagicMock()
    monkeypatch.setattr(tray_mod.subprocess, "Popen", popen)

    tray_mod.TrayApp._start_session_host(MagicMock())

    assert popen.call_count == 1, "session-host should have been spawned"
    return popen.call_args, log_path


def test_session_host_stdout_is_a_real_file_not_devnull(_spawned):
    """The regression: DEVNULL here is what made the failure undiagnosable."""
    (_args, kwargs), log_path = _spawned

    assert kwargs["stdout"] is not subprocess.DEVNULL, "logs would go nowhere"
    assert kwargs["stderr"] is subprocess.STDOUT, "stderr must fold into the log"
    # A real writable handle on the declared path — this is where basicConfig's
    # stderr handler, and therefore run_git's WARNING, actually lands.
    assert hasattr(kwargs["stdout"], "write")
    assert log_path.exists()


def test_session_host_log_is_appended_not_truncated(_spawned, tmp_path):
    """A restart must not erase the previous boot's explanation."""
    (_args, _kwargs), log_path = _spawned
    assert log_path.read_text(encoding="utf-8") == ""

    log_path.write_text("earlier boot said why it failed\n", encoding="utf-8")
    # Re-spawn over the existing file.
    tray_mod.TrayApp._start_session_host(MagicMock())

    assert "earlier boot said why it failed" in log_path.read_text(encoding="utf-8")


def test_tray_does_not_pin_the_log_handle(_spawned):
    """The tray outlives the spawn, so it must close its own copy of the handle
    (the child inherited its own duplicate)."""
    (_args, kwargs), _log_path = _spawned
    assert kwargs["stdout"].closed, "tray still holds the session-host log open"


def test_unwritable_log_falls_back_to_devnull_and_still_spawns(monkeypatch, tmp_path):
    """Diagnostics must never cost the PTY host its start."""
    monkeypatch.setattr(tray_mod, "SESSION_HOST_LOG", tmp_path / "webapp" / "session-host.log")
    monkeypatch.setattr(tray_mod.registered_trays, "port_listening", lambda _p: False)

    def _boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr("builtins.open", _boom)
    popen = MagicMock()
    monkeypatch.setattr(tray_mod.subprocess, "Popen", popen)

    tray_mod.TrayApp._start_session_host(MagicMock())

    assert popen.call_count == 1, "a bad log target must not block the spawn"
    kwargs = popen.call_args[1]
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL
