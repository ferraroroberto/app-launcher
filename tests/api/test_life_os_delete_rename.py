"""Life OS API: deleting, renaming and name-syncing conversations."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime

import pytest

from src import life_os_title

from tests.api._life_os_support import RESUMABLE_SID


# ------------------------------------------------ delete a conversation (#1410)
class TestDeleteConversation:
    """DELETE /api/life-os/skills/{id}/conversations: capture + exact transcript + indexes.

    Synthetic files in a temporary home and Life OS folder only: ``HOME`` /
    ``USERPROFILE`` point at ``tmp_path`` and the fixture asserts it, so a real
    ``~/.claude/projects`` can never be reached.
    """

    ENDPOINT = "/api/life-os/skills/journal-daily/conversations"
    CAPTURE = "2026-08-01-0900-ferry-booking.md"
    NEIGHBOUR_SID = "e70b4cb1-9f3d-4a21-8c55-2b7d19a4f6e1"  # one digit off RESUMABLE_SID

    @pytest.fixture(autouse=True)
    def _isolated(self, life_os_client, tmp_path, monkeypatch):
        from app.webapp import middleware
        from src import session_client
        from src.life_os_transcript import claude_projects_dir

        monkeypatch.setattr(middleware, "LOOPBACK_HOSTS", frozenset({"testclient"}))
        home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        assert tmp_path in claude_projects_dir().parents, "must never touch the real home"
        self.projects = claude_projects_dir()
        self.folder = self.projects / "proj-a"
        self.folder.mkdir(parents=True)
        self.sessions: list = []
        monkeypatch.setattr(session_client, "list_sessions", lambda port: self.sessions)
        self.client, _, overrides = life_os_client
        self.audit = overrides["audit"]
        self.conversations = overrides["life_os_dir"] / ".claude/skills/journal-daily/conversations"

    def _delete(self, name: str = CAPTURE):
        return self.client.delete(
            self.ENDPOINT, params={"path": f".claude/skills/journal-daily/conversations/{name}"}
        )

    def _transcript(self, sid=RESUMABLE_SID, folder=None, sidecar=True):
        folder = folder or self.folder
        jsonl = folder / f"{sid}.jsonl"
        jsonl.write_text('{"synthetic": true}\n', encoding="utf-8")
        if sidecar:
            (folder / sid / "subagents").mkdir(parents=True)
            (folder / sid / "subagents" / "agent.jsonl").write_text("{}", encoding="utf-8")
        return jsonl

    def _index_files(self):
        return [r["file"] for r in json.loads((self.conversations / "index.json").read_text("utf-8"))]

    def test_exact_id_removes_capture_transcript_sidecar_and_index_row(self):
        jsonl = self._transcript()
        resp = self._delete()
        assert resp.status_code == 200, resp.text
        removed = resp.json()["removed"]
        assert removed["capture"] and removed["transcript"] == "removed"
        assert (removed["transcript_files"], removed["transcript_folders"]) == (1, 1)
        assert removed["index_json"] is True
        assert not (self.conversations / self.CAPTURE).exists()
        assert not jsonl.exists() and not (self.folder / RESUMABLE_SID).exists()
        assert self.CAPTURE not in self._index_files()
        assert len(self._index_files()) == 2
        event = self.audit.audit_event.call_args
        assert event.args == ("lifeos_conversation_delete",)
        assert event.kwargs["transcript"] == "removed" and event.kwargs["sid"] == RESUMABLE_SID

    def test_neighbouring_transcripts_are_left_alone(self):
        self._transcript()
        neighbour = self._transcript(self.NEIGHBOUR_SID)
        agent_file = self.folder / f"{RESUMABLE_SID}-agent.jsonl"
        agent_file.write_text("{}", encoding="utf-8")
        backup = self.folder / f"{RESUMABLE_SID}.jsonl.bak"
        backup.write_text("{}", encoding="utf-8")
        other_capture = self.conversations / "2026-07-02-1030-notion-schema.md"
        assert self._delete().status_code == 200
        assert neighbour.exists() and (self.folder / self.NEIGHBOUR_SID).is_dir()
        assert agent_file.exists() and backup.exists()
        assert other_capture.exists()

    def test_no_transcript_still_deletes_the_capture_and_says_so(self):
        self._transcript(self.NEIGHBOUR_SID)
        resp = self._delete()
        assert resp.status_code == 200, resp.text
        removed = resp.json()["removed"]
        assert removed["transcript"] == "not_found" and removed["transcript_files"] == 0
        assert not (self.conversations / self.CAPTURE).exists()
        assert (self.folder / f"{self.NEIGHBOUR_SID}.jsonl").exists()

    @pytest.mark.parametrize("by_hook_row", [False, True])
    def test_live_session_conversation_is_refused(self, by_hook_row, tmp_path):
        jsonl = self._transcript()
        if by_hook_row:
            # A session that moved to this conversation via /resume: only the hook row knows.
            session = {"session_id": "s1", "alive": True, "flags": "--model opus", "project_dir": "x"}
            (tmp_path / "sessions-state.json").write_text(json.dumps({
                RESUMABLE_SID: {"launcher_session_id": "s1", "agent": "claude", "cwd": "x",
                                "updated_at": datetime.now().astimezone().isoformat()},
            }), encoding="utf-8")
        else:
            session = {"session_id": "s1", "alive": True, "flags": f"--model opus --resume {RESUMABLE_SID}"}
        self.sessions = [session]
        resp = self._delete()
        assert resp.status_code == 409, resp.text
        assert "running session" in resp.json()["detail"]
        assert jsonl.exists() and (self.conversations / self.CAPTURE).exists()
        assert self.CAPTURE in self._index_files()

    def test_a_stopped_session_does_not_block(self):
        self._transcript()
        self.sessions = [{"session_id": "s1", "alive": False, "flags": f"--resume {RESUMABLE_SID}"}]
        assert self._delete().status_code == 200

    def test_unreachable_session_host_is_unknown_not_clear(self, monkeypatch):
        from src import session_client

        def down(port):
            raise session_client.SessionHostError("down")
        monkeypatch.setattr(session_client, "list_sessions", down)
        jsonl = self._transcript()
        resp = self._delete()
        assert resp.status_code == 503, resp.text
        assert jsonl.exists() and (self.conversations / self.CAPTURE).exists()

    @pytest.mark.parametrize("rel", [
        "../../../../outside.md",
        "../2026-08-01-0900-ferry-booking.md",
        ".claude/skills/journal-daily/SKILL.md",
        ".claude/skills/journal-daily/conversations/index.json",
        ".claude/skills/journal-daily/conversations/index.md",
        "C:/Windows/win.ini",
        "",
    ])
    def test_path_escape_is_refused_and_nothing_is_deleted(self, rel, tmp_path):
        canary = tmp_path / "outside.md"
        canary.write_text("canary", encoding="utf-8")
        jsonl = self._transcript()
        resp = self.client.delete(self.ENDPOINT, params={"path": rel})
        assert resp.status_code in (400, 404), resp.text
        assert canary.exists() and jsonl.exists() and (self.conversations / self.CAPTURE).exists()

    def test_a_linked_project_folder_is_never_entered(self, tmp_path):
        outside = tmp_path / "outside-projects"
        outside.mkdir()
        victim = self._transcript(folder=outside)
        link = self.projects / "linked"
        if os.name == "nt":
            import _winapi
            _winapi.CreateJunction(str(outside), str(link))
        else:
            link.symlink_to(outside, target_is_directory=True)
        try:
            resp = self._delete()
            assert resp.status_code == 200, resp.text
            assert resp.json()["removed"]["transcript"] == "not_found"
            assert victim.exists() and (outside / RESUMABLE_SID).is_dir()
        finally:
            if os.name == "nt":
                os.rmdir(link)
            else:
                link.unlink()

    def test_codex_capture_deletes_only_the_capture(self):
        (self.conversations / "2026-06-01-1917-trial.md").write_text(
            f'<!-- capture sid="{RESUMABLE_SID}" agent="codex" updated="synthetic" -->\ncodex log',
            encoding="utf-8",
        )
        jsonl = self._transcript()
        resp = self._delete("2026-06-01-1917-trial.md")
        assert resp.status_code == 200, resp.text
        assert resp.json()["removed"]["transcript"] == "not_claude"
        assert jsonl.exists()

    def test_capture_whose_header_cannot_be_read_is_refused(self, monkeypatch):
        from src import life_os_history

        def broken(fleet_dir, texts):
            raise OSError("shared interpreter missing")
        monkeypatch.setattr(life_os_history, "_parse_capture_texts", broken)
        jsonl = self._transcript()
        resp = self._delete()
        assert resp.status_code == 409, resp.text
        assert jsonl.exists() and (self.conversations / self.CAPTURE).exists()

    def test_transcript_failure_leaves_the_capture_intact(self, monkeypatch):
        from src import life_os_delete

        def locked(*args, **kwargs):
            raise PermissionError("locked")
        monkeypatch.setattr(life_os_delete, "remove_claude_transcript", locked)
        resp = self._delete()
        assert resp.status_code == 500, resp.text
        assert (self.conversations / self.CAPTURE).exists() and self.CAPTURE in self._index_files()

    def test_route_is_passkey_gated(self, monkeypatch):
        from app.webapp import middleware
        monkeypatch.setattr(middleware, "LOOPBACK_HOSTS", frozenset())
        jsonl = self._transcript()
        resp = self._delete()
        assert resp.status_code in (401, 403), resp.text
        assert jsonl.exists()


# ------------------------------------------- rename a conversation (#1495)
# A stand-in for fleet-config's hooks/conversation_title.py (fleet-config#1348):
# the same arguments, JSON line and exit codes, run against the fixture's own
# conversations folder only. STUB_EXIT forces an exit code, STUB_SLEEP holds the
# write open, STUB_LOG records each invocation's start/end.
_TITLE_STUB = r'''
import argparse, json, os, sys, time
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--cwd", required=True)
ap.add_argument("--skill", required=True)
ap.add_argument("--file", required=True)
g = ap.add_mutually_exclusive_group(required=True)
g.add_argument("--title")
g.add_argument("--clear", action="store_true")
args = ap.parse_args()

log = os.environ.get("STUB_LOG")
if log:
    with open(log, "a", encoding="utf-8") as fh:
        fh.write("start %s\n" % time.monotonic())
time.sleep(float(os.environ.get("STUB_SLEEP", "0")))
code = int(os.environ.get("STUB_EXIT", "0"))
if code:
    print("stub refusal", file=sys.stderr)
    sys.exit(code)
folder = Path(args.cwd) / ".claude" / "skills" / args.skill / "conversations"
title = "" if args.clear else " ".join(args.title.split())
if len(title) > 120:
    sys.exit(2)
if title and not (folder / args.file).is_file():
    sys.exit(3)
sidecar = folder / "titles.json"
titles = json.loads(sidecar.read_text("utf-8")) if sidecar.exists() else {}
if title:
    titles[args.file] = title
else:
    titles.pop(args.file, None)
sidecar.write_text(json.dumps(titles), encoding="utf-8")
index = folder / "index.json"
rows = json.loads(index.read_text("utf-8"))
for row in rows:
    row["title"] = titles.get(row["file"], "")
index.write_text(json.dumps(rows), encoding="utf-8")
if log:
    with open(log, "a", encoding="utf-8") as fh:
        fh.write("end %s\n" % time.monotonic())
print(json.dumps({"skill": args.skill, "file": args.file, "title": title}))
'''


def _stub_title_cli(tmp_path, monkeypatch, calls: list) -> None:
    """Point the title CLI at ``_TITLE_STUB`` and record every subprocess argv
    the conversations router runs into ``calls``."""
    import sys

    from app.webapp import middleware
    from app.webapp.routers import life_os_conversations as router

    monkeypatch.setattr(middleware, "LOOPBACK_HOSTS", frozenset({"testclient"}))
    script = tmp_path / "conversation_title_stub.py"
    script.write_text(_TITLE_STUB, encoding="utf-8")
    real_run = subprocess.run

    def recording_run(argv, **kwargs):
        calls.append(list(argv))
        return real_run(argv, **kwargs)
    monkeypatch.setattr(router.subprocess, "run", recording_run)
    monkeypatch.setattr(life_os_title, "title_cli", lambda cfg: [sys.executable, str(script)])


class TestRenameConversation:
    """PATCH /api/life-os/skills/{id}/conversations: set or clear a title."""

    ENDPOINT = "/api/life-os/skills/journal-daily/conversations"
    CAPTURE = "2026-08-01-0900-ferry-booking.md"
    REL = f".claude/skills/journal-daily/conversations/{CAPTURE}"

    @pytest.fixture(autouse=True)
    def _isolated(self, life_os_client, tmp_path, monkeypatch):
        from app.webapp.routers import life_os_conversations as router
        from src import session_client

        self.calls: list = []
        _stub_title_cli(tmp_path, monkeypatch, self.calls)
        monkeypatch.setattr(session_client, "list_sessions", lambda port: [])
        self.log = tmp_path / "stub.log"
        monkeypatch.setenv("STUB_LOG", str(self.log))
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
        self.router = router
        self.monkeypatch = monkeypatch
        self.client, _, overrides = life_os_client
        self.audit = overrides["audit"]
        self.conversations = overrides["life_os_dir"] / ".claude/skills/journal-daily/conversations"

    def _rename(self, title, rel=REL, **kwargs):
        return self.client.patch(self.ENDPOINT, params={"path": rel}, json={"title": title}, **kwargs)

    def _row(self):
        rows = self.client.get(self.ENDPOINT).json()["conversations"]
        return next(r for r in rows if r["file"] == self.CAPTURE)

    def test_rename_then_list_shows_the_title_and_keeps_the_topic(self):
        resp = self._rename("  Ferry   to the\nisland ")
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"skill": "journal-daily", "path": self.REL,
                               "file": self.CAPTURE, "title": "Ferry to the island"}
        row = self._row()
        assert row["title"] == "Ferry to the island"
        assert row["topic"] == "booking the ferry"
        others = [r for r in self.client.get(self.ENDPOINT).json()["conversations"]
                  if r["file"] != self.CAPTURE]
        assert others and all(r["title"] == "" for r in others)
        event = self.audit.audit_event.call_args
        assert event.args == ("lifeos_conversation_rename",)
        assert event.kwargs["cleared"] is False
        assert "Ferry" not in str(event), "the title text is private and must not be audited"

    @pytest.mark.parametrize("cleared", ["", "   ", None])
    def test_clearing_restores_the_topic(self, cleared):
        assert self._rename("Ferry").status_code == 200
        resp = self._rename(cleared)
        assert resp.status_code == 200, resp.text
        assert resp.json()["title"] == ""
        assert self._row()["title"] == ""
        assert "--clear" in self.calls[-1]
        assert not any(a.startswith("--title") for a in self.calls[-1])
        assert self.audit.audit_event.call_args.kwargs["cleared"] is True

    def test_a_title_that_looks_like_an_option_reaches_the_writer_whole(self):
        resp = self._rename("--clear is not a flag")
        assert resp.status_code == 200, resp.text
        assert self._row()["title"] == "--clear is not a flag"
        assert "--title=--clear is not a flag" in self.calls[-1]

    @pytest.mark.parametrize("body", [None, {}, {"name": "x"}, {"title": 5}, {"title": ["x"]}])
    def test_a_missing_or_malformed_body_is_refused_not_read_as_clear(self, body):
        assert self._rename("Kept").status_code == 200
        calls = len(self.calls)
        resp = self.client.patch(self.ENDPOINT, params={"path": self.REL}, json=body)
        assert resp.status_code == 400, resp.text
        assert len(self.calls) == calls, "refused before the writer ran"
        assert self._row()["title"] == "Kept"

    def test_over_long_title_is_refused_before_the_writer_runs(self):
        assert self._rename("x" * 121).status_code == 400
        assert self.calls == []
        assert self._rename("x" * 120).status_code == 200

    @pytest.mark.parametrize("code,status", [(1, 503), (2, 400), (3, 404), (4, 500), (9, 500)])
    def test_each_writer_exit_code_has_its_own_error(self, code, status):
        self.monkeypatch.setenv("STUB_EXIT", str(code))
        resp = self._rename("Ferry")
        assert resp.status_code == status, resp.text
        assert self._row()["title"] == ""
        assert self.audit.audit_event.call_count == 0

    def test_exit_code_messages_are_distinct(self):
        seen = set()
        for code in (1, 2, 3, 4, 9):
            self.monkeypatch.setenv("STUB_EXIT", str(code))
            seen.add(self._rename("Ferry").json()["detail"])
        assert len(seen) == 5

    def test_writer_not_installed_is_503(self):
        self.monkeypatch.setattr(life_os_title, "title_cli", lambda cfg: None)
        resp = self._rename("Ferry")
        assert resp.status_code == 503, resp.text
        assert "isn't installed" in resp.json()["detail"]

    def test_writer_that_cannot_run_is_503(self):
        self.monkeypatch.setattr(life_os_title, "title_cli", lambda cfg: ["no-such-binary-1495"])
        assert self._rename("Ferry").status_code == 503

    @pytest.mark.parametrize("rel", [
        "../../../../outside.md",
        ".claude/skills/journal-daily/SKILL.md",
        ".claude/skills/journal-daily/conversations/index.json",
        ".claude/skills/journal-daily/conversations/index.md",
        ".claude/skills/journal-daily/conversations/../SKILL.md",
        "C:/Windows/win.ini",
        "",
    ])
    def test_path_jail_matches_delete(self, rel):
        resp = self._rename("Ferry", rel=rel)
        assert resp.status_code in (400, 404), resp.text
        assert self.calls == []

    def test_unknown_capture_and_unknown_skill(self):
        missing = ".claude/skills/journal-daily/conversations/2026-01-01-0000-gone.md"
        assert self._rename("Ferry", rel=missing).status_code == 404
        resp = self.client.patch("/api/life-os/skills/no-such-skill/conversations",
                                 params={"path": self.REL}, json={"title": "x"})
        assert resp.status_code == 404
        assert self.calls == []

    def test_same_auth_as_delete(self):
        from app.webapp import middleware
        assert middleware._terminal_guard_level(self.ENDPOINT) == "passkey"
        self.monkeypatch.setattr(middleware, "LOOPBACK_HOSTS", frozenset())
        resp = self._rename("Ferry")
        assert resp.status_code in (401, 403), resp.text
        assert self.calls == []
        assert self._rename("Ferry", headers={"Cf-Ray": "synthetic"}).status_code == 403

    def test_concurrent_renames_are_serialised(self):
        from concurrent.futures import ThreadPoolExecutor

        self.monkeypatch.setenv("STUB_SLEEP", "0.4")
        names = ["2026-08-01-0900-ferry-booking.md", "2026-07-02-1030-notion-schema.md"]

        def rename(name):
            rel = f".claude/skills/journal-daily/conversations/{name}"
            return self._rename("T " + name, rel=rel).status_code
        with ThreadPoolExecutor(2) as pool:
            statuses = list(pool.map(rename, names))
        assert statuses == [200, 200]
        events = [line.split()[0] for line in self.log.read_text("utf-8").splitlines()]
        assert events == ["start", "end", "start", "end"], events

    def test_delete_clears_the_title(self):
        assert self._rename("Ferry").status_code == 200
        titles = self.conversations / "titles.json"
        assert json.loads(titles.read_text("utf-8")) == {self.CAPTURE: "Ferry"}
        resp = self.client.delete(self.ENDPOINT, params={"path": self.REL})
        assert resp.status_code == 200, resp.text
        assert resp.json()["removed"]["title"] == "cleared"
        assert json.loads(titles.read_text("utf-8")) == {}
        assert "--clear" in self.calls[-1] and self.CAPTURE in self.calls[-1]

    def test_delete_still_succeeds_when_the_title_clear_fails(self):
        self.monkeypatch.setenv("STUB_EXIT", "4")
        resp = self.client.delete(self.ENDPOINT, params={"path": self.REL})
        assert resp.status_code == 200, resp.text
        assert resp.json()["removed"]["title"] == "unavailable"
        assert not (self.conversations / self.CAPTURE).exists()

    def test_search_rows_carry_the_title(self):
        from app.webapp.routers.life_os_conversations import _conversation_rows

        cfg = self.client.app.state.webapp_config
        rows = _conversation_rows(cfg, [{
            "skill": "journal-daily", "file": self.CAPTURE, "title": "Ferry",
            "topic": "booking the ferry", "date": "2026-08-01",
        }])
        assert rows[0]["title"] == "Ferry" and rows[0]["topic"] == "booking the ferry"


# ----------------------- a conversation's name follows its session (#1637)
class TestConversationNameSync:
    """Life OS title <-> live session name, both ways, and on resume.

    The session host is faked at ``session_client`` (list + rename); the title
    writer is the same stub the rename tests use. Titles are synthetic.
    """

    ENDPOINT = TestRenameConversation.ENDPOINT
    CAPTURE = TestRenameConversation.CAPTURE
    REL = TestRenameConversation.REL
    LIVE = "s-live"

    @pytest.fixture(autouse=True)
    def _isolated(self, life_os_client, tmp_path, monkeypatch):
        from app.webapp.routers import life_os_conversations as router
        from app.webapp.routers import life_os_spawn
        from src import session_client

        self.calls: list = []
        _stub_title_cli(tmp_path, monkeypatch, self.calls)
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))

        self.sessions = [
            {"session_id": self.LIVE, "alive": True, "flags": f"--model sonnet --resume {RESUMABLE_SID}"},
            {"session_id": "s-other", "alive": True, "flags": "--model sonnet"},
        ]
        self.renamed: list = []
        self.rename_fails = False
        self.host_down = False

        def list_sessions(port):
            if self.host_down:
                raise session_client.SessionHostError("down", status=502)
            return self.sessions

        def rename(port, session_id, title):
            if self.rename_fails:
                raise session_client.SessionHostError("rename refused", status=500)
            self.renamed.append((session_id, title))
            return {"session_id": session_id, "manual_title": title}
        monkeypatch.setattr(session_client, "list_sessions", list_sessions)
        monkeypatch.setattr(session_client, "rename", rename)
        # The sessions router holds the conftest's MagicMock of the same module.
        from app.webapp.routers import sessions as sessions_router
        monkeypatch.setattr(sessions_router.session_client, "rename", rename)
        monkeypatch.setattr(sessions_router.session_client, "list_sessions", list_sessions)
        self.spawned: dict = {}

        def fake_spawn(project_dir, name, flags, port, kind, agent,
                       rows=40, cols=120, history_lines=None):
            self.spawned.update(flags=flags)
            return {"session_id": "s-new", "kind": kind}
        monkeypatch.setattr(life_os_spawn, "spawn_claude_session", fake_spawn)
        self.router = router
        self.monkeypatch = monkeypatch
        self.client, _, overrides = life_os_client
        self.audit = overrides["audit"]
        self.tmp_path = tmp_path

    def _rename_conversation(self, title):
        return self.client.patch(self.ENDPOINT, params={"path": self.REL}, json={"title": title})

    def _rename_session(self, session_id, title):
        return self.client.post(f"/api/claude-code/sessions/{session_id}/rename", json={"title": title})

    def _conversation_title(self):
        rows = self.client.get(self.ENDPOINT).json()["conversations"]
        return next(r for r in rows if r["file"] == self.CAPTURE)["title"]

    def _resume(self):
        row = next(r for r in self.client.get(self.ENDPOINT).json()["conversations"]
                   if r["file"] == self.CAPTURE)
        return self.client.post(self.ENDPOINT + "/launch", json={
            "action": "resume", "model": "claude:sonnet", "capture": {
                key: row[key] for key in ("path", "revision", "agent", "sid")}})

    # ---- Life OS -> session
    def test_life_os_rename_renames_the_live_session(self):
        resp = self._rename_conversation("Ferry to the island")
        assert resp.status_code == 200, resp.text
        assert self.renamed == [(self.LIVE, "Ferry to the island")]
        assert resp.json()["session_sync"] == {"status": "synced", "renamed": 1, "failed": 0}

    def test_every_live_session_of_the_conversation_is_renamed(self):
        self.sessions.append({"session_id": "s-twin", "alive": True,
                              "flags": f"--resume {RESUMABLE_SID.upper()}"})
        assert self._rename_conversation("Ferry").status_code == 200
        assert sorted(self.renamed) == [(self.LIVE, "Ferry"), ("s-twin", "Ferry")]

    def test_a_conversation_with_no_live_session_syncs_nothing(self):
        self.sessions[:] = [self.sessions[1]]
        resp = self._rename_conversation("Ferry")
        assert resp.status_code == 200, resp.text
        assert self.renamed == []
        assert "session_sync" not in resp.json()

    def test_a_long_title_is_truncated_into_the_session_with_an_ellipsis(self):
        title = "Ferry " + "x" * 94
        resp = self._rename_conversation(title)
        assert resp.status_code == 200, resp.text
        [(_, name)] = self.renamed
        assert len(name) == 60 and name.endswith("…") and title.startswith(name[:-1])
        assert self._conversation_title() == title, "the conversation keeps the full title"

    def test_the_session_cap_matches_the_session_host(self):
        from src import session_host
        assert self.router._SESSION_NAME_MAX_CHARS == session_host._MANUAL_TITLE_MAX_CHARS

    @pytest.mark.parametrize("cleared", ["", "   ", None])
    def test_clearing_the_title_leaves_the_session_name_alone(self, cleared):
        assert self._rename_conversation("Ferry").status_code == 200
        self.renamed.clear()
        resp = self._rename_conversation(cleared)
        assert resp.status_code == 200, resp.text
        assert self.renamed == []
        assert "session_sync" not in resp.json()

    def test_a_session_that_refuses_the_rename_never_fails_the_conversation_rename(self, caplog):
        self.rename_fails = True
        with caplog.at_level("WARNING"):
            resp = self._rename_conversation("Ferry")
        assert resp.status_code == 200, resp.text
        assert self._conversation_title() == "Ferry"
        sync = resp.json()["session_sync"]
        assert sync["status"] == "failed" and sync["failed"] == 1 and sync["error"]
        assert any("live session was not renamed" in r.getMessage() for r in caplog.records)

    def test_an_unreachable_session_host_never_fails_the_conversation_rename(self, caplog):
        self.host_down = True
        with caplog.at_level("WARNING"):
            resp = self._rename_conversation("Ferry")
        assert resp.status_code == 200, resp.text
        assert self._conversation_title() == "Ferry"
        assert resp.json()["session_sync"]["status"] == "failed"
        assert any("could not be asked which sessions" in r.getMessage() for r in caplog.records)

    def test_a_refused_conversation_rename_touches_no_session(self):
        self.monkeypatch.setenv("STUB_EXIT", "4")
        assert self._rename_conversation("Ferry").status_code == 500
        assert self.renamed == []

    # ---- session -> Life OS
    def test_session_rename_sets_the_linked_conversation_title(self):
        resp = self._rename_session(self.LIVE, "Ankle pain assessment")
        assert resp.status_code == 200, resp.text
        assert self.renamed == [(self.LIVE, "Ankle pain assessment")]
        assert self._conversation_title() == "Ankle pain assessment"
        assert resp.json()["life_os_sync"] == {"status": "synced", "conversations": 1}
        assert "Ankle" not in str(self.audit.audit_event.call_args_list)

    def test_session_rename_follows_the_board_claim_too(self):
        from src import board
        self.sessions[0]["flags"] = "--model sonnet"
        self.monkeypatch.setattr(
            board, "state_sid_for_session", lambda live, rows, sid: RESUMABLE_SID)
        assert self._rename_session(self.LIVE, "Moved here").status_code == 200
        assert self._conversation_title() == "Moved here"

    def test_an_unlinked_session_renames_exactly_as_before(self):
        resp = self._rename_session("s-other", "Just a session")
        assert resp.status_code == 200, resp.text
        assert self.renamed == [("s-other", "Just a session")]
        assert "life_os_sync" not in resp.json()
        assert self.calls == [], "the title writer never ran"

    def test_a_linked_session_to_no_capture_is_unlinked(self):
        self.sessions[0]["flags"] = "--resume 11111111-2222-4333-8444-555555555555"
        resp = self._rename_session(self.LIVE, "Orphan")
        assert resp.status_code == 200, resp.text
        assert "life_os_sync" not in resp.json() and self.calls == []

    def test_clearing_the_session_name_leaves_the_conversation_title(self):
        assert self._rename_conversation("Kept").status_code == 200
        self.calls.clear()
        resp = self._rename_session(self.LIVE, "")
        assert resp.status_code == 200, resp.text
        assert self._conversation_title() == "Kept"
        assert self.calls == []

    def test_a_title_writer_failure_never_fails_the_session_rename(self, caplog):
        self.monkeypatch.setenv("STUB_EXIT", "4")
        with caplog.at_level("WARNING"):
            resp = self._rename_session(self.LIVE, "Ankle")
        assert resp.status_code == 200, resp.text
        assert self.renamed == [(self.LIVE, "Ankle")]
        assert resp.json()["life_os_sync"]["status"] == "failed"
        assert resp.json()["life_os_sync"]["error"]
        assert any("conversation title was not updated" in r.getMessage() for r in caplog.records)

    def test_a_refused_session_rename_leaves_the_conversation_alone(self):
        self.rename_fails = True
        assert self._rename_session(self.LIVE, "Ankle").status_code == 500
        assert self._conversation_title() == "" and self.calls == []

    def test_the_two_directions_do_not_loop(self):
        assert self._rename_session(self.LIVE, "Once").status_code == 200
        assert self.renamed == [(self.LIVE, "Once")]
        assert len(self.calls) == 1

    # ---- resume
    def test_resume_opens_the_session_under_the_life_os_title(self):
        assert self._rename_conversation("Ferry to the island").status_code == 200
        self.renamed.clear()
        resp = self._resume()
        assert resp.status_code == 200, resp.text
        assert f"--resume {RESUMABLE_SID}" in self.spawned["flags"]
        assert self.renamed == [("s-new", "Ferry to the island")]
        assert resp.json()["title_sync"] == {"status": "synced"}

    def test_resume_truncates_a_long_title_like_the_live_rename(self):
        title = "y" * 100
        assert self._rename_conversation(title).status_code == 200
        self.renamed.clear()
        assert self._resume().status_code == 200
        [(_, name)] = self.renamed
        assert len(name) == 60 and name.endswith("…")

    def test_resume_of_an_untitled_conversation_names_nothing(self):
        resp = self._resume()
        assert resp.status_code == 200, resp.text
        assert self.renamed == []
        assert "title_sync" not in resp.json()

    def test_resume_survives_a_session_that_refuses_the_name(self, caplog):
        assert self._rename_conversation("Ferry").status_code == 200
        self.rename_fails = True
        with caplog.at_level("WARNING"):
            resp = self._resume()
        assert resp.status_code == 200, resp.text
        assert resp.json()["title_sync"]["status"] == "failed"
        assert any("resumed session was not named" in r.getMessage() for r in caplog.records)
