"""Telegram relay ``new``: a fresh saved conversation, relay-owned like a reopened one.

Fictional profiles, captures and sessions throughout. The spawn is a recorder
and the session host's stop and the Board's turn facts are the stop tests' stubs.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from src.life_os import relay_registry
from tests.api._life_os_support import RESUMABLE_SID
from tests.unit.life_os.test_life_os_relay import PREFIX, _conversation_session, _steer, relay  # noqa: F401
from tests.unit.life_os.test_life_os_relay_stop import _status, _stop, stop_host  # noqa: F401


def _new(relay, **body):
    return relay.client.post(f"{PREFIX}/new", headers=relay.headers, json=body)


@pytest.fixture
def new_host(stop_host, monkeypatch):  # noqa: F811
    """``stop_host`` whose spawn hands out ``new-1``, ``new-2``, ..."""
    from app.webapp.routers import life_os_spawn
    ids = iter(f"new-{n}" for n in range(1, 9))

    def fake_spawn(project_dir, name, flags, port, kind, agent, rows=40, cols=120,
                   history_lines=None, label=""):
        from types import SimpleNamespace
        stop_host.spawned.append(SimpleNamespace(flags=flags, kind=kind, label=label))
        return {"session_id": next(ids), "kind": kind}

    monkeypatch.setattr(life_os_spawn, "spawn_claude_session", fake_spawn)
    return stop_host


def _live(relay, sid):
    relay.sessions.append({"session_id": sid, "alive": True, "kind": "pty",
                           "name": "fictional", "flags": ""})


class TestNew:
    def test_starts_the_ordinary_life_os_way_and_registers_it(self, new_host):
        resp = _new(new_host, chat="chat-a")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["state"] == "started" and body["session_id"] == "new-1"
        (spawn,) = new_host.spawned
        assert spawn.label == "" and spawn.kind == "pty"
        assert "--channels" not in spawn.flags and "--settings" not in spawn.flags
        assert spawn.flags.rstrip().endswith("/journal-daily")
        (entry,) = relay_registry.entries("health", ["new-1"], chat="chat-a")
        assert entry["origin"] == "new" and entry["conversation"] == ""

    def test_a_chat_with_one_open_gets_it_back_from_new_and_from_resume(self, new_host):
        _new(new_host, chat="chat-a")
        _live(new_host, "new-1")
        again = _new(new_host, chat="chat-a").json()
        assert again["state"] == "already_open" and again["session_id"] == "new-1"
        resume = new_host.client.post(f"{PREFIX}/resume", headers=new_host.headers,
                                      json={"chat": "chat-a"}).json()
        assert resume["state"] == "already_open" and resume["session_id"] == "new-1"
        assert len(new_host.spawned) == 1
        # Another chat is its own: one per person's chat.
        assert _new(new_host, chat="chat-b").json()["session_id"] == "new-2"

    def test_relay_off_and_a_wrong_token_start_nothing(self, new_host):
        off = new_host.client.post("/api/life-os/channels/plain/relay/new",
                                   headers=new_host.headers, json={})
        assert off.status_code == 403 and off.json()["detail"]["state"] == "relay_off"
        wrong = new_host.client.post(f"{PREFIX}/new", headers={"X-Relay-Token": "nope"}, json={})
        assert wrong.status_code == 401
        new_host.sessions[:] = []
        assert _new(new_host).status_code == 403
        assert new_host.spawned == []

    def test_a_failed_registry_write_ends_the_session_it_started(self, new_host, monkeypatch):
        def broken(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(relay_registry, "add", broken)
        resp = _new(new_host, chat="chat-a")
        assert resp.status_code == 500 and resp.json()["detail"]["state"] == "unknown"
        assert new_host.stops == [("new-1", "quit")]

    def test_a_skill_launched_from_the_app_is_never_registered(self, new_host):
        resp = new_host.client.post("/api/life-os/skills/journal-daily/launch", json={})
        assert resp.status_code == 200, resp.text
        assert relay_registry.entries("health", ["new-1"]) == []


class TestOwnedBeforeItsFirstCapture:
    def test_the_bot_can_steer_its_new_session_but_not_a_stranger(self, new_host):
        _new(new_host, chat="chat-a")
        _live(new_host, "new-1")
        _live(new_host, "stranger-1")
        assert _steer(new_host, "new-1").status_code == 200
        assert _steer(new_host, "stranger-1").status_code == 403

    def test_another_bots_new_session_is_not_this_bots(self, new_host):
        relay_registry.add("school", "", "new-9", "", "x", time.time(), origin="new")
        _live(new_host, "new-9")
        assert _steer(new_host, "new-9").status_code == 403

    def test_stop_waits_for_the_first_capture_then_closes_gracefully(self, new_host):
        _new(new_host, chat="chat-a")
        # The session shows the conversation its first turn captured to.
        new_host.sessions.append(_conversation_session("new-1", RESUMABLE_SID))
        new_host.facts["new-1"] = {"status": "idle", "last_turn_at": 0.0}
        # Unbound until a relay check sees the capture; stop's own target check binds it.
        entry = relay_registry.find("health", "new-1")
        assert entry["conversation"] == ""
        assert _stop(new_host, "new-1").json()["state"] == "stopped"
        assert new_host.stops == [("new-1", "quit")]
        (stopped,) = _status(new_host)["stops"]
        assert stopped["reason"] == "done" and stopped["conversation"].endswith(".md")

    def test_stop_with_no_capture_at_all_does_nothing(self, new_host):
        _new(new_host, chat="chat-a")
        _live(new_host, "new-1")
        new_host.facts["new-1"] = {"status": "idle", "last_turn_at": 0.0}
        resp = _stop(new_host, "new-1")
        assert resp.status_code == 409
        assert resp.json()["detail"]["state"] == "capture_unconfirmed"
        assert new_host.stops == []
        assert Path(new_host.client.app.state.webapp_config.life_os_dir).is_dir()


def test_the_idle_close_closes_a_new_conversation_like_a_reopened_one(new_host, monkeypatch):
    import asyncio

    from app.webapp import relay_idle_close
    from src.clients import session as session_client

    typed = []
    monkeypatch.setattr(session_client, "send_input", lambda port, sid, data, submit=True: (
        typed.append((sid, data)) or {"ok": True, "reason": "ok"}))
    path = new_host.client.app.state.channel_profiles_path
    path.write_text(path.read_text(encoding="utf-8").replace(
        '"relay": true', '"relay": true, "idle_close_minutes": 30'), encoding="utf-8")
    _new(new_host, chat="chat-a")
    new_host.sessions.append({**_conversation_session("new-1", RESUMABLE_SID),
                              "composer_dirty": False})
    new_host.facts["new-1"] = {"status": "idle", "last_turn_at": 0.0}
    app = new_host.client.app
    state = relay_idle_close.ReaperState()
    now = time.time() + 31 * 60
    asyncio.run(relay_idle_close.tick_once(
        app.state.webapp_config, app.state.channel_profiles_path, state, now))
    assert new_host.stops == [("new-1", "quit")]
    assert typed and typed[0][1].startswith(relay_idle_close.NOTICE_PREFIX)
    (stopped,) = _status(new_host)["stops"]
    assert stopped["reason"] == "idle"
