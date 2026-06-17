"""Life OS tab API — list, launch, content browser, gating (issue #102)."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from app.webapp.routers.life_os import resolve_within, _recap_staleness


# --------------------------------------------------------------- path jail
class TestResolveWithin:
    def test_accepts_simple_relative_path(self, tmp_path: Path):
        (tmp_path / "a").mkdir()
        (tmp_path / "a" / "f.md").write_text("hi", encoding="utf-8")
        out = resolve_within(tmp_path, "a/f.md")
        assert out is not None and out.name == "f.md"

    def test_rejects_parent_traversal(self, tmp_path: Path):
        root = tmp_path / "life-os"
        root.mkdir()
        (tmp_path / "secret.txt").write_text("nope", encoding="utf-8")
        assert resolve_within(root, "../secret.txt") is None

    def test_rejects_absolute_path(self, tmp_path: Path):
        root = tmp_path / "life-os"
        root.mkdir()
        # An absolute path joined under the root resolves outside it.
        assert resolve_within(root, str(tmp_path / "secret.txt")) is None

    def test_rejects_empty(self, tmp_path: Path):
        assert resolve_within(tmp_path, "") is None


# ------------------------------------------------------- recap staleness (#167)
class TestRecapStaleness:
    """Pure threshold mapping — amber past 7 days, red past 14."""

    def test_never_when_no_ledger(self):
        assert _recap_staleness(None) == "never"

    def test_fresh_inclusive_of_7_days(self):
        assert _recap_staleness(0.0) == "fresh"
        assert _recap_staleness(7.0) == "fresh"

    def test_due_just_past_7(self):
        assert _recap_staleness(7.01) == "due"
        assert _recap_staleness(14.0) == "due"

    def test_overdue_past_14(self):
        assert _recap_staleness(14.01) == "overdue"
        assert _recap_staleness(99.0) == "overdue"


# --------------------------------------------------------------- fixtures
def _make_life_os(root: Path) -> Path:
    """Build a minimal life-os layout with one skill + identity."""
    skill = root / ".claude" / "skills" / "journal-daily"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: journal-daily\ndescription: Journal from a transcript.\n---\n# journal-daily\n",
        encoding="utf-8",
    )
    (skill / "description.md").write_text("Public blurb.", encoding="utf-8")
    (skill / "memory").mkdir()
    (skill / "memory" / "observations.md").write_text(
        "# obs\n\nprivate note", encoding="utf-8"
    )
    (skill / "conversations").mkdir()
    (skill / "conversations" / "2026-06-01-1917-trial.md").write_text(
        "trial log", encoding="utf-8"
    )
    # The placeholder that keeps an empty conversations/ tracked — must stay
    # un-deletable / un-renameable.
    (skill / "conversations" / ".gitkeep").write_text("", encoding="utf-8")
    identity = root / "identity"
    identity.mkdir()
    (identity / "who-i-am.md").write_text("# who\n\nme", encoding="utf-8")
    return root


@pytest.fixture
def life_os_client(webapp_client, tmp_path):
    """webapp_client with life_os_dir pointed at a temp life-os checkout."""
    client, app, overrides = webapp_client
    life_os = _make_life_os(tmp_path / "life-os")
    app.state.webapp_config.life_os_dir = str(life_os)
    overrides["life_os_dir"] = life_os
    return client, app, overrides


# --------------------------------------------------------------- list
class TestListSkills:
    def test_lists_skills(self, life_os_client):
        client, _, _ = life_os_client
        resp = client.get("/api/life-os/skills")
        assert resp.status_code == 200
        body = resp.json()
        assert body["available"] is True
        ids = [s["id"] for s in body["skills"]]
        assert ids == ["journal-daily"]
        assert body["skills"][0]["command"] == "journal-daily"

    def test_unavailable_when_dir_missing(self, webapp_client, tmp_path):
        client, app, _ = webapp_client
        app.state.webapp_config.life_os_dir = str(tmp_path / "nope")
        resp = client.get("/api/life-os/skills")
        assert resp.status_code == 200
        body = resp.json()
        assert body["available"] is False
        assert body["skills"] == []


# --------------------------------------------------------------- launch
class TestLaunchSkill:
    def test_launch_pty_sonnet_appends_skill_command(
        self, life_os_client, monkeypatch
    ):
        client, _, _ = life_os_client
        from app.webapp.routers import life_os as life_os_router

        captured = {}

        def fake_spawn(project_dir, name, flags, port, kind, agent):
            captured.update(
                project_dir=str(project_dir), flags=flags, kind=kind, agent=agent
            )
            return {"session_id": "s1", "kind": kind}

        monkeypatch.setattr(life_os_router, "spawn_claude_session", fake_spawn)
        resp = client.post(
            "/api/life-os/skills/journal-daily/launch",
            json={"mode": "pty", "opus": False},
        )
        assert resp.status_code == 200, resp.text
        # cwd is the life-os root; bare /skill is the positional prompt;
        # opus off → sonnet; agent is always claude.
        assert captured["agent"] == "claude"
        assert captured["kind"] == "pty"
        assert captured["flags"].endswith(" /journal-daily")
        assert "--model sonnet" in captured["flags"]
        assert "--remote-control" in captured["flags"]

    def test_launch_opus_overrides_model(self, life_os_client, monkeypatch):
        client, _, _ = life_os_client
        from app.webapp.routers import life_os as life_os_router

        captured = {}

        def fake_spawn(project_dir, name, flags, port, kind, agent):
            captured["flags"] = flags
            return {"session_id": "s1", "kind": kind}

        monkeypatch.setattr(life_os_router, "spawn_claude_session", fake_spawn)
        resp = client.post(
            "/api/life-os/skills/journal-daily/launch",
            json={"opus": True},
        )
        assert resp.status_code == 200, resp.text
        assert "--model opus" in captured["flags"]

    def test_launch_resume_streams_pty_when_detached_off(
        self, life_os_client, monkeypatch
    ):
        """Resume (issue #151) reopens Claude's picker: it swaps in
        `--resume` and drops the /<skill> positional prompt. With Detached
        off (mode=pty) it streams the picker to the phone over a PTY."""
        client, _, _ = life_os_client
        from app.webapp.routers import life_os as life_os_router

        captured = {}

        def fake_spawn(project_dir, name, flags, port, kind, agent):
            captured.update(flags=flags, kind=kind, agent=agent)
            return {"session_id": "s1", "kind": kind}

        monkeypatch.setattr(life_os_router, "spawn_claude_session", fake_spawn)
        resp = client.post(
            "/api/life-os/skills/journal-daily/launch",
            json={"mode": "pty", "resume": True, "opus": True},
        )
        assert resp.status_code == 200, resp.text
        assert captured["kind"] == "pty"
        assert captured["agent"] == "claude"
        assert "--resume" in captured["flags"]
        assert "/journal-daily" not in captured["flags"]
        # The opus override still applies to the resumed session's model.
        assert "--model opus" in captured["flags"]
        assert resp.json()["resume"] is True

    def test_launch_resume_with_detached_renders_in_remote_console(
        self, life_os_client, monkeypatch
    ):
        """Detached + Resume are orthogonal (issue #157, matching the Coding
        tab): a resume with mode=remote honours the requested mode and spawns
        a detached console (kind=remote), still swapping in `--resume` and
        dropping the /<skill> prompt. Resume no longer forces a PTY."""
        client, _, _ = life_os_client
        from app.webapp.routers import life_os as life_os_router

        captured = {}

        def fake_spawn(project_dir, name, flags, port, kind, agent):
            captured.update(flags=flags, kind=kind, agent=agent)
            return {"session_id": "s1", "kind": kind}

        monkeypatch.setattr(life_os_router, "spawn_claude_session", fake_spawn)
        resp = client.post(
            "/api/life-os/skills/journal-daily/launch",
            json={"mode": "remote", "resume": True, "opus": True},
        )
        assert resp.status_code == 200, resp.text
        assert captured["kind"] == "remote"
        assert captured["agent"] == "claude"
        assert "--resume" in captured["flags"]
        assert "/journal-daily" not in captured["flags"]
        # The opus override still applies to the resumed session's model.
        assert "--model opus" in captured["flags"]
        assert resp.json()["resume"] is True

    def test_launch_unknown_skill_404(self, life_os_client):
        client, _, _ = life_os_client
        resp = client.post("/api/life-os/skills/does-not-exist/launch", json={})
        assert resp.status_code == 404


# --------------------------------------------------------------- gating
class TestContentGate:
    def test_files_refused_over_cloudflare(self, life_os_client):
        client, _, _ = life_os_client
        resp = client.get(
            "/api/life-os/skills/journal-daily/files",
            headers={"Cf-Ray": "abc-123"},
        )
        assert resp.status_code == 403
        assert "public tunnel" in resp.json()["detail"].lower()

    def test_file_refused_off_tailnet(self, life_os_client):
        client, _, _ = life_os_client
        # TestClient connects as host 'testclient' (not loopback, not
        # tailnet) → the terminal gate refuses it.
        resp = client.get("/api/life-os/file?path=identity/who-i-am.md")
        assert resp.status_code == 403


# --------------------------------------------------------------- content
class TestContentBrowser:
    """Treat the TestClient host as loopback so the terminal gate is
    skipped and the endpoint logic (file tree, path-jail) is exercised —
    the gate itself is covered by TestContentGate above."""

    @pytest.fixture(autouse=True)
    def _bypass_gate(self, monkeypatch):
        from app.webapp import middleware
        monkeypatch.setattr(
            middleware,
            "LOOPBACK_HOSTS",
            frozenset({"testclient", "127.0.0.1", "::1", "localhost"}),
        )

    def test_files_lists_public_and_private(self, life_os_client):
        client, _, _ = life_os_client
        resp = client.get("/api/life-os/skills/journal-daily/files")
        assert resp.status_code == 200, resp.text
        files = resp.json()["files"]
        cats = {f["category"] for f in files}
        # Public skill files + private memory + shared identity.
        assert "skill" in cats
        assert "memory" in cats
        assert "identity" in cats
        paths = {f["path"] for f in files}
        assert any(p.endswith("observations.md") for p in paths)
        # Row labels drop the leading directory once it's the category —
        # the section header already shows it (#118). The full path is
        # untouched (the file endpoints rely on it).
        by_cat = {f["category"]: f for f in files if f["category"] == "memory"}
        mem = by_cat["memory"]
        assert mem["name"] == "observations.md"
        assert mem["path"].replace("\\", "/").endswith("memory/observations.md")
        conv = next(f for f in files if f["category"] == "conversations"
                    and f["name"] != ".gitkeep")
        assert "/" not in conv["name"] and "\\" not in conv["name"]
        # Top-level skill files keep their bare name (no prefix to drop).
        skill_names = {f["name"] for f in files if f["category"] == "skill"}
        assert "SKILL.md" in skill_names

    def test_file_content_returned(self, life_os_client):
        client, _, _ = life_os_client
        resp = client.get("/api/life-os/file?path=identity/who-i-am.md")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "me" in body["content"]
        assert body["truncated"] is False

    def test_file_path_jail_rejects_traversal(self, life_os_client):
        client, _, _ = life_os_client
        resp = client.get("/api/life-os/file?path=../../../../etc/hosts")
        assert resp.status_code == 400
        assert "escape" in resp.json()["detail"].lower()

    # --- delete: conversation logs only ---------------------------------
    def _conv_path(self, life_os):
        rel = (
            life_os / ".claude" / "skills" / "journal-daily"
            / "conversations" / "2026-06-01-1917-trial.md"
        ).relative_to(life_os)
        return str(rel).replace("\\", "/")

    def test_delete_conversation_log(self, life_os_client):
        client, _, overrides = life_os_client
        life_os = overrides["life_os_dir"]
        rel = self._conv_path(life_os)
        target = life_os / rel
        assert target.is_file()
        resp = client.request("DELETE", f"/api/life-os/file?path={rel}")
        assert resp.status_code == 200, resp.text
        assert resp.json()["deleted"] == rel
        assert not target.exists()

    def test_delete_source_file_refused(self, life_os_client):
        client, _, overrides = life_os_client
        life_os = overrides["life_os_dir"]
        rel = ".claude/skills/journal-daily/SKILL.md"
        resp = client.request("DELETE", f"/api/life-os/file?path={rel}")
        assert resp.status_code == 403
        assert (life_os / rel).is_file()  # untouched

    def test_delete_memory_file_refused(self, life_os_client):
        client, _, overrides = life_os_client
        life_os = overrides["life_os_dir"]
        rel = ".claude/skills/journal-daily/memory/observations.md"
        resp = client.request("DELETE", f"/api/life-os/file?path={rel}")
        assert resp.status_code == 403
        assert (life_os / rel).is_file()

    def test_delete_traversal_rejected(self, life_os_client):
        client, _, _ = life_os_client
        resp = client.request(
            "DELETE", "/api/life-os/file?path=../../../../etc/hosts"
        )
        assert resp.status_code == 400

    def test_delete_gitkeep_refused(self, life_os_client):
        client, _, overrides = life_os_client
        life_os = overrides["life_os_dir"]
        rel = ".claude/skills/journal-daily/conversations/.gitkeep"
        resp = client.request("DELETE", f"/api/life-os/file?path={rel}")
        assert resp.status_code == 403
        assert (life_os / rel).is_file()  # untouched

    # --- rename: keep the date prefix, swap the slug --------------------
    def test_rename_keeps_date_prefix(self, life_os_client):
        client, _, overrides = life_os_client
        life_os = overrides["life_os_dir"]
        rel = self._conv_path(life_os)
        resp = client.post(
            "/api/life-os/file/rename",
            json={"path": rel, "slug": "Use Personal Journal"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["name"] == "2026-06-01-1917-use-personal-journal.md"
        old = life_os / rel
        new = old.with_name("2026-06-01-1917-use-personal-journal.md")
        assert not old.exists()
        assert new.is_file()

    def test_rename_sanitizes_slug(self, life_os_client):
        client, _, overrides = life_os_client
        life_os = overrides["life_os_dir"]
        rel = self._conv_path(life_os)
        resp = client.post(
            "/api/life-os/file/rename",
            json={"path": rel, "slug": "  Foo / Bar!! "},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["name"] == "2026-06-01-1917-foo-bar.md"

    def test_rename_empty_slug_refused(self, life_os_client):
        client, _, overrides = life_os_client
        life_os = overrides["life_os_dir"]
        rel = self._conv_path(life_os)
        resp = client.post(
            "/api/life-os/file/rename", json={"path": rel, "slug": "!!!"}
        )
        assert resp.status_code == 400
        assert (life_os / rel).is_file()  # untouched

    def test_rename_collision_refused(self, life_os_client):
        client, _, overrides = life_os_client
        life_os = overrides["life_os_dir"]
        conv = (
            life_os / ".claude" / "skills" / "journal-daily" / "conversations"
        )
        (conv / "2026-06-01-1917-taken.md").write_text("x", encoding="utf-8")
        rel = self._conv_path(life_os)
        resp = client.post(
            "/api/life-os/file/rename", json={"path": rel, "slug": "taken"}
        )
        assert resp.status_code == 409
        assert (life_os / rel).is_file()  # original untouched

    def test_rename_source_file_refused(self, life_os_client):
        client, _, overrides = life_os_client
        life_os = overrides["life_os_dir"]
        rel = ".claude/skills/journal-daily/SKILL.md"
        resp = client.post(
            "/api/life-os/file/rename", json={"path": rel, "slug": "evil"}
        )
        assert resp.status_code == 403
        assert (life_os / rel).is_file()

    def test_rename_gitkeep_refused(self, life_os_client):
        client, _, overrides = life_os_client
        life_os = overrides["life_os_dir"]
        rel = ".claude/skills/journal-daily/conversations/.gitkeep"
        resp = client.post(
            "/api/life-os/file/rename", json={"path": rel, "slug": "nope"}
        )
        assert resp.status_code == 403
        assert (life_os / rel).is_file()


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
    def test_launch_invokes_weekly_recap_review(self, life_os_client, monkeypatch):
        client, _, _ = life_os_client
        from app.webapp.routers import life_os as life_os_router

        captured = {}

        def fake_spawn(project_dir, name, flags, port, kind, agent):
            captured.update(flags=flags, kind=kind, name=name, agent=agent)
            return {"session_id": "r1", "kind": kind}

        monkeypatch.setattr(life_os_router, "spawn_claude_session", fake_spawn)
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
        from app.webapp.routers import life_os as life_os_router

        captured = {}

        def fake_spawn(project_dir, name, flags, port, kind, agent):
            captured.update(flags=flags, kind=kind)
            return {"session_id": "r1", "kind": kind}

        monkeypatch.setattr(life_os_router, "spawn_claude_session", fake_spawn)
        resp = client.post(
            "/api/life-os/recap/launch", json={"mode": "remote", "opus": True}
        )
        assert resp.status_code == 200, resp.text
        assert captured["kind"] == "remote"
        assert "--model opus" in captured["flags"]
