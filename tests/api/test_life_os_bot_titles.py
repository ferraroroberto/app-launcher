"""Life OS API: a bot chat's row carries a fixed ``Telegram bot - <skill>`` title,
and the list leaves bot chats out for the Life tab's System group."""

from __future__ import annotations

import json

import pytest

from src.life_os.bot_titles import (
    bot_conversation_ids,
    bot_rule,
    bot_title,
    pending_bot_rows,
    split_system_rows,
)
from tests.api._life_os_support import RESUMABLE_SID
from tests.api.test_life_os_delete_rename import _stub_title_cli


class TestDecision:
    """The bot / non-bot call and the no-overwrite rule, without the web layer."""

    def test_a_telegram_label_marks_the_conversation_as_a_bot_chat(self):
        groups = {
            "aaa": [{"session_id": "s1", "label": "telegram:health"}],
            "bbb": [{"session_id": "s2", "label": ""}],
            "ccc": [{"session_id": "s3"}],
            "ddd": [{"session_id": "s4", "label": "chief"}],
            "eee": [{"session_id": "s5", "label": ""}, {"session_id": "s6", "label": "telegram:money"}],
        }
        assert bot_conversation_ids(groups) == {"aaa", "eee"}

    def test_an_owner_titled_or_unreadable_row_is_never_pending(self):
        rows = [
            {"skill": "health", "path": "p1", "sid": "AAA", "title": ""},
            {"skill": "health", "path": "p2", "sid": "aaa", "title": "My own name"},
            {"skill": "health", "path": "", "sid": "aaa", "title": ""},
            {"skill": "health", "path": "p4", "sid": "bbb", "title": ""},
        ]
        assert [r["path"] for r in pending_bot_rows(rows, {"aaa"})] == ["p1"]

    def test_the_title_names_the_skill(self):
        assert bot_title("health") == "Telegram bot - health"

    def test_each_marker_alone_makes_a_system_row(self):
        assert bot_rule({"sid": "AAA"}, {"aaa"}) == "live-label"
        assert bot_rule({"sid": "bbb", "bot_chat": True, "bot_chat_rule": "channel-turn"},
                        {"aaa"}) == "channel-turn"
        assert bot_rule({"sid": "bbb", "title": bot_title("health")}, set()) == "bot-title"
        assert bot_rule({"sid": "bbb", "title": "Ferry plans"}, {"aaa"}) == ""

    def test_the_split_keeps_every_row_and_its_order(self):
        rows = [{"sid": "a"}, {"sid": "b", "title": bot_title("x")}, {"sid": "c"},
                {"sid": "d", "bot_chat": True}]
        normal, system = split_system_rows(rows, set())
        assert [r["sid"] for r in normal] == ["a", "c"]
        assert [r["sid"] for r in system] == ["b", "d"]


class TestBotChatRow:
    """GET /api/life-os/skills/{id}/conversations over a live ``telegram:*`` session."""

    ENDPOINT = "/api/life-os/skills/journal-daily/conversations"
    CAPTURE = "2026-08-01-0900-ferry-booking.md"
    OTHER_CAPTURE = "2026-07-02-1030-notion-schema.md"
    REL = f".claude/skills/journal-daily/conversations/{CAPTURE}"

    @pytest.fixture(autouse=True)
    def _isolated(self, life_os_client, tmp_path, monkeypatch):
        from src.clients import session as session_client

        self.calls: list = []
        _stub_title_cli(tmp_path, monkeypatch, self.calls)
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
        self.sessions = [self._bot()]
        monkeypatch.setattr(session_client, "list_sessions", lambda port: self.sessions)
        self.monkeypatch = monkeypatch
        self.client, _, overrides = life_os_client
        self.folder = overrides["life_os_dir"] / ".claude/skills/journal-daily/conversations"

    @staticmethod
    def _bot(label="telegram:journal", sid=RESUMABLE_SID):
        return {"session_id": "s1", "alive": True, "label": label,
                "flags": f"--model opus --resume {sid}"}

    def _lists(self):
        resp = self.client.get(self.ENDPOINT)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        return ({r["file"]: r for r in body["conversations"]},
                {r["file"]: r for r in body["system"]})

    def _rows(self):
        """Every row the list answers, the System ones included."""
        normal, system = self._lists()
        return {**normal, **system}

    def _add_capture(self, file, sid, body, title=""):
        capture = self.folder / file
        capture.write_text(f'<!-- capture sid="{sid}" agent="claude" updated="synthetic" -->\n\n'
                           f"{body}", encoding="utf-8")
        index = self.folder / "index.json"
        row = {"skill": "journal-daily", "file": file, "date": file[:10], "slug": "x",
               "sid": sid, "agent": "claude", "topic": "an invented topic"}
        if title:
            row["title"] = title
        index.write_text(json.dumps([*json.loads(index.read_text("utf-8")), row]),
                         encoding="utf-8")

    def _writes(self):
        return [c for c in self.calls if any(a.startswith("--title=") for a in c)]

    def test_a_bot_chat_row_gets_the_fixed_title_and_keeps_it_across_refreshes(self):
        rows = self._rows()
        assert rows[self.CAPTURE]["title"] == "Telegram bot - journal-daily"
        assert rows[self.CAPTURE]["topic"] == "booking the ferry"
        assert self._rows()[self.CAPTURE]["title"] == "Telegram bot - journal-daily"
        assert len(self._writes()) == 1, "the second refresh finds it titled and writes nothing"

    def test_a_bot_chat_is_listed_under_system_and_not_among_the_conversations(self):
        normal, system = self._lists()
        assert list(system) == [self.CAPTURE]
        assert self.CAPTURE not in normal and self.OTHER_CAPTURE in normal
        assert system[self.CAPTURE]["title"] == "Telegram bot - journal-daily"
        assert system[self.CAPTURE]["path"], "still readable, so the reader can open it"

    def test_a_bot_chat_stays_system_after_its_session_is_gone(self):
        self._lists()                       # the live read writes the fixed title
        self.sessions = []                  # a session-host restart
        normal, system = self._lists()
        assert list(system) == [self.CAPTURE]
        assert self.CAPTURE not in normal

    def test_a_renamed_bot_chat_with_channel_turns_stays_system_without_a_session(self):
        channel = ('**You**: <channel source="plugin:telegram:telegram" chat_id="1">'
                   "an invented message</channel>\n\n**Claude**: an invented reply\n")
        self._add_capture("2026-07-03-0900-renamed.md", "5b8c2d4e-7f1a-4c3b-8d2e-6a9f0b1c3d5e",
                          channel, title="Ferry plans")
        self.sessions = []
        normal, system = self._lists()
        assert "2026-07-03-0900-renamed.md" in system
        assert "2026-07-03-0900-renamed.md" not in normal

    def test_an_untitled_unheld_row_with_no_bot_marker_stays_a_normal_conversation(self):
        self._add_capture("2026-07-03-0900-plain.md", "2f4a6c8e-0b1d-4e3f-9a5c-7d9b1e3f5a70",
                          "**You**: a plain invented opening\n\n**Claude**: an invented reply\n")
        self.sessions = []
        normal, system = self._lists()
        assert "2026-07-03-0900-plain.md" in normal and system == {}

    def test_every_other_row_is_untouched(self):
        rows = self._rows()
        assert rows[self.OTHER_CAPTURE]["title"] == ""
        assert all(r["title"] == "" for f, r in rows.items() if f != self.CAPTURE)

    @pytest.mark.parametrize("label", ["", "chief", "telegram"])
    def test_a_normal_session_on_the_conversation_changes_nothing(self, label):
        self.sessions = [self._bot(label=label)]
        assert all(r["title"] == "" for r in self._rows().values())
        assert self._writes() == []

    def test_an_owner_rename_is_never_overwritten(self):
        assert self.client.patch(
            self.ENDPOINT, params={"path": self.REL}, json={"title": "Ferry plans"}).status_code == 200
        self.calls.clear()
        assert self._rows()[self.CAPTURE]["title"] == "Ferry plans"
        assert self._writes() == []

    def test_a_title_set_between_the_read_and_the_write_survives(self):
        from src.life_os import bot_titles

        real = bot_titles.read_index_entry

        def owner_renamed_meanwhile(path):
            index = self.folder / "index.json"
            data = json.loads(index.read_text("utf-8"))
            for row in data:
                if row["file"] == path.name:
                    row["title"] = "Renamed meanwhile"
            index.write_text(json.dumps(data), encoding="utf-8")
            return real(path)
        self.monkeypatch.setattr(bot_titles, "read_index_entry", owner_renamed_meanwhile)
        self._rows()
        assert self._writes() == []

    def test_an_unreachable_session_host_leaves_the_list_as_it_was(self):
        from src.clients import session as session_client

        def down(port):
            raise session_client.SessionHostError("down")
        self.monkeypatch.setattr(session_client, "list_sessions", down)
        normal, system = self._lists()
        assert all(r["title"] == "" for r in {**normal, **system}.values())
        assert self._writes() == []
        assert system == {}, "only a live label marks this one, and it is unknown, not absent"

    def test_an_unreachable_session_host_still_reads_the_markers_that_outlive_it(self):
        from src.clients import session as session_client

        self._lists()                       # titles the bot chat while it is live

        def down(port):
            raise session_client.SessionHostError("down")
        self.monkeypatch.setattr(session_client, "list_sessions", down)
        assert list(self._lists()[1]) == [self.CAPTURE]

    def test_a_failing_title_writer_leaves_the_row_on_its_topic(self):
        self.monkeypatch.setenv("STUB_EXIT", "5")
        row = self._rows()[self.CAPTURE]
        assert row["title"] == "" and row["topic"] == "booking the ferry"
