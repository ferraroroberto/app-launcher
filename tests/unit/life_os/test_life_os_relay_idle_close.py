"""Telegram relay idle close: only what the relay reopened, after the bot's timeout.

Fictional profiles, captures and sessions throughout, reusing the stop tests'
host recorders. Each tick runs against a fake clock (``now``) set minutes past
the moment the conversation was reopened.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.life_os import relay_registry
from tests.api._life_os_support import RESUMABLE_SID
from tests.unit.life_os.test_life_os_relay import BOT, _conversation_session, relay  # noqa: F401
from tests.unit.life_os.test_life_os_relay_stop import _reopen, _status, stop_host  # noqa: F401

MINUTE = 60.0


@pytest.fixture
def reaper(stop_host, monkeypatch):
    """The stop host plus a recorder for what is typed into any session."""
    from src.clients import session as session_client

    stop_host.typed = []

    def fake_send(port, sid, data, submit=True):
        stop_host.typed.append((sid, data))
        return {"ok": True, "reason": "ok", "submit_confirmed": True}

    monkeypatch.setattr(session_client, "send_input", fake_send)
    from app.webapp import relay_idle_close
    stop_host.state = relay_idle_close.ReaperState()
    return stop_host


def _set_timeout(relay, minutes):
    path = relay.client.app.state.channel_profiles_path
    data = json.loads(path.read_text(encoding="utf-8"))
    data["profiles"][0]["idle_close_minutes"] = minutes
    path.write_text(json.dumps(data), encoding="utf-8")


def _tick(relay, now):
    from app.webapp import relay_idle_close
    app = relay.client.app
    asyncio.run(relay_idle_close.tick_once(app.state.webapp_config,
                                           app.state.channel_profiles_path, relay.state, now))


def _opened(relay):
    """Reopen a conversation and make its session idle with a clean composer;
    the moment it went quiet."""
    body = _reopen(relay)
    sess = next(s for s in relay.sessions if s["session_id"] == body["session_id"])
    sess["composer_dirty"] = False
    entry = relay_registry.find("health", body["session_id"])
    quiet = max(entry["last_activity"], relay.facts[body["session_id"]]["last_turn_at"])
    return body["session_id"], sess, quiet


def _alive(relay, sid):
    return any(s["session_id"] == sid for s in relay.sessions)


class TestWhenDue:
    def test_idle_past_the_timeout_closes_and_recent_output_does_not(self, reaper):
        _set_timeout(reaper, 30)
        sid, sess, quiet = _opened(reaper)
        sess["last_output_at"] = quiet + 26 * MINUTE
        _tick(reaper, quiet + 31 * MINUTE)
        assert _alive(reaper, sid) and reaper.stops == []
        _tick(reaper, quiet + 26 * MINUTE + 31 * MINUTE)
        assert reaper.stops == [(sid, "quit")] and not _alive(reaper, sid)

    @pytest.mark.parametrize("field", ["last_input", "composer_dirty_since"])
    def test_input_from_the_app_resets_the_idle_time(self, reaper, field):
        _set_timeout(reaper, 30)
        sid, sess, quiet = _opened(reaper)
        typed_at = quiet + 25 * MINUTE
        sess[field] = {"at": typed_at, "reason": "ok"} if field == "last_input" else typed_at
        _tick(reaper, quiet + 31 * MINUTE)
        assert reaper.stops == []
        _tick(reaper, typed_at + 31 * MINUTE)
        assert reaper.stops == [(sid, "quit")]

    @pytest.mark.parametrize("dirty", [True, None])
    def test_typed_or_unknown_composer_text_is_never_thrown_away(self, reaper, dirty):
        _set_timeout(reaper, 30)
        sid, sess, quiet = _opened(reaper)
        sess["composer_dirty"] = dirty
        _tick(reaper, quiet + 300 * MINUTE)
        assert reaper.stops == [] and _alive(reaper, sid)

    def test_off_never_closes_anything_for_that_bot(self, reaper):
        _set_timeout(reaper, 0)
        sid, _sess, quiet = _opened(reaper)
        _tick(reaper, quiet + 10_000 * MINUTE)
        assert reaper.stops == [] and _alive(reaper, sid)

    def test_the_default_timeout_is_an_hour(self, reaper):
        sid, _sess, quiet = _opened(reaper)
        _tick(reaper, quiet + 59 * MINUTE)
        assert reaper.stops == []
        _tick(reaper, quiet + 61 * MINUTE)
        assert reaper.stops == [(sid, "quit")]


class TestWaits:
    def test_a_working_conversation_waits_for_the_next_tick(self, reaper):
        _set_timeout(reaper, 30)
        sid, _sess, quiet = _opened(reaper)
        reaper.facts[sid]["status"] = "working"
        _tick(reaper, quiet + 31 * MINUTE)
        assert reaper.stops == [] and _alive(reaper, sid)
        reaper.facts[sid]["status"] = "idle"
        _tick(reaper, quiet + 32 * MINUTE)
        assert reaper.stops == [(sid, "quit")]

    def test_three_unconfirmed_captures_leave_it_alone(self, reaper, caplog, monkeypatch):
        _set_timeout(reaper, 30)
        sid, _sess, quiet = _opened(reaper)
        capture = Path(reaper.client.app.state.webapp_config.life_os_dir) / \
            relay_registry.find("health", sid)["conversation"]
        reaper.facts[sid]["last_turn_at"] = capture.stat().st_mtime + 1
        attempts = []
        from app.webapp.routers import life_os_relay
        real_stop = life_os_relay.stop_conversation

        async def counting_stop(caller, target, reason):
            attempts.append(target)
            return await real_stop(caller, target, reason)

        monkeypatch.setattr(life_os_relay, "stop_conversation", counting_stop)
        with caplog.at_level("WARNING", logger="app.webapp.relay_idle_close"):
            for minute in range(31, 36):
                _tick(reaper, capture.stat().st_mtime + minute * MINUTE)
        assert attempts == [sid] * 3
        assert reaper.stops == [] and _alive(reaper, sid)
        assert any("left alone" in r.getMessage() for r in caplog.records)


class TestNotice:
    def test_one_notice_line_and_status_lists_the_idle_stop(self, reaper):
        _set_timeout(reaper, 30)
        sid, _sess, quiet = _opened(reaper)
        title = relay_registry.find("health", sid)["title"]
        _tick(reaper, quiet + 31 * MINUTE)
        _tick(reaper, quiet + 32 * MINUTE)
        assert reaper.typed == [(BOT["session_id"],
                                 f"[launcher-relay] closed after idle: {title}")]
        (stopped,) = _status(reaper)["stops"]
        assert stopped["reason"] == "idle" and stopped["session_id"] == sid

    def test_a_failed_notice_keeps_the_recorded_stop(self, reaper, monkeypatch):
        from src.clients import session as session_client

        def refuse(port, sid, data, submit=True):
            raise session_client.SessionHostError("gone", status=409)

        monkeypatch.setattr(session_client, "send_input", refuse)
        _set_timeout(reaper, 30)
        sid, _sess, quiet = _opened(reaper)
        _tick(reaper, quiet + 31 * MINUTE)
        assert reaper.stops == [(sid, "quit")]
        (stopped,) = _status(reaper)["stops"]
        assert stopped["reason"] == "idle"

    def test_the_notice_is_one_line_whatever_the_title(self):
        from app.webapp.relay_idle_close import _notice
        line = _notice("a fictional\ntopic\r\nwith [brackets]" + "x" * 300)
        assert "\n" not in line and "\r" not in line
        assert line.startswith("[launcher-relay] closed after idle: a fictional topic")
        assert _notice("") == "[launcher-relay] closed after idle: the conversation"

    def test_the_idle_stop_is_audited_with_its_outcome(self, reaper, caplog):
        _set_timeout(reaper, 30)
        sid, _sess, quiet = _opened(reaper)
        with caplog.at_level("INFO", logger="launcher.terminal_audit"):
            _tick(reaper, quiet + 31 * MINUTE)
        (line,) = [r.getMessage() for r in caplog.records
                   if "action='relay_idle_stop'" in r.getMessage()]
        assert "outcome='stopped'" in line and "profile='health'" in line
        assert "idle_minutes=31" in line and f"session='{sid}'" in line


class TestNeverTouched:
    def test_a_life_os_session_from_the_app_is_never_closed(self, reaper):
        _set_timeout(reaper, 30)
        reaper.sessions.append(_conversation_session(
            "app-1", RESUMABLE_SID, composer_dirty=False, last_output_at=0.0))
        reaper.facts["app-1"] = {"status": "idle", "last_turn_at": 0.0}
        _tick(reaper, time.time() + 10_000 * MINUTE)
        assert reaper.stops == [] and reaper.typed == [] and _alive(reaper, "app-1")

    @pytest.mark.parametrize("label", ["telegram:health", "chief"])
    def test_a_forged_entry_for_a_bot_or_the_chief_is_refused(self, reaper, label):
        _set_timeout(reaper, 30)
        reaper.sessions.append(_conversation_session(
            "tg-9", RESUMABLE_SID, label=label, composer_dirty=False))
        reaper.facts["tg-9"] = {"status": "idle", "last_turn_at": 0.0}
        relay_registry.add("health", "", "tg-9", "x.md", "", 0.0)
        _tick(reaper, time.time() + 10_000 * MINUTE)
        assert reaper.stops == [] and reaper.typed == []


@pytest.mark.asyncio
class TestLifespan:
    async def _ticks(self, monkeypatch):
        from app.webapp import relay_idle_close, server

        ticks = []
        real_create_task = asyncio.create_task

        def _spy(coro, *a, **k):
            module = coro.cr_frame.f_globals.get("__name__") if coro.cr_frame else ""
            coro.close()
            task = real_create_task(asyncio.sleep(3600))
            if module == relay_idle_close.__name__:
                ticks.append(task)
            return task

        async def _noop(app):
            return None

        monkeypatch.setattr(server.asyncio, "create_task", _spy)
        monkeypatch.setattr(server, "_reconcile_orphan_mirror_windows", _noop)
        cfg = SimpleNamespace(jobs_coverage_interval_minutes=0, chief_doorbell=False)
        async with server._lifespan(SimpleNamespace(state=SimpleNamespace(webapp_config=cfg))):
            pass
        return ticks

    async def test_canonical_instance_starts_and_cancels_it(self, monkeypatch):
        from src import instance_role
        from src.webapp_config import SESSION_HOST_PORT_ENV
        monkeypatch.delenv(SESSION_HOST_PORT_ENV, raising=False)
        monkeypatch.setattr(instance_role, "canonical_instance",
                            lambda project_root=None: (True, instance_role.REASON_CANONICAL))
        ticks = await self._ticks(monkeypatch)
        assert len(ticks) == 1 and ticks[0].cancelled()

    async def test_disposable_instance_never_starts_it(self, monkeypatch):
        from src.webapp_config import SESSION_HOST_PORT_ENV
        monkeypatch.setenv(SESSION_HOST_PORT_ENV, "54321")
        assert await self._ticks(monkeypatch) == []
