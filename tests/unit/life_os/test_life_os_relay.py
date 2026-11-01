"""Telegram relay (#1639): a skill's bot may steer only its own conversations.

Fictional skills, captures and sessions throughout: nothing real is launched,
typed into or read. The session-host list is a plain list per test, the spawn
and the PTY write are stubs, and the Board exchange is a fake.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.launching import channel_profiles
from tests.api._life_os_support import OTHER_SID, RESUMABLE_SID

PREFIX = "/api/life-os/channels/health/relay"
BOT = {"session_id": "bot-1", "alive": True, "kind": "pty", "label": "telegram:health",
       "name": "Telegram · Health"}


def _conversation_session(sid: str, conversation: str, **extra) -> dict:
    """A live session launched to resume ``conversation`` (``--resume <id>``)."""
    return {"session_id": sid, "alive": True, "kind": "pty", "name": "fictional",
            "flags": f"--resume {conversation}", **extra}


def _add_skill(root: Path, skill: str, file: str, sid: str) -> None:
    folder = root / ".claude" / "skills" / skill
    (folder / "conversations").mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        f"---\nname: {skill}\ndescription: A fictional skill.\n---\n# {skill}\n", encoding="utf-8")
    (folder / "conversations" / file).write_text(
        f'<!-- capture sid="{sid}" agent="claude" updated="synthetic" -->\nlog', encoding="utf-8")
    (folder / "conversations" / "index.json").write_text(json.dumps([
        {"skill": skill, "file": file, "date": file[:10], "slug": "x", "sid": sid,
         "agent": "claude", "topic": "a fictional topic"},
    ]), encoding="utf-8")


@pytest.fixture
def relay(life_os_client, tmp_path, monkeypatch):
    """A relay-enabled ``health`` profile on journal-daily, its settings file
    written as a launch writes it, and the bot's session running."""
    from app.webapp import middleware
    from app.webapp.routers import life_os_relay, life_os_spawn
    from app.webapp.routers import sessions as sessions_router
    from src.clients import session as session_client

    client, app, overrides = life_os_client
    monkeypatch.setattr(middleware, "LOOPBACK_HOSTS", frozenset({"testclient"}))
    monkeypatch.setenv("APP_LAUNCHER_DATA_DIR", (tmp_path / "data").as_posix())
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    _add_skill(overrides["life_os_dir"], "school-notes", "2026-09-01-0800-homework.md", OTHER_SID)

    state = tmp_path / "tg-health"
    state.mkdir()
    rows = [{"id": "health", "label": "Health", "skill": "journal-daily",
             "state_dir": state.as_posix(), "relay": True},
            {"id": "plain", "label": "Plain", "skill": "journal-daily",
             "state_dir": (tmp_path / "tg-plain").as_posix()}]
    (tmp_path / "tg-plain").mkdir()
    profiles_path = tmp_path / "profiles.json"
    profiles_path.write_text(json.dumps({"profiles": rows}), encoding="utf-8")
    app.state.channel_profiles_path = profiles_path
    profiles, problems = channel_profiles.load_channel_profiles(profiles_path)
    assert problems == []
    settings = json.loads(Path(channel_profiles.write_channel_settings(
        profiles[0], relay_url="https://127.0.0.1:8445")).read_text(encoding="utf-8"))
    token = settings["env"][channel_profiles.RELAY_TOKEN_ENV]

    ns = SimpleNamespace(client=client, token=token, sessions=[dict(BOT)], spawned=[],
                         exchange={"available": True, "user": None, "source": "native",
                                   "assistant": {"text": "PONG", "timestamp": "2026-10-17T10:00:00Z"}})
    monkeypatch.setattr(session_client, "list_sessions", lambda port: ns.sessions)

    def fake_spawn(project_dir, name, flags, port, kind, agent,
                   rows=40, cols=120, history_lines=None, label=""):
        ns.spawned.append(SimpleNamespace(flags=flags, kind=kind, label=label))
        return {"session_id": "new-1", "kind": kind}

    monkeypatch.setattr(life_os_spawn, "spawn_claude_session", fake_spawn)
    ns.send_input = sessions_router.session_client.send_input
    ns.send_input.return_value = {"ok": True, "reason": "ok", "submit_state": "confirmed",
                                  "delivered": True}

    async def fake_exchange(sid, request):
        ns.exchange_reads = getattr(ns, "exchange_reads", 0) + 1
        return ns.exchange

    monkeypatch.setattr(life_os_relay, "session_exchange", fake_exchange)
    ns.headers = {"X-Relay-Token": token}
    return ns


def _steer(relay, sid, **body):
    return relay.client.post(f"{PREFIX}/steer", headers=relay.headers,
                             json={"session_id": sid, "data": "hello", **body})


# ------------------------------------------------------------ identity
class TestWhoMayCall:
    def test_relay_routes_are_terminal_gated(self):
        from app.webapp import middleware
        for tail in ("conversations", "resume", "steer", "read"):
            assert middleware._terminal_guard_level(f"{PREFIX}/{tail}") == "passkey"

    def test_a_missing_or_wrong_token_is_refused(self, relay):
        assert relay.client.get(f"{PREFIX}/conversations").status_code == 401
        resp = relay.client.get(f"{PREFIX}/conversations",
                                headers={"X-Relay-Token": "not-the-token"})
        assert resp.status_code == 401
        assert resp.json()["detail"]["state"] == "refused"

    def test_a_relaunch_retires_the_old_token(self, relay, tmp_path):
        profiles, _ = channel_profiles.load_channel_profiles(tmp_path / "profiles.json")
        channel_profiles.write_channel_settings(profiles[0], relay_url="https://127.0.0.1:8445")
        assert relay.client.get(f"{PREFIX}/conversations", headers=relay.headers).status_code == 401

    def test_relay_off_profile_is_refused_even_with_a_token(self, relay):
        resp = relay.client.get("/api/life-os/channels/plain/relay/conversations",
                                headers=relay.headers)
        assert resp.status_code == 403
        assert resp.json()["detail"]["state"] == "relay_off"

    def test_off_pc_callers_are_refused(self, relay, monkeypatch):
        from app.webapp import middleware
        from app.webapp.routers import life_os_relay
        monkeypatch.setattr(life_os_relay, "is_pc_itself", lambda host, headers: False)
        resp = relay.client.get(f"{PREFIX}/conversations", headers=relay.headers)
        assert resp.status_code == 403
        assert "this PC" in resp.json()["detail"]["message"]
        assert middleware  # the gate itself is pinned by test_relay_routes_are_terminal_gated

    def test_the_bot_must_be_running(self, relay):
        relay.sessions[:] = []
        resp = relay.client.get(f"{PREFIX}/conversations", headers=relay.headers)
        assert resp.status_code == 403
        assert "not running" in resp.json()["detail"]["message"]

    def test_host_unreachable_is_its_own_state(self, relay, monkeypatch):
        from src.clients import session as session_client

        def down(port):
            raise session_client.SessionHostError("down")

        monkeypatch.setattr(session_client, "list_sessions", down)
        resp = relay.client.post(f"{PREFIX}/resume", headers=relay.headers, json={})
        assert resp.status_code == 503
        assert resp.json()["detail"]["state"] == "host_unreachable"
        assert relay.spawned == []


# ------------------------------------------------------------ scope
class TestScope:
    def test_a_conversation_of_another_skill_is_refused(self, relay):
        relay.sessions.append(_conversation_session("other-1", OTHER_SID))
        resp = _steer(relay, "other-1")
        assert resp.status_code == 403
        assert resp.json()["detail"]["state"] == "not_this_skill"
        relay.send_input.assert_not_called()
        read = relay.client.get(f"{PREFIX}/read", headers=relay.headers,
                                params={"session_id": "other-1"})
        assert read.status_code == 403
        assert getattr(relay, "exchange_reads", 0) == 0

    def test_a_bot_or_the_chief_is_refused_even_on_this_skills_conversation(self, relay):
        relay.sessions.append(_conversation_session(
            "tg-2", RESUMABLE_SID, label="telegram:school"))
        relay.sessions.append(_conversation_session("chief-1", RESUMABLE_SID, label="chief"))
        for sid in ("tg-2", "chief-1", "bot-1"):
            resp = _steer(relay, sid)
            assert resp.status_code == 403, sid
            assert resp.json()["detail"]["state"] == "not_this_skill"
        relay.send_input.assert_not_called()

    def test_a_session_on_no_conversation_is_refused(self, relay):
        relay.sessions.append({"session_id": "plain-1", "alive": True, "kind": "pty",
                               "name": "a coding session", "flags": ""})
        assert _steer(relay, "plain-1").json()["detail"]["state"] == "not_this_skill"

    def test_unknown_or_dead_sessions_are_not_found(self, relay):
        relay.sessions.append(_conversation_session("dead-1", RESUMABLE_SID, alive=False))
        for sid in ("nope", "dead-1"):
            resp = _steer(relay, sid)
            assert resp.status_code == 404
            assert resp.json()["detail"]["state"] == "not_found"


# ------------------------------------------------------------ actions
class TestActions:
    def test_find_lists_this_skill_only_and_marks_open_ones(self, relay):
        relay.sessions.append(_conversation_session("conv-1", RESUMABLE_SID))
        body = relay.client.get(f"{PREFIX}/conversations", headers=relay.headers).json()
        assert body["state"] == "found" and body["skill"] == "journal-daily"
        paths = [c["path"] for c in body["conversations"]]
        assert all("school-notes" not in p for p in paths)
        ferry = next(c for c in body["conversations"] if c["path"].endswith("ferry-booking.md"))
        assert ferry["resumable"] is True and ferry["session_id"] == "conv-1"

    def test_find_search_is_narrowed_to_the_skill(self, relay, monkeypatch):
        from app.webapp.routers import life_os_relay
        seen = {}

        async def fake_search(cfg, query, skill, limit):
            seen.update(query=query, skill=skill)
            return {"available": True, "results": []}

        monkeypatch.setattr(life_os_relay, "run_conversation_search", fake_search)
        body = relay.client.get(f"{PREFIX}/conversations", headers=relay.headers,
                                params={"q": "ferry", "skill": "school-notes"}).json()
        assert seen == {"query": "ferry", "skill": "journal-daily"}
        assert body["state"] == "none"

    def test_resume_launches_without_channels_or_mirror(self, relay, monkeypatch):
        from app.webapp.routers import _helpers
        mirrored = []
        monkeypatch.setattr(_helpers, "should_mirror_to_pc", lambda *a: mirrored.append(a) or True)
        resp = relay.client.post(f"{PREFIX}/resume", headers=relay.headers, json={})
        assert resp.status_code == 200, resp.text
        assert resp.json()["state"] == "resumed" and resp.json()["session_id"] == "new-1"
        (spawn,) = relay.spawned
        assert f"--resume {RESUMABLE_SID}" in spawn.flags
        # The target never gets the plugin, the settings file or the bot's label;
        # it is marked as the relay's so the Board does not read it as the owner's.
        assert "--channels" not in spawn.flags and "--settings" not in spawn.flags
        assert spawn.label == "relay:health"
        assert mirrored == []

    def test_resume_of_an_open_conversation_opens_it(self, relay):
        relay.sessions.append(_conversation_session("conv-1", RESUMABLE_SID))
        body = relay.client.post(f"{PREFIX}/resume", headers=relay.headers, json={}).json()
        assert body == {**body, "state": "opened", "session_id": "conv-1"}
        assert relay.spawned == []

    def test_resume_refuses_another_skills_path(self, relay):
        other = str(Path(".claude/skills/school-notes/conversations/2026-09-01-0800-homework.md"))
        resp = relay.client.post(f"{PREFIX}/resume", headers=relay.headers, json={"path": other})
        assert resp.status_code == 404
        assert resp.json()["detail"]["state"] == "not_found"
        assert relay.spawned == []

    def test_resume_takes_a_path_from_find(self, relay):
        found = relay.client.get(f"{PREFIX}/conversations", headers=relay.headers).json()
        by_name = {Path(c["path"]).name: c for c in found["conversations"]}
        ferry = by_name["2026-08-01-0900-ferry-booking.md"]
        resp = relay.client.post(f"{PREFIX}/resume", headers=relay.headers,
                                 json={"path": ferry["path"]})
        assert resp.json()["state"] == "resumed"
        # A capture with no session id is listed but not resumable.
        notion = by_name["2026-07-02-1030-notion-schema.md"]
        assert notion["resumable"] is False
        resp = relay.client.post(f"{PREFIX}/resume", headers=relay.headers,
                                 json={"path": notion["path"]})
        assert resp.status_code == 409
        assert resp.json()["detail"]["state"] == "not_resumable"
        assert len(relay.spawned) == 1

    @pytest.mark.parametrize("verdict, state", [
        ({"ok": True, "reason": "ok", "submit_state": "confirmed"}, "delivered"),
        ({"ok": True, "reason": "deferred", "submit_state": "pending"}, "queued"),
        ({"ok": True, "reason": "unverified", "submit_state": "unconfirmed"}, "unconfirmed"),
    ])
    def test_steer_names_its_outcome(self, relay, verdict, state):
        relay.sessions.append(_conversation_session("conv-1", RESUMABLE_SID))
        relay.send_input.return_value = verdict
        resp = _steer(relay, "conv-1")
        assert resp.status_code == 200, resp.text
        assert resp.json()["state"] == state
        relay.send_input.assert_called_once()
        assert relay.send_input.call_args.args[1:] == ("conv-1", "hello", True)

    @pytest.mark.parametrize("status, state", [(502, "stranded"), (409, "gone"),
                                               (500, "unknown")])
    def test_steer_failures_are_distinct_and_never_retried(self, relay, status, state):
        from src.clients.session import SessionHostError
        relay.sessions.append(_conversation_session("conv-1", RESUMABLE_SID))
        relay.send_input.side_effect = SessionHostError("no", status=status)
        resp = _steer(relay, "conv-1")
        assert resp.json()["detail"]["state"] == state
        assert relay.send_input.call_count == 1

    def test_steer_always_submits(self, relay):
        relay.sessions.append(_conversation_session("conv-1", RESUMABLE_SID))
        assert _steer(relay, "conv-1", submit=False).status_code == 400
        relay.send_input.assert_not_called()

    def test_read_returns_the_reply_or_says_why_not(self, relay):
        relay.sessions.append(_conversation_session("conv-1", RESUMABLE_SID,
                                                    last_output_at="2026-10-17T10:00:01Z"))
        get = lambda: relay.client.get(f"{PREFIX}/read", headers=relay.headers,  # noqa: E731
                                       params={"session_id": "conv-1"}).json()
        body = get()
        assert body["state"] == "reply" and body["text"] == "PONG"
        assert body["timestamp"] == "2026-10-17T10:00:00Z"
        relay.exchange = {**relay.exchange, "stale": True}
        assert get() == {**get(), "state": "unavailable", "reason": "stale"}
        assert "text" not in get()
        relay.exchange = {"available": False, "reason": "capture_unparseable"}
        assert get()["reason"] == "capture_unparseable"


# ------------------------------------------------------------ bot chats
CHANNEL_TURN = ('<channel source="plugin:telegram:telegram" chat_id="1" user="someone">'
                "an invented message</channel>")
HELD_SID = "1a7b3c5d-8e2f-4a6b-9c0d-3e5f7a9b1c2d"
PAST_BOT_SID = "9d1e6c3a-2b4f-4e8a-b7c1-5f0a3d9e2c64"
LATER_SID = "5b8c2d4e-7f1a-4c3b-8d2e-6a9f0b1c3d5e"


def _add_journal_conversation(root: Path, file: str, sid: str, turns: list, newer_by: int) -> dict:
    """A capture whose user turns are ``turns``, made the newest by ``newer_by`` seconds."""
    folder = root / ".claude" / "skills" / "journal-daily" / "conversations"
    body = "".join(f"**You**: {turn}\n\n**Claude**: an invented reply\n\n" for turn in turns)
    capture = folder / file
    capture.write_text(f'<!-- capture sid="{sid}" agent="claude" updated="synthetic" -->\n\n'
                       f"{body}", encoding="utf-8")
    stamp = capture.stat().st_mtime + newer_by
    os.utime(capture, (stamp, stamp))
    row = {"skill": "journal-daily", "file": file, "date": file[:10], "slug": "x",
           "sid": sid, "agent": "claude", "topic": "an invented topic"}
    index = folder / "index.json"
    index.write_text(json.dumps([*json.loads(index.read_text(encoding="utf-8")), row]),
                     encoding="utf-8")
    return row


class TestBotChatsNeverOffered:
    """A bot's own chat (live, or one whose capture opens on a channel
    message) is never found or resumed: resuming it would attach a second
    session to the bot's channel transcript."""

    @pytest.fixture
    def bots(self, relay, life_os_client):
        root = life_os_client[2]["life_os_dir"]
        relay.sessions[0]["flags"] = f"--resume {HELD_SID}"  # the running bot's own chat
        relay.rows = [
            _add_journal_conversation(root, "2026-10-31-0900-held.md", HELD_SID,
                                      ["a plain invented opening"], 300),
            _add_journal_conversation(root, "2026-10-30-0900-past-bot.md", PAST_BOT_SID,
                                      [CHANNEL_TURN], 200),
            # Only the first user turn decides: a later channel message does not.
            _add_journal_conversation(root, "2026-10-29-0900-later.md", LATER_SID,
                                      ["a plain invented opening", CHANNEL_TURN], 100),
        ]
        return relay

    def _names(self, body):
        return [Path(c["path"]).name for c in body["conversations"]]

    def test_find_leaves_them_out_before_its_limit(self, bots):
        get = lambda **q: bots.client.get(  # noqa: E731
            f"{PREFIX}/conversations", headers=bots.headers, params=q).json()
        names = self._names(get())
        assert "2026-10-31-0900-held.md" not in names
        assert "2026-10-30-0900-past-bot.md" not in names
        assert names[0] == "2026-10-29-0900-later.md" and len(names) == 4
        assert self._names(get(limit="1")) == ["2026-10-29-0900-later.md"]

    def test_search_leaves_them_out_and_still_fills_its_limit(self, bots, monkeypatch):
        from app.webapp.routers import life_os_conversations, life_os_relay
        ranked = [*bots.rows, {"skill": "journal-daily", "file": "2026-08-01-0900-ferry-booking.md"}]

        async def fake_search(cfg, query, skill, limit):
            hits = life_os_conversations._conversation_rows(cfg, ranked[:limit])
            return {"available": True, "results": hits}

        monkeypatch.setattr(life_os_relay, "run_conversation_search", fake_search)
        body = bots.client.get(f"{PREFIX}/conversations", headers=bots.headers,
                               params={"q": "invented", "limit": "2"}).json()
        assert self._names(body) == ["2026-10-29-0900-later.md",
                                     "2026-08-01-0900-ferry-booking.md"]

    def test_resume_skips_them_and_refuses_their_path(self, bots):
        resp = bots.client.post(f"{PREFIX}/resume", headers=bots.headers, json={})
        assert resp.json()["state"] == "resumed", resp.text
        assert f"--resume {LATER_SID}" in bots.spawned[0].flags
        past = str(Path(".claude/skills/journal-daily/conversations/2026-10-30-0900-past-bot.md"))
        resp = bots.client.post(f"{PREFIX}/resume", headers=bots.headers, json={"path": past})
        assert resp.status_code == 409
        assert resp.json()["detail"]["state"] == "not_resumable"
        assert len(bots.spawned) == 1


# ------------------------------------------------------------ settings file
class TestRelaySettings:
    def test_relay_off_writes_the_file_exactly_as_before(self, tmp_path, monkeypatch):
        monkeypatch.setenv("APP_LAUNCHER_DATA_DIR", (tmp_path / "data").as_posix())
        state = tmp_path / "h"
        state.mkdir()
        profile = channel_profiles.ChannelProfile("health", "Health", "health", state.as_posix())
        path = channel_profiles.write_channel_settings(profile, relay_url="https://127.0.0.1:1")
        assert json.loads(Path(path).read_text(encoding="utf-8")) == {
            "env": {"TELEGRAM_STATE_DIR": state.as_posix()}}

    def test_relay_on_allows_exactly_the_relay_command(self, tmp_path, monkeypatch):
        monkeypatch.setenv("APP_LAUNCHER_DATA_DIR", (tmp_path / "data").as_posix())
        state = tmp_path / "h"
        state.mkdir()
        profile = channel_profiles.ChannelProfile("health", "Health", "health",
                                                  state.as_posix(), relay=True)
        path = channel_profiles.write_channel_settings(profile, relay_url="https://127.0.0.1:1")
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        command = channel_profiles.relay_command()
        assert command.endswith("scripts/telegram_relay.py")
        assert data["permissions"] == {"allow": [f"Bash({command}:*)"]}
        env = data["env"]
        assert env["LAUNCHER_RELAY_COMMAND"] == command
        assert env["LAUNCHER_RELAY_PROFILE"] == "health"
        assert env["LAUNCHER_RELAY_URL"] == "https://127.0.0.1:1"
        # The context filter's rewrite would hide the command from the rule.
        assert env["FLEET_CONTEXT_FILTER_MODE"] == "off"
        assert len(env["LAUNCHER_RELAY_TOKEN"]) >= 32
        assert channel_profiles.relay_token_matches(profile, env["LAUNCHER_RELAY_TOKEN"])
        assert not channel_profiles.relay_token_matches(profile, "")
        off = channel_profiles.ChannelProfile("health", "Health", "health", state.as_posix())
        assert not channel_profiles.relay_token_matches(off, env["LAUNCHER_RELAY_TOKEN"])

    def test_only_a_json_true_turns_relay_on(self, tmp_path):
        rows = []
        for index, value in enumerate([True, "true", 1, None]):
            d = tmp_path / f"s{index}"
            d.mkdir()
            rows.append({"id": f"p{index}", "skill": "x", "state_dir": d.as_posix(),
                         "relay": value})
        path = tmp_path / "p.json"
        path.write_text(json.dumps({"profiles": rows}), encoding="utf-8")
        profiles, _ = channel_profiles.load_channel_profiles(path)
        assert [p.relay for p in profiles] == [True, False, False, False]
