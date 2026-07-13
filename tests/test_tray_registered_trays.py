"""Registered Trays sequential boot orchestration (issue #456 part 2/2).

`_fleet_toml_port` is a pure function, tested directly. `TrayApp`'s
constructor wires a full WebappManager/HealthWatchdog — heavier than this
orchestration logic needs — so the three orchestration methods (none of
which touch instance state besides calling each other) are exercised via
`object.__new__(TrayApp)`, bypassing `__init__` entirely.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import app.tray.tray as tray_mod
from app.tray.tray import TrayApp, _fleet_toml_port
from src.registry import AppEntry, Registry


def _write_fleet_toml(repo_dir: Path, port_line: str) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / ".fleet.toml").write_text(
        f'layer = "enabling"\n{port_line}\n', encoding="utf-8"
    )


class TestFleetTomlPort:
    def test_bare_int_port(self, tmp_path: Path):
        _write_fleet_toml(tmp_path, "port = 8447")
        assert _fleet_toml_port(tmp_path) == 8447

    def test_leading_colon_string_port(self, tmp_path: Path):
        _write_fleet_toml(tmp_path, 'port = ":8445"')
        assert _fleet_toml_port(tmp_path) == 8445

    def test_missing_file_returns_none(self, tmp_path: Path):
        assert _fleet_toml_port(tmp_path) is None

    def test_missing_port_field_returns_none(self, tmp_path: Path):
        tmp_path.mkdir(parents=True, exist_ok=True)
        (tmp_path / ".fleet.toml").write_text('layer = "enabling"\n', encoding="utf-8")
        assert _fleet_toml_port(tmp_path) is None

    def test_malformed_string_port_returns_none(self, tmp_path: Path):
        _write_fleet_toml(tmp_path, 'port = "not-a-port"')
        assert _fleet_toml_port(tmp_path) is None

    def test_bool_port_returns_none(self, tmp_path: Path):
        """bool is an int subclass in Python — a stray `port = true` must
        not be silently treated as port 1."""
        _write_fleet_toml(tmp_path, "port = true")
        assert _fleet_toml_port(tmp_path) is None

    def test_malformed_toml_returns_none(self, tmp_path: Path):
        tmp_path.mkdir(parents=True, exist_ok=True)
        (tmp_path / ".fleet.toml").write_text("not { valid toml", encoding="utf-8")
        assert _fleet_toml_port(tmp_path) is None


def _bare_tray_app() -> TrayApp:
    """A TrayApp instance with __init__ skipped — the orchestration methods
    under test touch no instance state."""
    return object.__new__(TrayApp)


class TestLaunchRegisteredTrays:
    def test_no_autostart_entries_is_a_noop(self, monkeypatch, tmp_path: Path):
        app = _bare_tray_app()
        registry = Registry(
            scan_root=str(tmp_path),
            apps=[
                AppEntry(
                    id="a", name="A", kind="tray", bat_path=str(tmp_path / "tray.bat"),
                    autostart=False,
                ),
            ],
        )
        monkeypatch.setattr(tray_mod, "load_registry", lambda: registry)
        spawn = MagicMock()
        monkeypatch.setattr(TrayApp, "_spawn_tray_bat_detached", spawn)
        app._launch_registered_trays()
        spawn.assert_not_called()

    def test_launches_autostart_trays_in_registry_order(
        self, monkeypatch, tmp_path: Path
    ):
        bat_a = tmp_path / "repo-a" / "tray.bat"
        bat_b = tmp_path / "repo-b" / "tray.bat"
        for b in (bat_a, bat_b):
            b.parent.mkdir(parents=True, exist_ok=True)
            b.write_text("@echo off\r\n", encoding="utf-8")
        registry = Registry(
            scan_root=str(tmp_path),
            apps=[
                AppEntry(id="a", name="A", kind="tray", bat_path=str(bat_a), autostart=True),
                AppEntry(id="b", name="B", kind="tray", bat_path=str(bat_b), autostart=True),
                AppEntry(
                    id="c", name="C", kind="streamlit",
                    bat_path=str(tmp_path / "repo-c" / "run.bat"), autostart=True,
                ),
            ],
        )
        monkeypatch.setattr(tray_mod, "load_registry", lambda: registry)
        spawned: list = []
        monkeypatch.setattr(
            TrayApp, "_spawn_tray_bat_detached",
            lambda self, bat_path: spawned.append(bat_path),
        )
        monkeypatch.setattr(TrayApp, "_wait_for_tray_ready", lambda self, repo_dir: True)

        app = _bare_tray_app()
        app._launch_registered_trays()

        # Only the two kind=="tray" autostart entries, in registry order —
        # the streamlit row (wrong kind) is never touched even though its
        # autostart flag is also True.
        assert spawned == [bat_a, bat_b]

    def test_one_tray_failing_does_not_abort_the_rest(
        self, monkeypatch, tmp_path: Path
    ):
        bat_a = tmp_path / "repo-a" / "tray.bat"
        bat_b = tmp_path / "repo-b" / "tray.bat"
        for b in (bat_a, bat_b):
            b.parent.mkdir(parents=True, exist_ok=True)
            b.write_text("@echo off\r\n", encoding="utf-8")
        registry = Registry(
            scan_root=str(tmp_path),
            apps=[
                AppEntry(id="a", name="A", kind="tray", bat_path=str(bat_a), autostart=True),
                AppEntry(id="b", name="B", kind="tray", bat_path=str(bat_b), autostart=True),
            ],
        )
        monkeypatch.setattr(tray_mod, "load_registry", lambda: registry)

        def _spawn(self, bat_path):
            if bat_path == bat_a:
                raise OSError("boom")

        launched: list = []
        monkeypatch.setattr(TrayApp, "_spawn_tray_bat_detached", _spawn)
        monkeypatch.setattr(
            TrayApp, "_wait_for_tray_ready",
            lambda self, repo_dir: launched.append(repo_dir) or True,
        )

        app = _bare_tray_app()
        app._launch_registered_trays()

        # repo-a's spawn raised — its readiness wait is never reached — but
        # repo-b still launches.
        assert launched == [bat_b.parent]

    def test_missing_bat_path_is_skipped_not_fatal(self, monkeypatch, tmp_path: Path):
        registry = Registry(
            scan_root=str(tmp_path),
            apps=[
                AppEntry(id="a", name="A", kind="tray", bat_path=None, autostart=True),
            ],
        )
        monkeypatch.setattr(tray_mod, "load_registry", lambda: registry)
        spawn = MagicMock()
        monkeypatch.setattr(TrayApp, "_spawn_tray_bat_detached", spawn)
        app = _bare_tray_app()
        app._launch_registered_trays()  # must not raise
        spawn.assert_not_called()

    def test_nonexistent_bat_file_is_skipped_not_fatal(
        self, monkeypatch, tmp_path: Path
    ):
        registry = Registry(
            scan_root=str(tmp_path),
            apps=[
                AppEntry(
                    id="a", name="A", kind="tray",
                    bat_path=str(tmp_path / "does-not-exist" / "tray.bat"),
                    autostart=True,
                ),
            ],
        )
        monkeypatch.setattr(tray_mod, "load_registry", lambda: registry)
        spawn = MagicMock()
        monkeypatch.setattr(TrayApp, "_spawn_tray_bat_detached", spawn)
        app = _bare_tray_app()
        app._launch_registered_trays()
        spawn.assert_not_called()

    def test_registry_load_failure_does_not_raise(self, monkeypatch):
        def _boom():
            raise OSError("disk on fire")

        monkeypatch.setattr(tray_mod, "load_registry", _boom)
        app = _bare_tray_app()
        app._launch_registered_trays()  # must not raise


class TestWaitForTrayReady:
    def test_no_fleet_toml_falls_back_to_delay(self, monkeypatch, tmp_path: Path):
        sleeps: list = []
        monkeypatch.setattr(tray_mod.time, "sleep", lambda s: sleeps.append(s))
        app = _bare_tray_app()
        ready = app._wait_for_tray_ready(tmp_path)
        assert ready is False
        assert sleeps == [tray_mod._TRAY_FALLBACK_DELAY_S]

    def test_polls_until_port_listening(self, monkeypatch, tmp_path: Path):
        _write_fleet_toml(tmp_path, "port = 8447")
        calls = {"n": 0}

        def fake_port_listening(port):
            calls["n"] += 1
            assert port == 8447
            return calls["n"] >= 3

        monkeypatch.setattr(tray_mod, "_port_listening", fake_port_listening)
        monkeypatch.setattr(tray_mod.time, "sleep", lambda s: None)
        app = _bare_tray_app()
        assert app._wait_for_tray_ready(tmp_path) is True
        assert calls["n"] == 3

    def test_times_out_when_port_never_listens(self, monkeypatch, tmp_path: Path):
        _write_fleet_toml(tmp_path, "port = 8447")
        monkeypatch.setattr(tray_mod, "_port_listening", lambda port: False)
        # Simulate the timeout clock without a real 30s wall-clock wait.
        clock = {"t": 0.0}
        monkeypatch.setattr(tray_mod.time, "monotonic", lambda: clock["t"])

        def fake_sleep(s):
            clock["t"] += s

        monkeypatch.setattr(tray_mod.time, "sleep", fake_sleep)
        app = _bare_tray_app()
        assert app._wait_for_tray_ready(tmp_path) is False
