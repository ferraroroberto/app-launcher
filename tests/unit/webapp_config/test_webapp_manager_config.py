"""`WebappManagerConfig` wiring from config/config.json's `webapp` section (#687).

`load_config` used to read only `enabled` / `host` / `port`, so the three
timing knobs the dataclass advertises — and that `WebappManager` really
consumes at runtime — always took their dataclass defaults no matter what
the config file said. Raising `startup_timeout_seconds` to survive a slow
boot on a loaded box was a silent no-op.
"""

from __future__ import annotations

from app.webapp.manager import WebappManagerConfig, load_config


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
            }
        )
        assert cfg == WebappManagerConfig(
            enabled=False,
            host="127.0.0.1",
            port=9999,
            startup_timeout_seconds=45.0,
            request_timeout_seconds=2.5,
            poll_interval_seconds=1.25,
        )

    def test_partial_section_keeps_defaults_for_the_rest(self):
        cfg = load_config({"startup_timeout_seconds": 60})
        assert cfg.startup_timeout_seconds == 60.0
        assert cfg.request_timeout_seconds == WebappManagerConfig().request_timeout_seconds
        assert cfg.poll_interval_seconds == WebappManagerConfig().poll_interval_seconds
        assert cfg.port == WebappManagerConfig().port
