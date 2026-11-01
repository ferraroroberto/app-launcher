"""Life OS API: recap status and launch, last-used skills, resume-latest, model memory and skill icons."""

from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path

import pytest

from tests.api.test_life_os_conversations import TestTargetedResume as _TargetedResume
from tests.api._life_os_support import OTHER_SID, RESUMABLE_SID


# ------------------------------------------------------- recap-status endpoint
def _write_ledger(life_os: Path, age_days: float) -> Path:
    """Create a _recap ledger whose mtime is ``age_days`` in the past."""
    led = life_os / ".claude" / "skills" / "_recap" / "memory" / "ledger.json"
    led.parent.mkdir(parents=True, exist_ok=True)
    led.write_text("{}", encoding="utf-8")
    when = time.time() - age_days * 86400.0
    os.utime(led, (when, when))
    return led


class TestRecapStatus:
    def test_never_when_no_ledger(self, life_os_client):
        client, _, _ = life_os_client
        resp = client.get("/api/life-os/recap-status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["available"] is True
        assert body["ledger_exists"] is False
        assert body["age_days"] is None
        assert body["staleness"] == "never"
        assert body["proposal_pending"] is False

    def test_fresh_recent_ledger(self, life_os_client):
        client, _, overrides = life_os_client
        _write_ledger(overrides["life_os_dir"], 2.0)
        body = client.get("/api/life-os/recap-status").json()
        assert body["ledger_exists"] is True
        assert body["staleness"] == "fresh"
        assert 1.5 < body["age_days"] < 2.5

    def test_due_amber(self, life_os_client):
        client, _, overrides = life_os_client
        _write_ledger(overrides["life_os_dir"], 9.0)
        assert client.get("/api/life-os/recap-status").json()["staleness"] == "due"

    def test_overdue_red(self, life_os_client):
        client, _, overrides = life_os_client
        _write_ledger(overrides["life_os_dir"], 20.0)
        body = client.get("/api/life-os/recap-status").json()
        assert body["staleness"] == "overdue"

    def test_proposal_pending_surfaced(self, life_os_client):
        client, _, overrides = life_os_client
        life_os = overrides["life_os_dir"]
        _write_ledger(life_os, 9.0)
        pdir = life_os / ".claude" / "skills" / "_recap" / "proposals"
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "2026-06-01.md").write_text("older", encoding="utf-8")
        (pdir / "2026-06-12.md").write_text("newest", encoding="utf-8")
        body = client.get("/api/life-os/recap-status").json()
        assert body["proposal_pending"] is True
        # newest-first: the latest dated proposal wins.
        assert body["proposal_name"] == "2026-06-12.md"

    def test_unavailable_when_dir_missing(self, webapp_client, tmp_path):
        client, app, _ = webapp_client
        app.state.webapp_config.life_os_dir = str(tmp_path / "nope")
        body = client.get("/api/life-os/recap-status").json()
        assert body["available"] is False
        assert body["staleness"] == "never"


class TestLaunchRecap:

    @pytest.fixture(autouse=True)
    def _bypass_gate(self, monkeypatch):
        """Treat the TestClient host as loopback: the launch is passkey-gated
        since #1036, and the gate itself is covered by
        test_launch_routes_are_passkey_gated."""
        from app.webapp import middleware
        monkeypatch.setattr(
            middleware.trust,
            "LOOPBACK_HOSTS",
            frozenset({"testclient", "127.0.0.1", "::1", "localhost"}),
        )

    def test_launch_invokes_weekly_recap_review(self, life_os_client, monkeypatch):
        client, _, _ = life_os_client
        from app.webapp.routers import life_os_spawn

        captured = {}

        def fake_spawn(project_dir, name, flags, port, kind, agent,
                       rows=40, cols=120, history_lines=None):
            captured.update(flags=flags, kind=kind, name=name, agent=agent)
            return {"session_id": "r1", "kind": kind}

        monkeypatch.setattr(life_os_spawn, "spawn_claude_session", fake_spawn)
        resp = client.post(
            "/api/life-os/recap/launch", json={"mode": "pty", "opus": False}
        )
        assert resp.status_code == 200, resp.text
        assert captured["agent"] == "claude"
        assert captured["kind"] == "pty"
        # bare /weekly-recap (review), sonnet, and crucially NOT the draft mode.
        assert captured["flags"].endswith(" /weekly-recap")
        assert "--model sonnet" in captured["flags"]
        assert "draft" not in captured["flags"]
        assert resp.json()["launched"] == "weekly-recap"

    def test_launch_opus_detached(self, life_os_client, monkeypatch):
        client, _, _ = life_os_client
        from app.webapp.routers import life_os_spawn

        captured = {}

        def fake_spawn(project_dir, name, flags, port, kind, agent,
                       rows=40, cols=120, history_lines=None):
            captured.update(flags=flags, kind=kind)
            return {"session_id": "r1", "kind": kind}

        monkeypatch.setattr(life_os_spawn, "spawn_claude_session", fake_spawn)
        resp = client.post(
            "/api/life-os/recap/launch", json={"mode": "remote", "opus": True}
        )
        assert resp.status_code == 200, resp.text
        assert captured["kind"] == "remote"
        assert "--model opus" in captured["flags"]

    def test_launch_codex_astra_loads_recap_skill(
        self, life_os_client, monkeypatch
    ):
        client, _, _ = life_os_client
        from app.webapp.routers import life_os_spawn

        captured = {}

        def fake_spawn(project_dir, name, flags, port, kind, agent,
                       rows=40, cols=120, history_lines=None):
            captured.update(flags=flags, agent=agent)
            return {"session_id": "r1", "kind": kind}

        monkeypatch.setattr(life_os_spawn, "spawn_claude_session", fake_spawn)
        resp = client.post(
            "/api/life-os/recap/launch",
            json={"mode": "pty", "model": "codex:gpt-6-astra"},
        )
        assert resp.status_code == 200, resp.text
        assert captured["agent"] == "codex"
        assert "--model gpt-6-astra" in captured["flags"]
        assert ".claude/skills/_recap/SKILL.md" in captured["flags"]


CODEX_SID = "01a13256-a07b-7281-9683-11ee6a98a3be"
SKILL_CONVOS = ".claude/skills/journal-daily/conversations"


def _claude_transcript(home: Path, sid: str, model: str) -> None:
    folder = home / ".claude" / "projects" / "proj-a"
    folder.mkdir(parents=True, exist_ok=True)
    lines = [{"type": "assistant", "message": {"model": "claude-sonnet-5-5"}},
             {"type": "assistant", "message": {"model": model}}]
    (folder / f"{sid}.jsonl").write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")


def _codex_rollout(home: Path, sid: str, model: str) -> None:
    folder = home / ".codex" / "sessions" / "2026" / "09" / "03"
    folder.mkdir(parents=True, exist_ok=True)
    lines = [{"type": "session_meta", "payload": {"id": sid}},
             {"type": "turn_context", "payload": {"model": model}}]
    (folder / f"rollout-2026-09-03T08-00-00-{sid}.jsonl").write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")


class TestSkillLastUsed:
    """``last_used`` / ``has_conversations`` on the skills list: the index plus
    one stat per capture, cached by the index's mtime, never a capture read."""

    @pytest.fixture(autouse=True)
    def _fresh_cache(self, monkeypatch):
        from app.webapp.routers import life_os_conversations
        monkeypatch.setattr(life_os_conversations, "_last_used_cache", {})

    def _skill(self, client):
        return client.get("/api/life-os/skills").json()["skills"][0]

    def test_newest_capture_mtime_is_the_last_use(self, life_os_client):
        client, _, overrides = life_os_client
        folder = overrides["life_os_dir"] / SKILL_CONVOS
        stamp = datetime(2026, 9, 20, 12, 0).timestamp()
        os.utime(folder / "2026-06-01-1917-trial.md", (stamp, stamp))
        old = datetime(2026, 1, 1, 12, 0).timestamp()
        for name in ("2026-07-02-1030-notion-schema.md", "2026-08-01-0900-ferry-booking.md"):
            os.utime(folder / name, (old, old))
        skill = self._skill(client)
        # The oldest *name* was touched last: mtime wins, not the file-name date.
        assert skill["last_used"] == "2026-09-20"
        assert skill["has_conversations"] is True

    def test_no_index_is_empty_and_false(self, life_os_client):
        client, _, overrides = life_os_client
        (overrides["life_os_dir"] / SKILL_CONVOS / "index.json").unlink()
        skill = self._skill(client)
        assert skill["last_used"] == "" and skill["has_conversations"] is False

    def test_an_empty_index_has_no_conversations(self, life_os_client):
        client, _, overrides = life_os_client
        (overrides["life_os_dir"] / SKILL_CONVOS / "index.json").write_text("[]", encoding="utf-8")
        skill = self._skill(client)
        assert skill["last_used"] == "" and skill["has_conversations"] is False

    def test_cached_by_index_mtime_and_reads_no_capture(self, life_os_client, monkeypatch):
        from app.webapp.routers import life_os_conversations
        from src.life_os import history as life_os_history
        client, _, overrides = life_os_client
        reads = []
        real = life_os_conversations._read_conversation_index
        monkeypatch.setattr(life_os_conversations, "_read_conversation_index",
                            lambda path: reads.append(path) or real(path))

        def no_capture_reads(*a, **k):
            raise AssertionError("the skills list must not read a capture or spawn")
        monkeypatch.setattr(life_os_history, "_parse_capture_texts", no_capture_reads)
        monkeypatch.setattr(subprocess, "run", no_capture_reads)
        first = self._skill(client)["last_used"]
        assert self._skill(client)["last_used"] == first
        assert len(reads) == 1, "an unchanged index is not re-read"
        index = overrides["life_os_dir"] / SKILL_CONVOS / "index.json"
        later = time.time() + 5
        os.utime(index, (later, later))
        self._skill(client)
        assert len(reads) == 2, "a rewritten index is read again"


class TestResumeLatest:
    """POST .../conversations/resume-latest — fictional captures, a temporary
    home for transcripts, and a stubbed spawn: nothing real is launched."""

    ENDPOINT = "/api/life-os/skills/journal-daily/conversations/resume-latest"

    @pytest.fixture(autouse=True)
    def _isolated(self, life_os_client, tmp_path, monkeypatch):
        from app.webapp import middleware
        from src.clients import session as session_client
        from src.life_os.transcript import claude_projects_dir
        monkeypatch.setattr(middleware.trust, "LOOPBACK_HOSTS", frozenset({"testclient"}))
        home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        assert tmp_path in claude_projects_dir().parents, "must never touch the real home"
        self.home = home
        self.sessions: list = []
        monkeypatch.setattr(session_client, "list_sessions", lambda port: self.sessions)
        self.client, self.app, overrides = life_os_client
        self.folder = overrides["life_os_dir"] / SKILL_CONVOS
        self.captured = _TargetedResume()._spawn_capture(monkeypatch)

    def _add_capture(self, name, sid, *, agent="claude", mtime=None):
        path = self.folder / name
        path.write_text(f'<!-- capture sid="{sid}" agent="{agent}" updated="synthetic" -->\nlog',
                        encoding="utf-8")
        index = self.folder / "index.json"
        rows = json.loads(index.read_text(encoding="utf-8"))
        rows.append({"skill": "journal-daily", "file": name, "date": name[:10],
                     "slug": "synthetic", "sid": sid, "agent": agent, "topic": "a fictional topic"})
        index.write_text(json.dumps(rows), encoding="utf-8")
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def test_is_passkey_gated(self):
        from app.webapp import middleware
        assert middleware._terminal_guard_level(self.ENDPOINT) == "passkey"

    def test_newest_by_mtime_resumes_on_its_own_model(self):
        now = time.time()
        os.utime(self.folder / "2026-08-01-0900-ferry-booking.md", (now - 86400, now - 86400))
        # Created later by its name, but last touched before the ferry capture.
        self._add_capture("2026-09-03-0800-seed-order.md", OTHER_SID, mtime=now - 5 * 86400)
        _claude_transcript(self.home, RESUMABLE_SID, "claude-opus-5-5")
        resp = self.client.post(self.ENDPOINT, json={"mode": "pty", "model": "claude:haiku"})
        assert resp.status_code == 200, resp.text
        flags = self.captured["flags"]
        assert f"--resume {RESUMABLE_SID}" in flags
        # A bare --resume keeps the conversation's model; the request's is ignored.
        assert "--model" not in flags
        assert "/journal-daily" not in flags
        body = resp.json()
        assert body["model_kept"] is True
        assert body["model"] == "claude-opus-5-5"
        assert body["conversation_action"] == "resume"

    def test_detached_is_honoured(self):
        resp = self.client.post(self.ENDPOINT, json={"mode": "remote"})
        assert resp.status_code == 200, resp.text
        assert self.captured["kind"] == "remote"

    def test_unknown_claude_model_still_keeps_it(self):
        resp = self.client.post(self.ENDPOINT, json={})
        assert resp.status_code == 200, resp.text
        assert "--model" not in self.captured["flags"]
        assert resp.json()["model_kept"] is True and resp.json()["model"] == ""

    def test_codex_resumes_on_its_recorded_model(self):
        self.app.state.webapp_config.codex_model = "gpt-6-astra"
        self._add_capture("2026-09-03-0800-seed-order.md", CODEX_SID, agent="codex",
                          mtime=time.time() + 60)
        _codex_rollout(self.home, CODEX_SID, "gpt-5.6-terra")
        resp = self.client.post(self.ENDPOINT, json={"model": "claude:opus"})
        assert resp.status_code == 200, resp.text
        assert self.captured["agent"] == "codex"
        assert f"resume {CODEX_SID}" in self.captured["flags"]
        assert "--model gpt-5.6-terra" in self.captured["flags"]
        assert resp.json()["model_kept"] is True

    def test_codex_without_a_readable_model_says_it_was_not_kept(self):
        self.app.state.webapp_config.codex_model = "gpt-6-astra"
        self._add_capture("2026-09-03-0800-seed-order.md", CODEX_SID, agent="codex",
                          mtime=time.time() + 60)
        resp = self.client.post(self.ENDPOINT, json={})
        assert resp.status_code == 200, resp.text
        assert "--model gpt-6-astra" in self.captured["flags"]
        assert resp.json()["model_kept"] is False

    def test_no_conversation_yet_is_a_plain_409(self):
        (self.folder / "index.json").unlink()
        resp = self.client.post(self.ENDPOINT, json={})
        assert resp.status_code == 409
        assert "No conversation" in resp.json()["detail"]
        assert self.captured == {}

    def test_nothing_resumable_is_a_plain_409(self):
        (self.folder / "2026-08-01-0900-ferry-booking.md").write_text("no header", encoding="utf-8")
        resp = self.client.post(self.ENDPOINT, json={})
        assert resp.status_code == 409
        assert "can be resumed" in resp.json()["detail"]
        assert self.captured == {}

    def test_an_open_conversation_returns_its_session(self):
        self.sessions = [{"session_id": "live-1", "alive": True, "kind": "pty", "name": "fictional",
                          "flags": f"--resume {RESUMABLE_SID}"}]
        resp = self.client.post(self.ENDPOINT, json={})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["conversation_action"] == "open"
        assert body["session"]["session_id"] == "live-1"
        assert self.captured == {}

    def test_unreachable_session_host_launches_nothing(self, monkeypatch):
        from src.clients import session as session_client

        def down(port):
            raise session_client.SessionHostError("down")
        monkeypatch.setattr(session_client, "list_sessions", down)
        resp = self.client.post(self.ENDPOINT, json={})
        assert resp.status_code == 503
        assert "isn't known" in resp.json()["detail"]
        assert self.captured == {}


class TestKeepModel:
    """The explicit ``keep_model`` switch on the shared resume path."""

    def test_claude_resume_flags_drop_the_model(self, webapp_client):
        from src.launching.flags import build_resume_flags
        _, app, _ = webapp_client
        cfg = app.state.webapp_config
        kept = build_resume_flags(cfg, "claude", model_override="opus",
                                  session_id=RESUMABLE_SID, keep_model=True,
                                  effort_model="claude-opus-5-5")
        chosen = build_resume_flags(cfg, "claude", model_override="opus", session_id=RESUMABLE_SID)
        assert "--model" not in kept and "--model opus" in chosen
        assert kept == chosen.replace(" --model opus", "")

    def _resumable(self, client):
        rows = client.get("/api/life-os/skills/journal-daily/conversations").json()["conversations"]
        row = next(r for r in rows if r["resumable"])
        return {k: row[k] for k in ("path", "revision", "agent", "sid")}

    def test_absent_model_still_means_sonnet(self, life_os_client, monkeypatch):
        """An old cached client sends no model and no keep_model: unchanged."""
        from app.webapp import middleware
        monkeypatch.setattr(middleware.trust, "LOOPBACK_HOSTS", frozenset({"testclient"}))
        client, _, _ = life_os_client
        captured = _TargetedResume()._spawn_capture(monkeypatch)
        resp = client.post("/api/life-os/skills/journal-daily/conversations/launch", json={
            "action": "resume", "capture": self._resumable(client)})
        assert resp.status_code == 200, resp.text
        assert "--model sonnet" in captured["flags"]
        assert resp.json()["model_kept"] is None

    def test_keep_model_resumes_across_a_provider_mismatch(self, life_os_client, monkeypatch, tmp_path):
        """The conversation row's Resume: a Codex model in the bar no longer blocks
        resuming a Claude conversation, and the bar's model is not applied."""
        from app.webapp import middleware
        monkeypatch.setattr(middleware.trust, "LOOPBACK_HOSTS", frozenset({"testclient"}))
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
        client, _, _ = life_os_client
        captured = _TargetedResume()._spawn_capture(monkeypatch)
        resp = client.post("/api/life-os/skills/journal-daily/conversations/launch", json={
            "action": "resume", "model": "codex:gpt-6-astra", "keep_model": True,
            "capture": self._resumable(client)})
        assert resp.status_code == 200, resp.text
        assert captured["agent"] == "claude"
        assert f"--resume {RESUMABLE_SID}" in captured["flags"]
        assert "--model" not in captured["flags"]


class TestRecordedModel:
    """src.session_view.conversation_model against synthetic files in a temporary home."""

    @pytest.fixture(autouse=True)
    def _home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        self.home = tmp_path

    def test_claude_newest_assistant_model(self):
        from src.session_view.conversation_model import recorded_model
        _claude_transcript(self.home, RESUMABLE_SID, "claude-haiku-5-5")
        assert recorded_model("claude", RESUMABLE_SID) == "claude-haiku-5-5"
        assert recorded_model("claude", RESUMABLE_SID.upper()) == "claude-haiku-5-5"

    def test_codex_newest_turn_model(self):
        from src.session_view.conversation_model import recorded_model
        _codex_rollout(self.home, CODEX_SID, "gpt-5.6-terra")
        assert recorded_model("codex", CODEX_SID) == "gpt-5.6-terra"

    @pytest.mark.parametrize("agent,sid", [
        ("claude", "not-a-uuid"), ("claude", "../" + RESUMABLE_SID), ("pi", RESUMABLE_SID),
        ("claude", OTHER_SID), ("codex", OTHER_SID),
    ])
    def test_anything_else_is_not_known(self, agent, sid):
        from src.session_view.conversation_model import recorded_model
        _claude_transcript(self.home, RESUMABLE_SID, "claude-haiku-5-5")
        assert recorded_model(agent, sid) == ""

    def test_two_codex_rollouts_for_one_id_are_not_a_fact(self):
        from src.session_view.conversation_model import recorded_model
        _codex_rollout(self.home, CODEX_SID, "gpt-5.6-terra")
        twin = self.home / ".codex" / "sessions" / "2026" / "09" / "04"
        twin.mkdir(parents=True)
        (twin / f"rollout-2026-09-04T08-00-00-{CODEX_SID}.jsonl").write_text("{}", encoding="utf-8")
        assert recorded_model("codex", CODEX_SID) == ""


class TestSkillIcon:
    """The optional SKILL.md ``icon:`` (#1503): a glyph name, or nothing."""

    @pytest.mark.parametrize("raw,expected", [
        ("sprout", "sprout"), ("Book-Open", "book-open"), ("", ""),
        ("../sprout", ""), ("sprout fill", ""), ("<svg>", ""), ("-sprout", ""),
    ])
    def test_icon_is_a_glyph_name_or_empty(self, life_os_client, raw, expected):
        client, _, overrides = life_os_client
        skill_md = overrides["life_os_dir"] / ".claude/skills/journal-daily/SKILL.md"
        skill_md.write_text(
            f'---\nname: journal-daily\ndescription: A fictional skill.\nicon: "{raw}"\n---\n',
            encoding="utf-8",
        )
        assert client.get("/api/life-os/skills").json()["skills"][0]["icon"] == expected

    def test_no_icon_key_is_empty(self, life_os_client):
        client, _, _ = life_os_client
        assert client.get("/api/life-os/skills").json()["skills"][0]["icon"] == ""
