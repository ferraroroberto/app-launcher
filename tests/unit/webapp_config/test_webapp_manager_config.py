"""`WebappManagerConfig` wiring from config/config.json's `webapp` section (#687).

`load_config` used to read only `enabled` / `host` / `port`, so the three
timing knobs the dataclass advertises — and that `WebappManager` really
consumes at runtime — always took their dataclass defaults no matter what
the config file said. Raising `startup_timeout_seconds` to survive a slow
boot on a loaded box was a silent no-op.
"""

from __future__ import annotations

import pytest

from app.webapp.manager import WebappManager, WebappManagerConfig, load_config


class TestLoadConfig:
    def test_defaults_when_section_absent(self):
        cfg = load_config(None)
        assert cfg == WebappManagerConfig()

    def test_every_field_is_wired(self):
        cfg = load_config(
            {
                "enabled": False,
                "host": "127.0.0.1",
                "port": 9999,
                "startup_timeout_seconds": 45,
                "request_timeout_seconds": 2.5,
                "poll_interval_seconds": 1.25,
                "server": "uvicorn",
            }
        )
        assert cfg == WebappManagerConfig(
            enabled=False,
            host="127.0.0.1",
            port=9999,
            startup_timeout_seconds=45.0,
            request_timeout_seconds=2.5,
            poll_interval_seconds=1.25,
            server="uvicorn",
        )

    def test_partial_section_keeps_defaults_for_the_rest(self):
        cfg = load_config({"startup_timeout_seconds": 60})
        assert cfg.startup_timeout_seconds == 60.0
        assert cfg.request_timeout_seconds == WebappManagerConfig().request_timeout_seconds
        assert cfg.poll_interval_seconds == WebappManagerConfig().poll_interval_seconds
        assert cfg.port == WebappManagerConfig().port

    def test_server_defaults_to_hypercorn(self):
        assert load_config({}).server == "hypercorn"

    def test_unknown_server_fails_loud(self):
        with pytest.raises(ValueError, match="webapp.server"):
            load_config({"server": "uvicron"})


def test_launch_command_names_the_configured_server():
    """The tray's spawn carries the server, so the fallback is one config
    edit plus `tray.bat --restart`."""
    for server in ("hypercorn", "uvicorn"):
        cmd = WebappManager(WebappManagerConfig(server=server))._build_command()
        assert cmd[1:3] == ["-m", "app.webapp.serve"]
        assert cmd[cmd.index("--server") + 1] == server
