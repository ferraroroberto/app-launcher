"""Life OS tab API — list, launch, content browser, gating (issue #102),
conversation index + search + targeted resume (issue #727)."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from app.webapp.routers.life_os import (
    resolve_within,
    _recap_staleness,
    _search_cli,
)

# A canonical session id — the only shape the launch route accepts, because
# the value reaches claude's command line.
RESUMABLE_SID = "e70b4cb1-9f3d-4a21-8c55-2b7d19a4f6e0"


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
    # The digested index the Conversations view reads (#727), covering all
    # three states the UI must render: resumable, no stored session id, and a
    # non-claude agent. Deliberately written oldest-first so the endpoint's
    # newest-first ordering is a property it enforces, not one it inherits.
    (skill / "conversations" / "2026-07-02-1030-notion-schema.md").write_text(
        "notion log", encoding="utf-8"
    )
    (skill / "conversations" / "2026-08-01-0900-ferry-booking.md").write_text(
        "ferry log", encoding="utf-8"
    )
    (skill / "conversations" / "index.json").write_text(
        json.dumps([
            {
                "skill": "journal-daily",
                "file": "2026-06-01-1917-trial.md",
                "date": "2026-06-01", "slug": "trial", "turns": 4,
                "sid": "not-a-uuid", "agent": "codex",
                "topic": "a codex trial", "decisions": "none",
                "open_loops": "none",
            },
            {
                "skill": "journal-daily",
                "file": "2026-08-01-0900-ferry-booking.md",
                "date": "2026-08-01", "slug": "ferry-booking", "turns": 12,
                "sid": RESUMABLE_SID, "agent": "claude",
                "topic": "booking the ferry", "decisions": "took the 07:40",
                "open_loops": "confirm the return leg",
            },
            {
                "skill": "journal-daily",
                "file": "2026-07-02-1030-notion-schema.md",
                "date": "2026-07-02", "slug": "notion-schema", "turns": 7,
                "sid": "", "agent": "claude",
                "topic": "notion schema", "decisions": "none",
                "open_loops": "none",
            },
        ]),
        encoding="utf-8",
    )
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

        def fake_spawn(project_dir, name, flags, port, kind, agent,
                       rows=40, cols=120, history_lines=None):
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

    def test_launch_threads_phone_terminal_size(
        self, life_os_client, monkeypatch
    ):
        """Issue #374: the phone's rows/cols must size the PTY at spawn.

        A skill streams output the moment the PTY exists; spawning at the
        legacy 40×120 poured 120-col text that re-wrapped into garble when
        the overlay's first fit() shrank the PTY to phone width. Same
        contract as the Coding-tab launch route (issue #126).
        """
        client, _, _ = life_os_client
        from app.webapp.routers import life_os as life_os_router

        captured = {}

        def fake_spawn(project_dir, name, flags, port, kind, agent,
                       rows=40, cols=120, history_lines=None):
            captured.update(rows=rows, cols=cols)
            return {"session_id": "s1", "kind": kind}

        monkeypatch.setattr(life_os_router, "spawn_claude_session", fake_spawn)
        resp = client.post(
            "/api/life-os/skills/journal-daily/launch",
            json={"mode": "pty", "opus": False, "rows": 44, "cols": 54},
        )
        assert resp.status_code == 200, resp.text
        assert captured["rows"] == 44
        assert captured["cols"] == 54

    def test_launch_defaults_size_when_omitted(
        self, life_os_client, monkeypatch
    ):
        """Desktop launches send no size — the legacy 40×120 still applies."""
        client, _, _ = life_os_client
        from app.webapp.routers import life_os as life_os_router

        captured = {}

        def fake_spawn(project_dir, name, flags, port, kind, agent,
                       rows=40, cols=120, history_lines=None):
            captured.update(rows=rows, cols=cols)
            return {"session_id": "s1", "kind": kind}

        monkeypatch.setattr(life_os_router, "spawn_claude_session", fake_spawn)
        resp = client.post(
            "/api/life-os/skills/journal-daily/launch",
            json={"mode": "pty", "opus": False},
        )
        assert resp.status_code == 200, resp.text
        assert captured["rows"] == 40
        assert captured["cols"] == 120

    def test_launch_opus_overrides_model(self, life_os_client, monkeypatch):
        client, _, _ = life_os_client
        from app.webapp.routers import life_os as life_os_router

        captured = {}

        def fake_spawn(project_dir, name, flags, port, kind, agent,
                       rows=40, cols=120, history_lines=None):
            captured["flags"] = flags
            return {"session_id": "s1", "kind": kind}

        monkeypatch.setattr(life_os_router, "spawn_claude_session", fake_spawn)
        resp = client.post(
            "/api/life-os/skills/journal-daily/launch",
            json={"opus": True},
        )
        assert resp.status_code == 200, resp.text
        assert "--model opus" in captured["flags"]

    @pytest.mark.parametrize("model", ["sonnet", "opus", "fable"])
    def test_launch_model_field_sets_model_flag(
        self, life_os_client, monkeypatch, model
    ):
        """#540: the model combo sends an explicit ``model`` — each of the three
        offered Claude tiers maps to its ``--model`` flag, and the response
        echoes it back."""
        client, _, _ = life_os_client
        from app.webapp.routers import life_os as life_os_router

        captured = {}

        def fake_spawn(project_dir, name, flags, port, kind, agent,
                       rows=40, cols=120, history_lines=None):
            captured["flags"] = flags
            return {"session_id": "s1", "kind": kind}

        monkeypatch.setattr(life_os_router, "spawn_claude_session", fake_spawn)
        resp = client.post(
            "/api/life-os/skills/journal-daily/launch",
            json={"mode": "pty", "model": model},
        )
        assert resp.status_code == 200, resp.text
        assert f"--model {model}" in captured["flags"]
        assert resp.json()["model"] == model

    def test_launch_codex_astra_reads_project_skill(self, life_os_client, monkeypatch):
        """The Skills selector can launch the same skill through Codex/Astra."""
        client, _, _ = life_os_client
        from app.webapp.routers import life_os as life_os_router

        captured = {}

        def fake_spawn(project_dir, name, flags, port, kind, agent,
                       rows=40, cols=120, history_lines=None):
            captured.update(flags=flags, agent=agent)
            return {"session_id": "s1", "kind": kind}

        monkeypatch.setattr(life_os_router, "spawn_claude_session", fake_spawn)
        resp = client.post(
            "/api/life-os/skills/journal-daily/launch",
            json={"mode": "pty", "model": "codex:gpt-6-astra"},
        )
        assert resp.status_code == 200, resp.text
        assert captured["agent"] == "codex"
        assert "--model gpt-6-astra" in captured["flags"]
        assert "Use the journal-daily skill" in captured["flags"]
        assert ".claude/skills/journal-daily/SKILL.md" in captured["flags"]
        assert resp.json()["model"] == "gpt-6-astra"

    def test_launch_model_field_wins_over_legacy_opus(
        self, life_os_client, monkeypatch
    ):
        """When both are sent, the explicit ``model`` takes precedence over the
        legacy ``opus`` bool (#540 back-compat resolution order)."""
        client, _, _ = life_os_client
        from app.webapp.routers import life_os as life_os_router

        captured = {}

        def fake_spawn(project_dir, name, flags, port, kind, agent,
                       rows=40, cols=120, history_lines=None):
            captured["flags"] = flags
            return {"session_id": "s1", "kind": kind}

        monkeypatch.setattr(life_os_router, "spawn_claude_session", fake_spawn)
        resp = client.post(
            "/api/life-os/skills/journal-daily/launch",
            json={"model": "fable", "opus": True},
        )
        assert resp.status_code == 200, resp.text
        assert "--model fable" in captured["flags"]
        assert "--model opus" not in captured["flags"]

    def test_launch_rejects_unknown_model(self, life_os_client):
        """An out-of-range model is a 400, not a silent fallback (#540)."""
        client, _, _ = life_os_client
        resp = client.post(
            "/api/life-os/skills/journal-daily/launch",
            json={"model": "gpt5.6"},
        )
        assert resp.status_code == 400, resp.text

    def test_launch_resume_streams_pty_when_detached_off(
        self, life_os_client, monkeypatch
    ):
        """Resume reopens Claude's picker inside a remote-enabled session.

        Issue #526: CLI startup shape ``--resume --remote-control`` can leave
        the selected conversation unavailable on mobile. Launch Remote Control
        normally, then invoke the native picker with positional ``/resume``.
        """
        client, _, _ = life_os_client
        from app.webapp.routers import life_os as life_os_router

        captured = {}

        def fake_spawn(project_dir, name, flags, port, kind, agent,
                       rows=40, cols=120, history_lines=None):
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
        assert "--remote-control" in captured["flags"]
        assert captured["flags"].endswith(" /resume")
        assert "--resume" not in captured["flags"]
        assert "/journal-daily" not in captured["flags"]
        # The opus override still applies to the resumed session's model.
        assert "--model opus" in captured["flags"]
        assert resp.json()["resume"] is True

    def test_launch_resume_with_detached_renders_in_remote_console(
        self, life_os_client, monkeypatch
    ):
        """Detached + Resume are orthogonal (issue #157, matching the Coding
        tab): a resume with mode=remote honours the requested mode and spawns
        a detached console (kind=remote), still invoking the positional
        ``/resume`` picker and dropping the /<skill> prompt."""
        client, _, _ = life_os_client
        from app.webapp.routers import life_os as life_os_router

        captured = {}

        def fake_spawn(project_dir, name, flags, port, kind, agent,
                       rows=40, cols=120, history_lines=None):
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
        assert "--remote-control" in captured["flags"]
        assert captured["flags"].endswith(" /resume")
        assert "--resume" not in captured["flags"]
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

        def fake_spawn(project_dir, name, flags, port, kind, agent,
                       rows=40, cols=120, history_lines=None):
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

        def fake_spawn(project_dir, name, flags, port, kind, agent,
                       rows=40, cols=120, history_lines=None):
            captured.update(flags=flags, kind=kind)
            return {"session_id": "r1", "kind": kind}

        monkeypatch.setattr(life_os_router, "spawn_claude_session", fake_spawn)
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
        from app.webapp.routers import life_os as life_os_router

        captured = {}

        def fake_spawn(project_dir, name, flags, port, kind, agent,
                       rows=40, cols=120, history_lines=None):
            captured.update(flags=flags, agent=agent)
            return {"session_id": "r1", "kind": kind}

        monkeypatch.setattr(life_os_router, "spawn_claude_session", fake_spawn)
        resp = client.post(
            "/api/life-os/recap/launch",
            json={"mode": "pty", "model": "codex:gpt-6-astra"},
        )
        assert resp.status_code == 200, resp.text
        assert captured["agent"] == "codex"
        assert "--model gpt-6-astra" in captured["flags"]
        assert ".claude/skills/_recap/SKILL.md" in captured["flags"]


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

    def test_unknown_skill_404(self, life_os_client):
        client, _, _ = life_os_client
        resp = client.get("/api/life-os/skills/nope/conversations")
        assert resp.status_code == 404


def _completed(stdout: str = "", returncode: int = 0, stderr: str = ""):
    return subprocess.CompletedProcess(
        args=["python"], returncode=returncode, stdout=stdout, stderr=stderr
    )


class TestConversationSearch:
    """The ranked cross-skill search — a thin, defensive shell around
    fleet-config's own CLI. Every failure mode degrades to
    ``available: false``; none is a 500, and none leaks infrastructure
    detail to the phone."""

    @pytest.fixture(autouse=True)
    def _bypass_gate(self, monkeypatch):
        from app.webapp import middleware
        monkeypatch.setattr(
            middleware,
            "LOOPBACK_HOSTS",
            frozenset({"testclient", "127.0.0.1", "::1", "localhost"}),
        )

    @pytest.fixture
    def stub_cli(self, monkeypatch):
        """Pretend fleet-config's CLI is installed (its own resolution is
        covered by TestSearchCliResolution)."""
        from app.webapp.routers import life_os as life_os_router
        monkeypatch.setattr(
            life_os_router, "_search_cli", lambda cfg: ["py", "search.py"]
        )
        return life_os_router

    def _hit(self, life_os: Path, file_name: str, **over):
        row = {
            "skill": "journal-daily",
            "file": file_name,
            "path": str(
                life_os / ".claude" / "skills" / "journal-daily"
                / "conversations" / file_name
            ),
            "date": "2026-08-01", "slug": "ferry-booking", "turns": 12,
            "sid": RESUMABLE_SID, "agent": "claude",
            "topic": "booking the ferry", "decisions": "took the 07:40",
            "open_loops": "none", "rank": -12.5,
            "resume": "claude --resume " + RESUMABLE_SID, "resumable": True,
        }
        row.update(over)
        return row

    def test_maps_hits_and_rewrites_absolute_path(
        self, life_os_client, stub_cli, monkeypatch
    ):
        client, _, overrides = life_os_client
        hit = self._hit(
            overrides["life_os_dir"], "2026-08-01-0900-ferry-booking.md"
        )
        monkeypatch.setattr(
            stub_cli.subprocess, "run",
            lambda *a, **k: _completed(json.dumps([hit])),
        )
        resp = client.get("/api/life-os/conversations/search?q=ferry")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["available"] is True
        row = body["results"][0]
        assert row["topic"] == "booking the ferry"
        assert row["resumable"] is True
        # The CLI hands back an absolute path; the phone gets the jailed,
        # life_os-relative shape /api/life-os/file accepts.
        assert row["path"].replace("\\", "/") == (
            ".claude/skills/journal-daily/conversations/"
            "2026-08-01-0900-ferry-booking.md"
        )
        # The CLI's ready-made shell command is deliberately not forwarded.
        assert "resume" not in row

    def test_passes_cwd_query_and_skill(
        self, life_os_client, stub_cli, monkeypatch
    ):
        client, _, overrides = life_os_client
        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = argv
            seen["kwargs"] = kwargs
            return _completed("[]")

        monkeypatch.setattr(stub_cli.subprocess, "run", fake_run)
        resp = client.get(
            "/api/life-os/conversations/search?q=ferry&skill=geek-out"
        )
        assert resp.status_code == 200, resp.text
        argv = seen["argv"]
        # --cwd, not --project: the project resolves from the configured
        # life_os_dir, so a non-default checkout still searches.
        assert argv[argv.index("--cwd") + 1] == str(overrides["life_os_dir"])
        assert argv[argv.index("--query") + 1] == "ferry"
        assert argv[argv.index("--skill") + 1] == "geek-out"
        assert "--json" in argv
        assert seen["kwargs"]["timeout"] > 0

    def test_empty_query_never_spawns(
        self, life_os_client, stub_cli, monkeypatch
    ):
        """Typing then clearing the box must cost nothing."""
        client, _, _ = life_os_client

        def boom(*a, **k):  # pragma: no cover - must not run
            raise AssertionError("empty query should not spawn the CLI")

        monkeypatch.setattr(stub_cli.subprocess, "run", boom)
        resp = client.get("/api/life-os/conversations/search?q=%20%20")
        assert resp.status_code == 200, resp.text
        assert resp.json() == {
            "available": True, "query": "", "skill": "", "results": []
        }

    def test_overlong_query_refused(self, life_os_client, stub_cli):
        client, _, _ = life_os_client
        resp = client.get("/api/life-os/conversations/search?q=" + ("x" * 500))
        assert resp.status_code == 400

    def test_unavailable_when_cli_not_installed(self, life_os_client, tmp_path):
        """A machine without fleet-config keeps a working tab, minus search."""
        client, app, _ = life_os_client
        app.state.webapp_config.claude_config_dir = str(tmp_path / "nowhere")
        resp = client.get("/api/life-os/conversations/search?q=ferry")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["available"] is False
        assert body["results"] == []
        assert body["reason"]

    @pytest.mark.parametrize(
        "outcome",
        [
            pytest.param(lambda *a, **k: _completed("", 1, "no such db"),
                         id="nonzero-exit"),
            pytest.param(lambda *a, **k: _completed("not json at all"),
                         id="unparseable-stdout"),
            pytest.param(lambda *a, **k: _completed('{"not": "a list"}'),
                         id="wrong-shape"),
        ],
    )
    def test_unavailable_on_bad_cli_result(
        self, life_os_client, stub_cli, monkeypatch, outcome
    ):
        client, _, _ = life_os_client
        monkeypatch.setattr(stub_cli.subprocess, "run", outcome)
        resp = client.get("/api/life-os/conversations/search?q=ferry")
        assert resp.status_code == 200, resp.text
        assert resp.json()["available"] is False

    def test_unavailable_on_timeout(
        self, life_os_client, stub_cli, monkeypatch
    ):
        def hang(*a, **k):
            raise subprocess.TimeoutExpired(cmd="search", timeout=15)

        client, _, _ = life_os_client
        monkeypatch.setattr(stub_cli.subprocess, "run", hang)
        resp = client.get("/api/life-os/conversations/search?q=ferry")
        assert resp.status_code == 200, resp.text
        assert resp.json()["available"] is False

    def test_reason_carries_no_infrastructure_detail(
        self, life_os_client, stub_cli, monkeypatch
    ):
        """Failure copy is sanitized — stderr goes to the log, not the phone."""
        client, _, _ = life_os_client
        monkeypatch.setattr(
            stub_cli.subprocess, "run",
            lambda *a, **k: _completed(
                "", 2, "Traceback: C:/Users/rober/.venv/db.sqlite is locked"
            ),
        )
        body = client.get("/api/life-os/conversations/search?q=ferry").json()
        assert body["available"] is False
        assert "Traceback" not in body["reason"]
        assert "Users" not in body["reason"]


class TestSearchCliResolution:
    """``_search_cli`` finds fleet-config's interpreter + script, or says no."""

    def _tree(self, root: Path, *, script: bool, venv: bool) -> Path:
        if script:
            (root / "hooks").mkdir(parents=True, exist_ok=True)
            (root / "hooks" / "conversation_search.py").write_text(
                "", encoding="utf-8"
            )
        if venv:
            win = root / ".venv" / "Scripts"
            win.mkdir(parents=True, exist_ok=True)
            (win / "python.exe").write_text("", encoding="utf-8")
        return root

    def _cfg(self, root: Path):
        class _Cfg:
            claude_config_dir = str(root)
        return _Cfg()

    def test_resolves_when_both_present(self, tmp_path):
        root = self._tree(tmp_path / "fleet-config", script=True, venv=True)
        cli = _search_cli(self._cfg(root))
        assert cli is not None
        assert cli[0].endswith("python.exe")
        assert cli[1].endswith("conversation_search.py")

    def test_none_without_script(self, tmp_path):
        root = self._tree(tmp_path / "fleet-config", script=False, venv=True)
        assert _search_cli(self._cfg(root)) is None

    def test_none_without_interpreter(self, tmp_path):
        root = self._tree(tmp_path / "fleet-config", script=True, venv=False)
        assert _search_cli(self._cfg(root)) is None


class TestTargetedResume:
    """Resume one exact conversation — ``--resume <sid>``, not the picker."""

    def _spawn_capture(self, monkeypatch):
        from app.webapp.routers import life_os as life_os_router
        captured = {}

        def fake_spawn(project_dir, name, flags, port, kind, agent,
                       rows=40, cols=120, history_lines=None):
            captured.update(
                project_dir=str(project_dir), flags=flags, kind=kind,
                agent=agent,
            )
            return {"session_id": "s1", "kind": kind}

        monkeypatch.setattr(life_os_router, "spawn_claude_session", fake_spawn)
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
        from app.webapp.routers import life_os as life_os_router

        def boom(*a, **k):  # pragma: no cover - must not run
            raise AssertionError("a malformed sid must never reach the spawn")

        monkeypatch.setattr(life_os_router, "spawn_claude_session", boom)
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
        from app.webapp.routers import life_os as life_os_router

        def boom(*a, **k):  # pragma: no cover - must not run
            raise AssertionError("cross-provider resume must not spawn")

        monkeypatch.setattr(life_os_router, "spawn_claude_session", boom)
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
