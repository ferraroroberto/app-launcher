"""Life OS API: the conversation list, targeted resume, the conversation gate and transcripts."""

from __future__ import annotations

import os
from datetime import datetime

import pytest

from tests.api._life_os_support import RESUMABLE_SID


# ------------------------------------------------- conversations (issue #727)
class TestConversationsList:
    """The digested per-skill index the Conversations view browses.

    The gate has its own coverage in TestConversationGate below, so it is
    bypassed here to exercise this endpoint's own contract: ordering, the
    derived capture path, and the resumability rule.
    """

    @pytest.fixture(autouse=True)
    def _bypass_gate(self, monkeypatch):
        from app.webapp import middleware
        monkeypatch.setattr(
            middleware,
            "LOOPBACK_HOSTS",
            frozenset({"testclient", "127.0.0.1", "::1", "localhost"}),
        )

    def test_lists_newest_first(self, life_os_client):
        client, _, _ = life_os_client
        resp = client.get("/api/life-os/skills/journal-daily/conversations")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["available"] is True
        files = [c["file"] for c in body["conversations"]]
        # The fixture writes them out of order; the endpoint sorts.
        assert files == [
            "2026-08-01-0900-ferry-booking.md",
            "2026-07-02-1030-notion-schema.md",
            "2026-06-01-1917-trial.md",
        ]

    def test_derives_path_the_file_viewer_accepts(self, life_os_client):
        client, _, _ = life_os_client
        resp = client.get("/api/life-os/skills/journal-daily/conversations")
        row = resp.json()["conversations"][0]
        rel = row["path"].replace("\\", "/")
        assert rel == (
            ".claude/skills/journal-daily/conversations/"
            "2026-08-01-0900-ferry-booking.md"
        )
        # The whole point of deriving it: the existing viewer can open it.
        assert client.get("/api/life-os/file?path=" + rel).status_code == 200

    def test_digest_fields_survive(self, life_os_client):
        client, _, _ = life_os_client
        resp = client.get("/api/life-os/skills/journal-daily/conversations")
        row = resp.json()["conversations"][0]
        assert row["topic"] == "booking the ferry"
        assert row["decisions"] == "took the 07:40"
        assert row["open_loops"] == "confirm the return leg"
        assert row["turns"] == 12

    def test_summary_passes_through_for_both_digest_shapes(self, life_os_client):
        """#1528: the newer index writes one ``summary`` paragraph
        (fleet-config#1373); older entries have only decisions / open loops.
        Both shapes reach the client, and an old entry reads as an empty
        summary rather than a missing key."""
        client, _, _ = life_os_client
        resp = client.get("/api/life-os/skills/journal-daily/conversations")
        rows = {r["file"]: r for r in resp.json()["conversations"]}
        assert rows["2026-07-02-1030-notion-schema.md"]["summary"] == (
            "Settled the property types for the reading list.")
        assert rows["2026-08-01-0900-ferry-booking.md"]["summary"] == ""

    def test_resumable_only_for_claude_with_canonical_sid(self, life_os_client):
        """The rule the launch route enforces, computed server-side.

        A UI that offered a resume the launch route would 400 is worse than
        no resume at all, so ``resumable`` is derived here rather than
        trusted from the index: non-claude agents and missing or malformed
        ids are all readable but not reopenable.
        """
        client, _, _ = life_os_client
        rows = {
            c["file"]: c for c in client.get(
                "/api/life-os/skills/journal-daily/conversations"
            ).json()["conversations"]
        }
        assert rows["2026-08-01-0900-ferry-booking.md"]["resumable"] is True
        # No stored session id — the pre-fleet-config#586 half of the archive.
        assert rows["2026-07-02-1030-notion-schema.md"]["resumable"] is False
        # A codex conversation, with a non-canonical id besides.
        assert rows["2026-06-01-1917-trial.md"]["resumable"] is False

    def test_available_false_when_index_absent(self, life_os_client):
        """A skill the indexer hasn't reached yet is an honest empty state."""
        client, _, overrides = life_os_client
        index = (
            overrides["life_os_dir"] / ".claude" / "skills" / "journal-daily"
            / "conversations" / "index.json"
        )
        index.unlink()
        resp = client.get("/api/life-os/skills/journal-daily/conversations")
        assert resp.status_code == 200, resp.text
        assert resp.json() == {
            "skill": "journal-daily", "available": False, "conversations": []
        }

    def test_available_false_when_index_corrupt(self, life_os_client):
        """A half-written index degrades; it never 500s the tab."""
        client, _, overrides = life_os_client
        index = (
            overrides["life_os_dir"] / ".claude" / "skills" / "journal-daily"
            / "conversations" / "index.json"
        )
        index.write_text("{not json", encoding="utf-8")
        resp = client.get("/api/life-os/skills/journal-daily/conversations")
        assert resp.status_code == 200, resp.text
        assert resp.json()["available"] is False

    def test_last_interaction_tracks_the_capture_mtime(self, life_os_client):
        """#886: ``last_interaction`` is the capture file's mtime, not its name.

        life-os rewrites the same ``.md`` when a resumed session appends
        turns, so mtime is the only "when was I last in this" signal that
        exists — the date-stamped filename (and therefore ``date``) keeps
        recording creation. Both must survive side by side; the UI sorts on
        one and shows the other.
        """
        client, _, overrides = life_os_client
        capture = (
            overrides["life_os_dir"] / ".claude" / "skills" / "journal-daily"
            / "conversations" / "2026-06-01-1917-trial.md"
        )
        touched = datetime(2026, 9, 3, 14, 30).timestamp()
        os.utime(capture, (touched, touched))

        rows = {
            c["file"]: c for c in client.get(
                "/api/life-os/skills/journal-daily/conversations"
            ).json()["conversations"]
        }
        row = rows["2026-06-01-1917-trial.md"]
        assert row["date"] == "2026-06-01"          # creation, untouched
        assert row["last_interaction"] == "2026-09-03"

    def test_last_interaction_falls_back_to_the_created_date(self, life_os_client):
        """An index row whose capture is gone still sorts (#886).

        A derived field that can fail to resolve must not come back empty —
        an empty string sorts as "oldest ever" and would bury the row. The
        creation date is the honest fallback.
        """
        client, _, overrides = life_os_client
        (
            overrides["life_os_dir"] / ".claude" / "skills" / "journal-daily"
            / "conversations" / "2026-07-02-1030-notion-schema.md"
        ).unlink()

        rows = {
            c["file"]: c for c in client.get(
                "/api/life-os/skills/journal-daily/conversations"
            ).json()["conversations"]
        }
        row = rows["2026-07-02-1030-notion-schema.md"]
        assert row["last_interaction"] == row["date"] == "2026-07-02"

    def test_unknown_skill_404(self, life_os_client):
        client, _, _ = life_os_client
        resp = client.get("/api/life-os/skills/nope/conversations")
        assert resp.status_code == 404


class TestTargetedResume:
    @pytest.fixture(autouse=True)
    def _bypass_gate(self, monkeypatch):
        from app.webapp import middleware
        monkeypatch.setattr(middleware, "LOOPBACK_HOSTS", frozenset({"testclient"}))

    """Resume one exact conversation — ``--resume <sid>``, not the picker."""

    def _spawn_capture(self, monkeypatch):
        from app.webapp.routers import life_os_spawn
        captured = {}

        def fake_spawn(project_dir, name, flags, port, kind, agent,
                       rows=40, cols=120, history_lines=None):
            captured.update(
                project_dir=str(project_dir), flags=flags, kind=kind,
                agent=agent,
            )
            return {"session_id": "s1", "kind": kind}

        monkeypatch.setattr(life_os_spawn, "spawn_claude_session", fake_spawn)
        return captured

    def test_resume_sid_pins_the_conversation(self, life_os_client, monkeypatch):
        client, _, overrides = life_os_client
        captured = self._spawn_capture(monkeypatch)
        resp = client.post(
            "/api/life-os/skills/journal-daily/launch",
            json={"mode": "pty", "resume_sid": RESUMABLE_SID},
        )
        assert resp.status_code == 200, resp.text
        # The id is pinned to claude's resume token — no picker, no /<skill>.
        assert f"--resume {RESUMABLE_SID}" in captured["flags"]
        assert "/journal-daily" not in captured["flags"]
        assert not captured["flags"].endswith(" /resume")
        # Still cwd'd in life-os: `claude --resume` only finds sessions
        # belonging to the cwd's project.
        assert captured["project_dir"] == str(overrides["life_os_dir"])
        body = resp.json()
        assert body["resume"] is True
        assert body["resume_sid"] == RESUMABLE_SID

    def test_resume_sid_honours_detached_and_model(
        self, life_os_client, monkeypatch
    ):
        """Targeted resume is orthogonal to Detached, exactly like #151's."""
        client, _, _ = life_os_client
        captured = self._spawn_capture(monkeypatch)
        resp = client.post(
            "/api/life-os/skills/journal-daily/launch",
            json={
                "mode": "remote", "model": "opus", "resume_sid": RESUMABLE_SID,
            },
        )
        assert resp.status_code == 200, resp.text
        assert captured["kind"] == "remote"
        assert "--model opus" in captured["flags"]
        assert f"--resume {RESUMABLE_SID}" in captured["flags"]

    @pytest.mark.parametrize(
        "bad",
        [
            "not-a-uuid",
            "../../etc/passwd",
            RESUMABLE_SID + " --dangerously-skip-permissions",
            "e70b4cb1-9f3d-4a21-8c55-2b7d19a4f6e",
            "; rm -rf /",
        ],
    )
    def test_malformed_sid_refused_before_spawn(
        self, life_os_client, monkeypatch, bad
    ):
        """The id reaches a command line, so it is validated by construction
        rather than sanitised — a near-miss is refused, not repaired."""
        client, _, _ = life_os_client
        from app.webapp.routers import life_os_spawn

        def boom(*a, **k):  # pragma: no cover - must not run
            raise AssertionError("a malformed sid must never reach the spawn")

        monkeypatch.setattr(life_os_spawn, "spawn_claude_session", boom)
        resp = client.post(
            "/api/life-os/skills/journal-daily/launch",
            json={"mode": "pty", "resume_sid": bad},
        )
        assert resp.status_code == 400, resp.text

    def test_bare_resume_still_opens_the_picker(
        self, life_os_client, monkeypatch
    ):
        """#151's toggle is untouched by #727 — no sid means the picker."""
        client, _, _ = life_os_client
        captured = self._spawn_capture(monkeypatch)
        resp = client.post(
            "/api/life-os/skills/journal-daily/launch",
            json={"mode": "pty", "resume": True},
        )
        assert resp.status_code == 200, resp.text
        assert captured["flags"].endswith(" /resume")
        assert "--resume" not in captured["flags"]
        assert resp.json()["resume_sid"] == ""

    def test_bare_codex_resume_opens_codex_picker(
        self, life_os_client, monkeypatch
    ):
        client, _, _ = life_os_client
        captured = self._spawn_capture(monkeypatch)
        resp = client.post(
            "/api/life-os/skills/journal-daily/launch",
            json={
                "mode": "pty",
                "resume": True,
                "model": "codex:gpt-6-astra",
            },
        )
        assert resp.status_code == 200, resp.text
        assert captured["agent"] == "codex"
        assert captured["flags"].startswith("resume --model gpt-6-astra")
        assert "SKILL.md" not in captured["flags"]

    def test_claude_conversation_id_rejects_codex_model(
        self, life_os_client, monkeypatch
    ):
        client, _, _ = life_os_client
        from app.webapp.routers import life_os_spawn

        def boom(*a, **k):  # pragma: no cover - must not run
            raise AssertionError("cross-provider resume must not spawn")

        monkeypatch.setattr(life_os_spawn, "spawn_claude_session", boom)
        resp = client.post(
            "/api/life-os/skills/journal-daily/launch",
            json={
                "model": "codex:gpt-6-astra",
                "resume_sid": RESUMABLE_SID,
            },
        )
        assert resp.status_code == 400, resp.text
        assert "only resume with Claude" in resp.json()["detail"]


class TestConversationGate:
    """Digests and search hits quote terminal content — same gate as Browse."""

    def test_conversations_refused_over_cloudflare(self, life_os_client):
        client, _, _ = life_os_client
        resp = client.get(
            "/api/life-os/skills/journal-daily/conversations",
            headers={"Cf-Ray": "abc-123"},
        )
        assert resp.status_code == 403
        assert "public tunnel" in resp.json()["detail"].lower()

    def test_search_refused_over_cloudflare(self, life_os_client):
        client, _, _ = life_os_client
        resp = client.get(
            "/api/life-os/conversations/search?q=ferry",
            headers={"Cf-Ray": "abc-123"},
        )
        assert resp.status_code == 403

    def test_conversations_refused_off_tailnet(self, life_os_client):
        client, _, _ = life_os_client
        resp = client.get("/api/life-os/skills/journal-daily/conversations")
        assert resp.status_code == 403

    def test_search_refused_off_tailnet(self, life_os_client):
        client, _, _ = life_os_client
        resp = client.get("/api/life-os/conversations/search?q=ferry")
        assert resp.status_code == 403


class TestConversationTranscript:
    """/api/life-os/file/transcript (#1119) — the viewer's parsed read.

    Same gate and jail as the raw file read, narrowed to conversation logs.
    Captures written here are synthetic; the real checkout is never read.
    """

    _CAPTURE = (
        'journal-daily #1\n\n'
        f'<!-- capture sid="{RESUMABLE_SID}" agent="claude" updated="synthetic" -->\n\n'
        '**You**: book the ferry\n\n'
        '**Claude**: Booked the 07:40.\n'
    )

    def _rel(self, life_os, name="2026-08-01-0900-ferry-booking.md"):
        return f".claude/skills/journal-daily/conversations/{name}"

    @pytest.fixture
    def _loopback(self, monkeypatch):
        from app.webapp import middleware
        monkeypatch.setattr(
            middleware, "LOOPBACK_HOSTS",
            frozenset({"testclient", "127.0.0.1", "::1", "localhost"}),
        )

    def test_refused_off_tailnet(self, life_os_client):
        # No _loopback fixture here: the gate is the point of this one.
        client, _, overrides = life_os_client
        rel = self._rel(overrides["life_os_dir"])
        assert client.get(f"/api/life-os/file/transcript?path={rel}").status_code == 403

    def test_refused_over_cloudflare(self, life_os_client, _loopback):
        client, _, overrides = life_os_client
        rel = self._rel(overrides["life_os_dir"])
        resp = client.get(
            f"/api/life-os/file/transcript?path={rel}",
            headers={"Cf-Ray": "abc-123"},
        )
        assert resp.status_code == 403
        assert "public tunnel" in resp.json()["detail"].lower()

    def test_returns_chat_entries(self, life_os_client, _loopback):
        client, _, overrides = life_os_client
        life_os = overrides["life_os_dir"]
        rel = self._rel(life_os)
        (life_os / rel).write_text(self._CAPTURE, encoding="utf-8")
        resp = client.get(f"/api/life-os/file/transcript?path={rel}")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["available"] is True and body["agent"] == "claude"
        assert [e["kind"] for e in body["entries"]] == ["user", "assistant"]
        assert body["entries"][0]["text"] == "book the ferry"
        assert body["truncated"] is False

    def test_unparseable_capture_is_unavailable_not_empty(
        self, life_os_client, _loopback
    ):
        client, _, overrides = life_os_client
        life_os = overrides["life_os_dir"]
        rel = self._rel(life_os, "2026-06-01-1917-trial.md")   # "trial log"
        body = client.get(f"/api/life-os/file/transcript?path={rel}").json()
        assert body["available"] is False and body["reason"] == "no_turns"
        assert body["entries"] == []

    def test_path_jail_rejects_traversal(self, life_os_client, _loopback):
        client, _, _ = life_os_client
        resp = client.get(
            "/api/life-os/file/transcript?path=../../../../etc/hosts"
        )
        assert resp.status_code == 400
        assert "escape" in resp.json()["detail"].lower()

    def test_refuses_a_file_outside_conversations(self, life_os_client, _loopback):
        # Reading arbitrary private knowledge through the transcript route
        # would widen the raw viewer's own narrow delete/rename guard.
        client, _, _ = life_os_client
        resp = client.get(
            "/api/life-os/file/transcript?path=identity/who-i-am.md"
        )
        assert resp.status_code == 403
        assert "conversation logs" in resp.json()["detail"]

    def test_missing_file_is_404(self, life_os_client, _loopback):
        client, _, _ = life_os_client
        resp = client.get(
            "/api/life-os/file/transcript"
            "?path=.claude/skills/journal-daily/conversations/nope.md"
        )
        assert resp.status_code == 404

    def test_oversized_capture_reports_truncation(
        self, life_os_client, _loopback, monkeypatch
    ):
        from app.webapp.routers import life_os_files
        monkeypatch.setattr(life_os_files, "_MAX_TRANSCRIPT_BYTES", 120)
        client, _, overrides = life_os_client
        life_os = overrides["life_os_dir"]
        rel = self._rel(life_os)
        (life_os / rel).write_text(
            "**You**: " + "x" * 400 + "\n", encoding="utf-8"
        )
        body = client.get(f"/api/life-os/file/transcript?path={rel}").json()
        assert body["available"] is True and body["truncated"] is True
