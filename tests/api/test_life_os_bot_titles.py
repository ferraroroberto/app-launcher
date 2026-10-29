"""Life OS API: a bot chat's row carries a fixed ``Telegram bot - <skill>`` title."""

from __future__ import annotations

import json

import pytest

from src.life_os.bot_titles import bot_conversation_ids, bot_title, pending_bot_rows

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

    def _rows(self):
        resp = self.client.get(self.ENDPOINT)
        assert resp.status_code == 200, resp.text
        return {r["file"]: r for r in resp.json()["conversations"]}

    def _writes(self):
        return [c for c in self.calls if any(a.startswith("--title=") for a in c)]

    def test_a_bot_chat_row_gets_the_fixed_title_and_keeps_it_across_refreshes(self):
        rows = self._rows()
        assert rows[self.CAPTURE]["title"] == "Telegram bot - journal-daily"
        assert rows[self.CAPTURE]["topic"] == "booking the ferry"
        assert self._rows()[self.CAPTURE]["title"] == "Telegram bot - journal-daily"
        assert len(self._writes()) == 1, "the second refresh finds it titled and writes nothing"

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
        assert all(r["title"] == "" for r in self._rows().values())
        assert self._writes() == []

    def test_a_failing_title_writer_leaves_the_row_on_its_topic(self):
        self.monkeypatch.setenv("STUB_EXIT", "5")
        row = self._rows()[self.CAPTURE]
        assert row["title"] == "" and row["topic"] == "booking the ferry"
