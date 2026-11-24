"""A running Telegram channel session whose plugin server is gone: detected,
restarted once, then flagged for the owner. Fake process tables and a fake
session-host only; no real process or session is touched."""

from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace

import pytest

from app.webapp import channel_heal
from app.webapp.routers import life_os_channels
from src.launching import channel_health
from src.launching.channel_health import ProcRow
from src.launching.channel_profiles import load_channel_profiles
from src.webapp_config import WebappConfig

T0 = 1_000_000.0
OLD = T0 - 3600  # a session started long before: past the startup grace


def _claude(pid: int, profile_id: str) -> ProcRow:
    settings = f"C:/data/app-launcher/channel-settings/{profile_id}.json"
    return ProcRow(pid, 1, "claude.exe", ("claude", "--settings", settings, "/health"))


def _bun(pid: int, parent: int) -> ProcRow:
    return ProcRow(pid, parent, "bun.exe", ("bun", "server.ts"))


def _session(started_at: float = OLD) -> dict:
    return {"session_id": "sid-1", "label": "telegram:health", "alive": True,
            "started_at": started_at}


class TestDetection:
    def test_server_states_follow_the_process_tree(self):
        rows = [_claude(10, "health"), ProcRow(11, 10, "cmd.exe", ()), _bun(12, 11),
                _claude(20, "school"), _claude(30, "other")]
        states = channel_health.server_states(["health", "school", "job"], rows)
        assert states == {"health": True, "school": False, "job": None}

    def test_assess_never_folds_an_unknown_into_ok_or_degraded(self):
        assess = channel_health.assess
        assert assess(None, False, True, T0) == (None, "")
        assert assess(_session(), True, True, T0) == ("ok", "")
        assert assess(_session(), False, True, T0) == (
            "degraded", channel_health.REASON_NO_SERVER)
        assert assess(_session(T0 - 5), False, True, T0)[0] == "starting"
        assert assess(_session(), None, True, T0)[0] == "unknown"
        assert assess(_session(), None, False, T0) == ("unknown", channel_health.REASON_NO_SCAN)


@pytest.fixture
def world(tmp_path, monkeypatch):
    """One ``health`` profile, a fake live list and process table, and a
    recording restart."""
    state_dir = tmp_path / "tg-health"
    state_dir.mkdir()
    profiles = tmp_path / "profiles.json"
    profiles.write_text(json.dumps({"profiles": [
        {"id": "health", "label": "Health", "skill": "health",
         "state_dir": state_dir.as_posix()},
    ]}), encoding="utf-8")
    live = {"health": _session()}
    procs = [_claude(10, "health"), _bun(11, 10)]
    restarts: list = []
    audits: list = []

    async def fake_live(cfg):
        return dict(live)

    async def fake_restart(cfg, profile, **kw):
        restarts.append(profile.id)
        return {"restarted": True}

    monkeypatch.setattr(life_os_channels, "_live_profile_sessions", fake_live)
    monkeypatch.setattr(life_os_channels, "restart_channel_session", fake_restart)
    monkeypatch.setattr(channel_heal.audit, "audit_event",
                        lambda kind, **f: audits.append((kind, f)))
    cfg = WebappConfig(life_os_dir=(tmp_path / "life-os").as_posix())
    return SimpleNamespace(cfg=cfg, profiles=profiles, live=live, procs=procs,
                           restarts=restarts, audits=audits, state=channel_heal.HealState())


def _tick(world, at: float) -> None:
    asyncio.run(channel_heal.tick_once(
        world.cfg, world.profiles, world.state, now=at, scan=lambda: list(world.procs)))


def _list_row(world, monkeypatch) -> dict:
    """The channel list's row for ``health``, on the fake process table."""
    monkeypatch.setattr(channel_health, "snapshot", lambda: list(world.procs))
    monkeypatch.setattr(life_os_channels.time, "time", lambda: T0)
    app = SimpleNamespace(state=SimpleNamespace(
        channel_profiles_path=world.profiles, installed_plugins_path=world.profiles,
        channel_heal=world.state))
    profiles, problems = load_channel_profiles(world.profiles)
    answer = life_os_channels._channels_answer(
        app, world.cfg, profiles, problems, dict(world.live))
    return answer["profiles"][0]


class TestHealing:
    def test_a_killed_server_reads_degraded_with_its_reason(self, world, monkeypatch):
        assert _list_row(world, monkeypatch)["health"] == "ok"
        world.procs[:] = [_claude(10, "health")]  # the plugin server was killed
        row = _list_row(world, monkeypatch)
        assert row["health"] == "degraded"
        assert row["health_reason"] == channel_health.REASON_NO_SERVER
        assert row["needs_owner"] is False

    def test_one_restart_then_an_owner_flag_never_a_loop(self, world, monkeypatch, caplog):
        caplog.set_level(logging.INFO, logger=channel_heal.__name__)
        world.procs[:] = [_claude(10, "health")]
        after = channel_heal.HEAL_AFTER_SECONDS
        _tick(world, T0)                       # detected
        _tick(world, T0 + after - 1)           # still inside the grace
        assert world.restarts == []
        _tick(world, T0 + after)               # the one restart
        assert world.restarts == ["health"]
        _tick(world, T0 + after + 60)          # degraded again: a new detection
        _tick(world, T0 + 2 * after + 60)      # the flag, not a second restart
        for minute in range(1, 6):
            _tick(world, T0 + 2 * after + 60 + minute * 60)
        assert world.restarts == ["health"]
        assert world.state.flagged["health"].endswith(channel_heal.FLAG_REASON_SUFFIX)
        assert [f["action"] for _k, f in world.audits] == [
            "detected", "restart", "detected", "flag_owner"]
        assert _list_row(world, monkeypatch)["needs_owner"] is True
        text = caplog.text
        assert "profile health degraded" in text
        assert "restarting it once" in text
        assert "flagged for the owner" in text

    def test_a_recovered_server_clears_the_record(self, world, caplog):
        caplog.set_level(logging.INFO, logger=channel_heal.__name__)
        world.procs[:] = [_claude(10, "health")]
        _tick(world, T0)
        _tick(world, T0 + channel_heal.HEAL_AFTER_SECONDS)
        world.procs.append(_bun(11, 10))
        _tick(world, T0 + channel_heal.HEAL_AFTER_SECONDS + 60)
        assert world.state.restarted_at == {} and world.state.flagged == {}
        assert "plugin server is running again" in caplog.text

    def test_an_unreadable_process_table_restarts_nothing(self, world):
        for minute in range(0, 20):
            asyncio.run(channel_heal.tick_once(
                world.cfg, world.profiles, world.state, now=T0 + minute * 60,
                scan=lambda: None))
        assert world.restarts == [] and world.audits == []

    def test_a_stopped_bot_is_never_started_here(self, world):
        world.live.clear()
        for minute in range(0, 20):
            _tick(world, T0 + minute * 60)
        assert world.restarts == []
