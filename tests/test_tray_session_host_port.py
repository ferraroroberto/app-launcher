"""app.tray.tray — session-host port resolution (issue #796).

``_start_session_host``/``_stop_session_host`` used to reference a
hardcoded ``SESSION_HOST_PORT = 8446`` module constant instead of the
user-editable ``session_host_port`` webapp-config setting — a third
definition alongside ``src/webapp_config.py``'s ``DEFAULT_SESSION_HOST_PORT``
and ``app/session_host/server.py``'s ``DEFAULT_PORT``. Setting the knob left
the tray adopting/reclaiming the wrong port and silently spawning a
duplicate session-host on every start.

Neither method touches ``self``, so they're exercised directly off the
class without constructing a full ``TrayApp`` (which would pull in
``WebappManager``, a real Windows named mutex via ``SingleInstance``, and
the tray icon).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.tray import tray as tray_mod

_CUSTOM_PORT = 19999


@pytest.fixture
def _custom_port(monkeypatch):
    """A non-default ``session_host_port``, proving the code actually reads
    the config instead of the old hardcoded 8446."""
    cfg = SimpleNamespace(session_host_port=_CUSTOM_PORT)
    monkeypatch.setattr(tray_mod, "load_webapp_config", lambda: cfg)
    return _CUSTOM_PORT


class TestSessionHostPort:
    def test_reads_from_webapp_config(self, _custom_port):
        assert tray_mod._session_host_port() == _custom_port


class TestStartSessionHostUsesConfiguredPort:
    def test_adopts_on_the_configured_port_not_the_default(
        self, _custom_port, monkeypatch
    ):
        seen_ports = []
        monkeypatch.setattr(
            tray_mod.registered_trays, "port_listening",
            lambda port: (seen_ports.append(port), True)[1],
        )
        tray_mod.TrayApp._start_session_host(None)
        assert seen_ports == [_custom_port]

    def test_spawns_on_the_configured_port_when_not_already_listening(
        self, _custom_port, monkeypatch
    ):
        monkeypatch.setattr(
            tray_mod.registered_trays, "port_listening", lambda port: False
        )
        popen = MagicMock()
        monkeypatch.setattr(tray_mod.subprocess, "Popen", popen)
        tray_mod.TrayApp._start_session_host(None)
        assert popen.called


class TestStopSessionHostUsesConfiguredPort:
    def test_noop_when_configured_port_not_listening(
        self, _custom_port, monkeypatch
    ):
        monkeypatch.setattr(
            tray_mod.registered_trays, "port_listening", lambda port: False
        )
        run = MagicMock()
        monkeypatch.setattr(tray_mod.subprocess, "run", run)
        tray_mod.TrayApp._stop_session_host(None)
        assert not run.called

    def test_reclaims_the_configured_port(
        self, _custom_port, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(
            tray_mod.registered_trays, "port_listening", lambda port: True
        )
        tray_dir = tmp_path / ".claude" / "tray"
        tray_dir.mkdir(parents=True)
        (tray_dir / "tray_lifecycle.ps1").write_text("", encoding="utf-8")
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        run = MagicMock(return_value=SimpleNamespace(returncode=0))
        monkeypatch.setattr(tray_mod.subprocess, "run", run)

        tray_mod.TrayApp._stop_session_host(None)

        assert run.called
        cmd = run.call_args.args[0]
        assert str(_custom_port) in cmd
