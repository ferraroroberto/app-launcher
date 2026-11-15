"""Telegram channel sessions start by themselves when the webapp starts (#1542).

A reboot kills every channel session, and the launcher comes back at log-on
without them, so each bot stays silent until someone starts it by hand. With
``autostart_channel_sessions`` on, the lifespan starts every ready profile once,
through the launch route's own body (lock + already-running refusal).

Unit-level: the session-host client, the spawn, the audit writer and the PC
mirror are faked, so no real session is ever started. Profile ids are invented.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.webapp import server
from app.webapp.routers import life_os_channels, life_os_spawn
from src import diagnostics, instance_role
from src.clients.session import SessionHostError
from src.webapp_config import SESSION_HOST_PORT_ENV

_LOGGER = life_os_channels.logger.name


def _ready_dir(tmp_path: Path, name: str, *, token: bool = True) -> str:
    d = tmp_path / name
    d.mkdir()
    if token:
        (d / ".env").write_text("TELEGRAM_BOT_TOKEN=x\n", encoding="utf-8")
    return d.as_posix()


@pytest.fixture
def boot(life_os_client, tmp_path, monkeypatch):
    """Two ready profiles, a recording spawn and a fake session-host list."""
    _client, app, _ = life_os_client
    monkeypatch.setenv("APP_LAUNCHER_DATA_DIR", (tmp_path / "data").as_posix())
    monkeypatch.setattr(life_os_channels, "_launch_locks", {})
    monkeypatch.setattr(life_os_channels, "AUTOSTART_HOST_WAIT_SECONDS", 0.2)
    monkeypatch.setattr(life_os_channels, "AUTOSTART_HOST_POLL_SECONDS", 0.01)
    monkeypatch.setattr(life_os_channels, "bun_available", lambda: True)
    monkeypatch.setattr(
        life_os_channels, "plugin_installed_for", lambda *a, **k: True
    )
    rows = [
        {"id": "garden", "label": "Garden", "skill": "journal-daily",
         "state_dir": _ready_dir(tmp_path, "tg-garden")},
        {"id": "kitchen", "label": "Kitchen", "skill": "journal-daily",
         "state_dir": _ready_dir(tmp_path, "tg-kitchen")},
    ]
    profiles_path = tmp_path / "profiles.json"

    def write_profiles(extra=()):
        profiles_path.write_text(
            json.dumps({"profiles": rows + list(extra)}), encoding="utf-8"
        )

    write_profiles()
    spawned: list = []
    live: list = []
    fail_for: set = set()

    def fake_spawn(project_dir, name, flags, port, kind, agent,
                   rows=40, cols=120, history_lines=None, label=""):
        if label in fail_for:
            raise RuntimeError("spawn blew up")
        spawned.append(SimpleNamespace(flags=flags, kind=kind, agent=agent,
                                       label=label))
        sid = f"sid-{len(spawned)}"
        live.append({"session_id": sid, "label": label, "name": name,
                     "alive": True})
        return {"session_id": sid, "kind": kind}

    mirrored: list = []
    monkeypatch.setattr(life_os_spawn, "spawn_claude_session", fake_spawn)
    monkeypatch.setattr(
        life_os_spawn, "open_local_terminal_window",
        lambda *a, **k: mirrored.append(a),
    )
    life_os_channels.session_client.list_sessions.side_effect = (
        lambda port: list(live)
    )
    cfg = app.state.webapp_config

    def run():
        return asyncio.run(life_os_channels.autostart_channel_sessions(
            cfg, profiles_path=profiles_path,
        ))

    return SimpleNamespace(
        run=run, cfg=cfg, spawned=spawned, live=live, mirrored=mirrored,
        fail_for=fail_for, write_profiles=write_profiles, tmp_path=tmp_path,
    )


def _messages(caplog) -> str:
    return "\n".join(
        r.getMessage() for r in caplog.records if r.name == _LOGGER
    )


class TestAutostart:
    def test_starts_every_ready_profile_once_in_a_fresh_skill_session(
        self, boot, caplog
    ):
        with caplog.at_level(logging.INFO, logger=_LOGGER):
            assert boot.run() == ["garden", "kitchen"]
        assert [s.label for s in boot.spawned] == [
            "telegram:garden", "telegram:kitchen"
        ]
        for spawn in boot.spawned:
            assert spawn.agent == "claude" and spawn.kind == "pty"
            # A fresh launch into the skill, never the /resume picker.
            assert spawn.flags.split()[-1] == "/journal-daily"
            assert f"--model {boot.cfg.channel_model}" in spawn.flags
        # Nobody asked from a browser: no PC mirror window, audited as such.
        assert boot.mirrored == []
        clients = {
            c.kwargs.get("client")
            for c in life_os_spawn.audit.audit_event.call_args_list
        }
        assert clients == {"startup"}
        log = _messages(caplog)
        assert "profile garden started in session sid-1" in log
        assert "profile kitchen started in session sid-2" in log

    def test_starts_on_the_channel_model_not_the_global_one(self, boot):
        boot.cfg.claude_model = "opus"
        assert boot.run() == ["garden", "kitchen"]
        for spawn in boot.spawned:
            assert "--model sonnet" in spawn.flags
            assert "--model opus" not in spawn.flags

    def test_a_configured_channel_model_is_used(self, boot):
        boot.cfg.channel_model = "opus"
        boot.cfg.claude_model = "sonnet"
        assert boot.run() == ["garden", "kitchen"]
        for spawn in boot.spawned:
            assert "--model opus" in spawn.flags

    def test_a_running_profile_is_left_alone(self, boot, caplog):
        boot.live.append({"session_id": "already-1", "label": "telegram:garden",
                          "name": "life-os", "alive": True})
        with caplog.at_level(logging.INFO, logger=_LOGGER):
            assert boot.run() == ["kitchen"]
        assert [s.label for s in boot.spawned] == ["telegram:kitchen"]
        assert "profile garden skipped" in _messages(caplog)

    def test_a_second_start_never_duplicates_a_session(self, boot):
        assert boot.run() == ["garden", "kitchen"]
        assert boot.run() == []
        assert len(boot.spawned) == 2

    def test_a_profile_failing_its_checks_is_skipped_and_the_rest_start(
        self, boot, caplog
    ):
        boot.write_profiles([
            {"id": "attic", "label": "Attic", "skill": "journal-daily",
             "state_dir": _ready_dir(boot.tmp_path, "tg-attic", token=False)},
            {"id": "cellar", "label": "Cellar", "skill": "no-such-skill",
             "state_dir": _ready_dir(boot.tmp_path, "tg-cellar")},
        ])
        with caplog.at_level(logging.INFO, logger=_LOGGER):
            assert boot.run() == ["garden", "kitchen"]
        log = _messages(caplog)
        assert "profile attic skipped: its token file (.env) is missing" in log
        assert "profile cellar skipped: its skill 'no-such-skill' was not found" in log

    def test_one_failing_launch_never_stops_the_others(self, boot, caplog):
        boot.fail_for.add("telegram:garden")
        with caplog.at_level(logging.INFO, logger=_LOGGER):
            assert boot.run() == ["kitchen"]
        assert "profile garden failed to start" in _messages(caplog)

    def test_an_unreachable_session_host_starts_nothing_and_does_not_raise(
        self, boot, caplog
    ):
        err = life_os_channels.session_client.SessionHostError
        life_os_channels.session_client.list_sessions.side_effect = err("down")
        with caplog.at_level(logging.INFO, logger=_LOGGER):
            assert boot.run() == []
        assert boot.spawned == []
        assert "session-host still unreachable" in _messages(caplog)

    def test_a_session_host_that_comes_up_late_is_waited_for(self, boot):
        err = life_os_channels.session_client.SessionHostError
        calls = {"n": 0}

        def flaky(port):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise err("booting")
            return list(boot.live)

        life_os_channels.session_client.list_sessions.side_effect = flaky
        assert boot.run() == ["garden", "kitchen"]

    def test_an_unconfirmed_plugin_install_starts_nothing(
        self, boot, monkeypatch, caplog
    ):
        # Unknown is not "installed": a start that cannot be confirmed waits.
        monkeypatch.setattr(
            life_os_channels, "plugin_installed_for", lambda *a, **k: None
        )
        with caplog.at_level(logging.INFO, logger=_LOGGER):
            assert boot.run() == []
        assert boot.spawned == []
        assert "install record could not be read" in _messages(caplog)


# ------------------------------------------------------------------ lifespan
def _app(**cfg_kw) -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(
        webapp_config=SimpleNamespace(jobs_coverage_interval_minutes=0, **cfg_kw)
    ))


async def _noop_async(*a, **k):
    return None


@pytest.fixture
def lifespan(monkeypatch):
    """Run the lifespan with every task body stubbed; return the autostart
    calls it made."""
    calls: list = []

    async def fake_autostart(app):
        calls.append(app)

    monkeypatch.delenv(instance_role.CANONICAL_OVERRIDE_ENV, raising=False)
    monkeypatch.setattr(server, "_reconcile_orphan_mirror_windows", _noop_async)
    monkeypatch.setattr(server, "_channel_autostart", fake_autostart)
    monkeypatch.setattr(server.jobs_snapshot, "tick_forever",
                        lambda: asyncio.sleep(3600))

    async def run(app):
        async with server._lifespan(app):
            await asyncio.sleep(0)  # let a created task take its first step
        return calls

    return run


def _canonical(monkeypatch):
    monkeypatch.delenv(SESSION_HOST_PORT_ENV, raising=False)
    monkeypatch.setattr(
        instance_role, "canonical_instance",
        lambda project_root=None: (True, instance_role.REASON_CANONICAL),
    )


class TestLifespanGate:
    def test_on_starts_the_autostart(self, lifespan, monkeypatch):
        _canonical(monkeypatch)
        app = _app(autostart_channel_sessions=True)
        assert asyncio.run(lifespan(app)) == [app]

    def test_off_starts_nothing(self, lifespan, monkeypatch, caplog):
        _canonical(monkeypatch)
        with caplog.at_level(logging.INFO, logger=server._log.name):
            assert asyncio.run(lifespan(_app(autostart_channel_sessions=False))) == []
        assert "autostart is off" in caplog.text

    @pytest.mark.parametrize("value", ["true", 1, None])
    def test_only_a_real_true_arms_it(self, lifespan, monkeypatch, value):
        _canonical(monkeypatch)
        assert asyncio.run(lifespan(_app(autostart_channel_sessions=value))) == []

    def test_a_config_without_the_key_starts_nothing(self, lifespan, monkeypatch):
        _canonical(monkeypatch)
        assert asyncio.run(lifespan(_app())) == []

    def test_a_disposable_instance_never_launches(self, lifespan, monkeypatch):
        # The gate's webapp talks to its own empty session-host: its
        # already-running check would miss every live bot.
        monkeypatch.setenv(SESSION_HOST_PORT_ENV, "54321")
        app = _app(autostart_channel_sessions=True)
        assert asyncio.run(lifespan(app)) == []


class TestTwoWebappProcesses:
    """A second webapp process has its own lock and a list taken before the
    first one's spawn registered; only the session-host's 409 stops it."""

    def test_the_second_pass_starts_nothing_and_says_why(
        self, boot, caplog, monkeypatch
    ):
        def host_spawn(project_dir, name, flags, port, kind, agent,
                       rows=40, cols=120, history_lines=None, label=""):
            if any(s["label"] == label for s in boot.live):
                raise SessionHostError(
                    f"{label} is live in session sid-1; one Telegram bot can "
                    "only be attached to one session.", status=409,
                )
            boot.live.append({"session_id": f"sid-{len(boot.live) + 1}",
                              "label": label, "name": name, "alive": True})
            boot.spawned.append(label)
            return {"session_id": boot.live[-1]["session_id"], "kind": kind}

        monkeypatch.setattr(life_os_spawn, "spawn_claude_session", host_spawn)
        life_os_channels.session_client.list_sessions.side_effect = (
            lambda port: []
        )
        with caplog.at_level(logging.INFO, logger=_LOGGER):
            assert boot.run() == ["garden", "kitchen"]
            life_os_channels._launch_locks.clear()  # the other process's locks
            assert boot.run() == []
        assert boot.spawned == ["telegram:garden", "telegram:kitchen"]
        assert "profile garden skipped: telegram:garden is live" in _messages(caplog)


class TestListenerOwnership:
    @pytest.fixture
    def own(self, monkeypatch):
        monkeypatch.setattr(server, "LISTENER_WAIT_SECONDS", 0.2)
        monkeypatch.setattr(server, "LISTENER_POLL_SECONDS", 0.01)
        views: list = []

        def listener_pids(port):
            return views.pop(0) if len(views) > 1 else views[0]

        monkeypatch.setattr(diagnostics, "listener_pids", listener_pids)
        return views

    def test_the_process_holding_the_port_runs_it(self, own):
        own.append([os.getpid()])
        assert asyncio.run(server._owns_listener(8445)) is True

    def test_waits_for_its_own_bind(self, own):
        own.extend([[], [], [os.getpid()]])
        assert asyncio.run(server._owns_listener(8445)) is True

    def test_another_process_holding_the_port_skips_at_once(self, own, caplog):
        own.append([os.getpid() + 1])
        with caplog.at_level(logging.INFO, logger=server._autostart_log.name):
            assert asyncio.run(server._owns_listener(8445)) is False
        assert f"held by pid {os.getpid() + 1}" in caplog.text

    def test_a_port_nobody_bound_is_not_confirmed(self, own, caplog):
        own.append([])
        with caplog.at_level(logging.INFO, logger=server._autostart_log.name):
            assert asyncio.run(server._owns_listener(8445)) is False
        assert "never bound" in caplog.text

    def test_an_unreadable_socket_table_is_not_confirmed(self, own, caplog):
        own.append(None)
        with caplog.at_level(logging.INFO, logger=server._autostart_log.name):
            assert asyncio.run(server._owns_listener(8445)) is False
        assert "could not be read" in caplog.text

    def test_a_non_owner_launches_nothing(self, own, monkeypatch):
        own.append([os.getpid() + 1])
        called: list = []

        async def autostart(*a, **k):
            called.append(a)

        monkeypatch.setattr(life_os_channels, "autostart_channel_sessions",
                            autostart)
        app = _app(autostart_channel_sessions=True)
        app.state.webapp_config.port = 8445
        asyncio.run(server._channel_autostart(app))
        assert called == []
        own[:] = [[os.getpid()]]
        asyncio.run(server._channel_autostart(app))
        assert len(called) == 1
