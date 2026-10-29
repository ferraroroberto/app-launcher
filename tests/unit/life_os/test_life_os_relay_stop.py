"""Telegram relay stop and status: only what the relay reopened, closed gracefully.

Fictional profiles, captures and sessions throughout. The session-host's stop
and session lookup are recorders, the Board's turn facts are a stub, and the
registry lives in the test's own runtime-data directory.
"""

from __future__ import annotations

import inspect
import json
import time
from pathlib import Path

import pytest

from src.life_os import relay_registry
from tests.api._life_os_support import RESUMABLE_SID
from tests.unit.life_os.test_life_os_relay import BOT, PREFIX, _conversation_session, relay  # noqa: F401

SECOND_SID = "9a1d3c55-2e7b-4f08-b6c4-0d5e8f7a1b23"


@pytest.fixture
def stop_host(relay, monkeypatch):
    """Record every session-host stop; a quit makes the session vanish
    unless the test says it should stay alive."""
    from app.webapp.routers import life_os_channels, life_os_relay
    from src.clients import session as session_client

    # The shared quit helper reaches the host through the restart module's
    # own client reference, which the webapp fixture already mocks.
    host = life_os_channels.session_client
    relay.stops, relay.survive_quit = [], False
    relay.facts = {}

    def fake_stop(port, sid, mode="quit"):
        relay.stops.append((sid, mode))
        if not relay.survive_quit:
            relay.sessions[:] = [s for s in relay.sessions if s["session_id"] != sid]
        return {"ok": True}

    def fake_get(port, sid):
        sess = next((s for s in relay.sessions if s["session_id"] == sid), None)
        if sess is None:
            raise session_client.SessionHostError("gone", status=404)
        return sess

    async def fake_facts(caller):
        return relay.facts

    host.stop.side_effect = fake_stop
    host.get_session.side_effect = fake_get
    monkeypatch.setattr(life_os_channels, "RESTART_POLL_SECONDS", 0.01)
    monkeypatch.setattr(life_os_relay, "RESTART_QUIT_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(life_os_relay, "_turn_facts", fake_facts)
    return relay


def _resume(relay, **body):
    return relay.client.post(f"{PREFIX}/resume", headers=relay.headers, json=body)


def _stop(relay, sid):
    return relay.client.post(f"{PREFIX}/stop", headers=relay.headers, json={"session_id": sid})


def _status(relay, **params):
    return relay.client.get(f"{PREFIX}/status", headers=relay.headers, params=params).json()


def _reopen(relay, chat="chat-a"):
    """Resume through the relay and make the launched session live, idle and
    captured after its last turn."""
    body = _resume(relay, chat=chat).json()
    assert body["state"] == "resumed" and body["registered"] is True, body
    relay.sessions.append(_conversation_session(body["session_id"], RESUMABLE_SID))
    capture = Path(relay.client.app.state.webapp_config.life_os_dir) / body["conversation"]
    relay.facts[body["session_id"]] = {"status": "idle",
                                       "last_turn_at": capture.stat().st_mtime - 5}
    return body


class TestRegistry:
    def test_resumed_registers_and_opened_does_not(self, stop_host):
        body = _reopen(stop_host)
        (entry,) = relay_registry.entries("health", [body["session_id"]])
        assert entry["chat"] == "chat-a" and entry["conversation"] == body["conversation"]
        # Found open again from another chat: no second entry, the owner unchanged.
        other = _resume(stop_host, chat="chat-b").json()
        assert other["state"] == "opened" and other["registered"] is True
        (entry,) = relay_registry.entries("health", [body["session_id"]])
        assert entry["chat"] == "chat-a"

    def test_a_session_the_relay_found_open_is_never_registered(self, stop_host):
        stop_host.sessions.append(_conversation_session("owner-1", RESUMABLE_SID))
        body = _resume(stop_host).json()
        assert body["state"] == "opened" and body["registered"] is False
        assert relay_registry.entries("health", ["owner-1"]) == []

    def test_entries_for_ended_sessions_are_pruned_on_read(self, stop_host):
        body = _reopen(stop_host)
        stop_host.sessions[:] = [dict(BOT)]
        assert _status(stop_host)["state"] == "none"
        assert relay_registry.find("health", body["session_id"]) is None


class TestStop:
    def test_stop_quits_gracefully_and_is_reported_once(self, stop_host):
        body = _reopen(stop_host)
        resp = _stop(stop_host, body["session_id"])
        assert resp.status_code == 200, resp.text
        assert resp.json()["state"] == "stopped"
        assert stop_host.stops == [(body["session_id"], "quit")]
        assert all(s["session_id"] != body["session_id"] for s in stop_host.sessions)
        (stopped,) = _status(stop_host)["stops"]
        assert stopped["reason"] == "done" and stopped["session_id"] == body["session_id"]
        assert _status(stop_host)["stops"] == []

    def test_an_unconfirmed_quit_keeps_the_entry_and_never_kills(self, stop_host):
        body = _reopen(stop_host)
        stop_host.survive_quit = True
        resp = _stop(stop_host, body["session_id"])
        assert resp.status_code == 502
        assert resp.json()["detail"]["state"] == "stop_unconfirmed"
        assert stop_host.stops == [(body["session_id"], "quit")]
        assert relay_registry.find("health", body["session_id"]) is not None

    def test_no_code_path_in_stop_issues_a_kill(self):
        from app.webapp.routers import life_os_relay
        source = inspect.getsource(life_os_relay.stop_conversation)
        assert '"kill"' not in source and "'kill'" not in source
        assert "_quit_and_confirm_gone" not in inspect.getsource(life_os_relay)

    @pytest.mark.parametrize("who", ["other_profile", "owner_opened", "bot", "chief"])
    def test_anything_the_relay_did_not_reopen_here_is_left_alone(self, stop_host, who):
        sid = {"other_profile": "conv-9", "owner_opened": "owner-1",
               "bot": "tg-2", "chief": "chief-1"}[who]
        extra = {"bot": {"label": "telegram:school"}, "chief": {"label": "chief"}}.get(who, {})
        stop_host.sessions.append(_conversation_session(sid, RESUMABLE_SID, **extra))
        stop_host.facts[sid] = {"status": "idle", "last_turn_at": 0.0}
        if who == "other_profile":
            relay_registry.add("school", "", sid, "x.md", "", time.time())
        if who in ("bot", "chief"):
            # Even a forged registry row can't reach them: the steer checks still apply.
            relay_registry.add("health", "", sid, "x.md", "", time.time())
        resp = _stop(stop_host, sid)
        assert resp.status_code == 403
        assert resp.json()["detail"]["state"] == "not_relay_owned"
        assert stop_host.stops == []
        assert any(s["session_id"] == sid for s in stop_host.sessions)

    @pytest.mark.parametrize("status", ["working", None])
    def test_a_turn_in_flight_or_unknown_is_busy(self, stop_host, status):
        body = _reopen(stop_host)
        stop_host.facts[body["session_id"]]["status"] = status
        resp = _stop(stop_host, body["session_id"])
        assert resp.status_code == 409
        assert resp.json()["detail"]["state"] == "busy"
        assert stop_host.stops == []

    def test_a_finished_turn_waiting_on_a_question_is_stopped(self, stop_host, monkeypatch):
        from app.webapp.routers import life_os_relay

        async def finished(caller, sid):
            return True

        monkeypatch.setattr(life_os_relay, "_turn_finished", finished)
        body = _reopen(stop_host)
        stop_host.facts[body["session_id"]]["status"] = "needs-you"
        resp = _stop(stop_host, body["session_id"])
        assert resp.status_code == 200, resp.text
        assert resp.json()["state"] == "stopped"
        assert stop_host.stops == [(body["session_id"], "quit")]

    def test_a_needs_you_that_is_not_a_finished_turn_is_busy(self, stop_host, monkeypatch):
        from app.webapp.routers import life_os_relay

        async def not_finished(caller, sid):
            return False

        monkeypatch.setattr(life_os_relay, "_turn_finished", not_finished)
        body = _reopen(stop_host)
        stop_host.facts[body["session_id"]]["status"] = "needs-you"
        resp = _stop(stop_host, body["session_id"])
        assert resp.status_code == 409
        assert resp.json()["detail"]["state"] == "busy"
        assert stop_host.stops == []

    def test_a_finished_question_still_waits_for_its_capture(self, stop_host, monkeypatch):
        from app.webapp.routers import life_os_relay

        async def finished(caller, sid):
            return True

        monkeypatch.setattr(life_os_relay, "_turn_finished", finished)
        body = _reopen(stop_host)
        facts = stop_host.facts[body["session_id"]]
        facts["status"] = "needs-you"
        facts["last_turn_at"] = None
        resp = _stop(stop_host, body["session_id"])
        assert resp.status_code == 409
        assert resp.json()["detail"]["state"] == "capture_unconfirmed"
        assert stop_host.stops == []

    @pytest.mark.parametrize("case", ["older_capture", "unknown_turn", "missing_capture"])
    def test_a_capture_not_newer_than_the_last_turn_waits(self, stop_host, case):
        body = _reopen(stop_host)
        facts = stop_host.facts[body["session_id"]]
        capture = Path(stop_host.client.app.state.webapp_config.life_os_dir) / body["conversation"]
        if case == "older_capture":
            facts["last_turn_at"] = capture.stat().st_mtime + 30
        elif case == "unknown_turn":
            facts["last_turn_at"] = None
        else:
            relay_registry.add("health", "chat-a", body["session_id"], "gone.md", "", time.time())
        resp = _stop(stop_host, body["session_id"])
        assert resp.status_code == 409
        assert resp.json()["detail"]["state"] == "capture_unconfirmed"
        assert stop_host.stops == []

    def test_stop_is_audited_with_its_outcome(self, stop_host, caplog):
        body = _reopen(stop_host)
        with caplog.at_level("INFO", logger="launcher.terminal_audit"):
            _stop(stop_host, body["session_id"])
        (line,) = [r.getMessage() for r in caplog.records
                   if "action='relay_stop'" in r.getMessage()]
        assert "outcome='stopped'" in line and "profile='health'" in line


class TestTurnFinished:
    """The real Board split behind ``stop`` on a ``needs-you`` row."""

    @pytest.mark.parametrize("tail,expected", [
        ("text", True),
        ("tool_use", False),
        ("AskUserQuestion", False),
        ("none", False),
    ])
    def test_only_a_clean_text_stop_is_a_finished_turn(self, tmp_path, monkeypatch, tail, expected):
        import asyncio
        from datetime import timedelta
        from types import SimpleNamespace

        from app.webapp.routers import life_os_relay
        from src import board
        from tests.unit.board._support import (
            NOW, _bash_tool_use_line, _live, _msg_line, _state_row, _transcript_file)

        stamp = NOW - timedelta(minutes=10) + timedelta(seconds=5)
        row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
        content = {
            "text": _msg_line("assistant", stamp),
            "tool_use": _bash_tool_use_line(stamp, "toolu_1", run_in_background=False),
            "AskUserQuestion": _bash_tool_use_line(
                stamp, "toolu_1", run_in_background=False, tool_name="AskUserQuestion"),
            "none": None,
        }[tail]
        row["transcript_path"] = (
            _transcript_file(tmp_path, stamp, content) if tail != "none" else None)
        real_merge = board.merge_sessions
        monkeypatch.setattr(board, "read_sessions_state", lambda path: {"rows": {"t": row}})
        monkeypatch.setattr(board, "merge_sessions",
                            lambda live, rows: real_merge(live, rows, now=NOW))
        caller = SimpleNamespace(
            cfg=SimpleNamespace(sessions_state_file=(tmp_path / "state.json").as_posix()),
            sessions=[_live("aaa", "E:/x/y", 30)])
        assert asyncio.run(life_os_relay._turn_finished(caller, "aaa")) is expected


class TestChats:
    def test_two_chats_each_keep_their_own_conversation(self, stop_host, monkeypatch):
        from app.webapp.routers import life_os_spawn
        ids = iter(["new-a", "new-b"])
        monkeypatch.setattr(life_os_spawn, "spawn_claude_session",
                            lambda *a, **k: {"session_id": next(ids), "kind": "pty"})
        root = Path(stop_host.client.app.state.webapp_config.life_os_dir)
        folder = root / ".claude" / "skills" / "journal-daily" / "conversations"
        second = "2026-09-02-0900-garden-plan.md"
        (folder / second).write_text(
            f'<!-- capture sid="{SECOND_SID}" agent="claude" updated="synthetic" -->\nlog',
            encoding="utf-8")
        index = json.loads((folder / "index.json").read_text(encoding="utf-8"))
        index.append({"skill": "journal-daily", "file": second, "date": "2026-09-02",
                      "slug": "garden", "sid": SECOND_SID, "agent": "claude", "topic": "garden"})
        (folder / "index.json").write_text(json.dumps(index), encoding="utf-8")

        a = _resume(stop_host, chat="chat-a").json()
        stop_host.sessions.append(_conversation_session(a["session_id"], RESUMABLE_SID))
        path_b = next(c["path"] for c in stop_host.client.get(
            f"{PREFIX}/conversations", headers=stop_host.headers).json()["conversations"]
            if c["path"].endswith(second))
        b = _resume(stop_host, chat="chat-b", path=path_b).json()
        stop_host.sessions.append(_conversation_session(b["session_id"], SECOND_SID))
        assert (a["state"], b["state"]) == ("resumed", "resumed")

        again = _resume(stop_host, chat="chat-a", path=path_b).json()
        assert again["state"] == "already_open" and again["session_id"] == "new-a"
        assert [r["session_id"] for r in _status(stop_host, chat="chat-a")["conversations"]] \
            == ["new-a"]
        assert {r["session_id"] for r in _status(stop_host)["conversations"]} == {"new-a", "new-b"}

    def test_status_lists_only_this_profiles_entries(self, stop_host):
        body = _reopen(stop_host)
        relay_registry.add("school", "chat-a", "conv-9", "x.md", "", time.time())
        stop_host.sessions.append(_conversation_session("conv-9", SECOND_SID))
        status = _status(stop_host)
        assert status["state"] == "open"
        (row,) = status["conversations"]
        assert row["session_id"] == body["session_id"]
        assert row["idle_minutes"] == 0 and row["minutes_to_close"] == 60

    def test_status_reports_the_resume_offer_setting_on_every_call(self, stop_host):
        path = stop_host.client.app.state.channel_profiles_path

        def set_offer(value):
            data = json.loads(path.read_text(encoding="utf-8"))
            data["profiles"][0]["resume_offer"] = value
            path.write_text(json.dumps(data), encoding="utf-8")

        assert _status(stop_host)["resume_offer"] is True
        assert _status(stop_host, chat="chat-a")["resume_offer"] is True
        set_offer(False)
        assert _status(stop_host)["resume_offer"] is False
        assert _status(stop_host, chat="chat-a")["resume_offer"] is False
        set_offer(True)
        assert _status(stop_host)["resume_offer"] is True

    def test_a_bad_chat_key_is_refused(self, stop_host):
        assert _resume(stop_host, chat={"x": 1}).status_code == 400
        assert _resume(stop_host, chat="x" * 65).status_code == 400
        assert stop_host.spawned == []


def test_find_passes_the_search_tier_through_only_when_sent(relay, monkeypatch):  # noqa: F811
    from app.webapp.routers import life_os_relay

    async def fake_search(cfg, query, skill, limit):
        return {"available": True, "results": [
            {"path": "a.md", "sid": "", "tier": 0}, {"path": "b.md", "sid": ""}]}

    monkeypatch.setattr(life_os_relay, "run_conversation_search", fake_search)
    rows = relay.client.get(f"{PREFIX}/conversations", headers=relay.headers,
                            params={"q": "garden"}).json()["conversations"]
    assert rows[0]["tier"] == 0 and "tier" not in rows[1]


def test_stop_and_status_are_terminal_gated():
    from app.webapp import middleware
    for tail in ("stop", "status"):
        assert middleware._terminal_guard_level(f"{PREFIX}/{tail}") == "passkey"
