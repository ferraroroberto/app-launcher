"""Life OS API: the content browser."""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from tests.api._life_os_support import _completed


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

    def test_files_conversations_sorted_newest_first(self, life_os_client):
        # The fixture's conversation logs are date-prefixed and deliberately
        # not in date order on disk (#863) — the endpoint must reorder them
        # itself, newest-first, unlike every other A-Z category.
        client, _, _ = life_os_client
        resp = client.get("/api/life-os/skills/journal-daily/files")
        assert resp.status_code == 200, resp.text
        files = resp.json()["files"]
        convo_dates = [
            f["name"][:10] for f in files
            if f["category"] == "conversations" and f["name"][:1].isdigit()
        ]
        assert convo_dates == sorted(convo_dates, reverse=True)
        assert convo_dates == ["2026-08-01", "2026-07-02", "2026-06-01"]

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

    def _index_path(self, life_os):
        return (
            life_os / ".claude" / "skills" / "journal-daily"
            / "conversations" / "index.json"
        )

    def test_delete_conversation_log_prunes_digested_index(self, life_os_client):
        # #906: a deleted log must disappear from the Conversations view on
        # the same refresh, without waiting on the external indexer to catch
        # up — so the digested index.json needs pruning too, not just disk.
        client, _, overrides = life_os_client
        life_os = overrides["life_os_dir"]
        rel = self._conv_path(life_os)
        resp = client.request("DELETE", f"/api/life-os/file?path={rel}")
        assert resp.status_code == 200, resp.text
        rows = json.loads(self._index_path(life_os).read_text(encoding="utf-8"))
        assert "2026-06-01-1917-trial.md" not in [r["file"] for r in rows]
        conv = client.get("/api/life-os/skills/journal-daily/conversations").json()
        assert "2026-06-01-1917-trial.md" not in [c["file"] for c in conv["conversations"]]

    def test_delete_survives_missing_index(self, life_os_client):
        # The launcher doesn't own index.json's lifecycle — a skill the
        # indexer hasn't reached yet must not block deleting its raw logs.
        client, _, overrides = life_os_client
        life_os = overrides["life_os_dir"]
        self._index_path(life_os).unlink()
        rel = self._conv_path(life_os)
        resp = client.request("DELETE", f"/api/life-os/file?path={rel}")
        assert resp.status_code == 200, resp.text
        assert not (life_os / rel).exists()

    def test_delete_survives_corrupt_index(self, life_os_client):
        client, _, overrides = life_os_client
        life_os = overrides["life_os_dir"]
        self._index_path(life_os).write_text("{not json", encoding="utf-8")
        rel = self._conv_path(life_os)
        resp = client.request("DELETE", f"/api/life-os/file?path={rel}")
        assert resp.status_code == 200, resp.text
        assert not (life_os / rel).exists()

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

    def test_rename_updates_digested_index(self, life_os_client):
        # #906: same gap as delete — the index row must follow the rename so
        # the Conversations view shows the new name immediately.
        client, _, overrides = life_os_client
        life_os = overrides["life_os_dir"]
        rel = self._conv_path(life_os)
        resp = client.post(
            "/api/life-os/file/rename",
            json={"path": rel, "slug": "Use Personal Journal"},
        )
        assert resp.status_code == 200, resp.text
        rows = json.loads(self._index_path(life_os).read_text(encoding="utf-8"))
        files = [r["file"] for r in rows]
        assert "2026-06-01-1917-trial.md" not in files
        assert "2026-06-01-1917-use-personal-journal.md" in files
        conv = client.get("/api/life-os/skills/journal-daily/conversations").json()
        assert "2026-06-01-1917-use-personal-journal.md" in [c["file"] for c in conv["conversations"]]

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

    # --- rename/delete: keep index.md + cross-skill search in sync (#971) --
    # #906 fixed the browse-view mirror (conversations/index.json) but
    # explicitly deferred fleet-config's own index.md (the format index.json
    # is regenerated *from*) and its cross-skill FTS5 search db — leaving a
    # renamed/deleted conversation resolvable through Search under its stale
    # pre-change identity until an unrelated external pipeline run resyncs
    # it. These tests cover the deferred half.
    def _index_md_path(self, life_os):
        return (
            life_os / ".claude" / "skills" / "journal-daily"
            / "conversations" / "index.md"
        )

    def _stub_search_cli(self, monkeypatch, argv=("py", "conversation_search.py")):
        """Pretend fleet-config's search CLI is installed, on an isolated
        ``subprocess`` binding so patching ``.run`` can't leak into the real
        module (mirrors ``TestConversationSearch.stub_cli``)."""
        from src.life_os import index as life_os_index
        monkeypatch.setattr(life_os_index, "search_cli", lambda cfg: list(argv))
        monkeypatch.setattr(life_os_index, "subprocess", SimpleNamespace(**vars(subprocess)))
        return life_os_index

    def test_rename_updates_index_md(self, life_os_client):
        # #971: index.json's own source of truth (fleet-config's index.md)
        # must follow the rename too — otherwise the external pipeline's
        # next run can silently overwrite or orphan the index.json patch
        # #906 already made.
        client, _, overrides = life_os_client
        life_os = overrides["life_os_dir"]
        self._index_md_path(life_os).write_text(
            '<!-- idx file="2026-06-01-1917-trial.md" mtime=1 turns=4 sid="" agent="codex" -->\n'
            '### 2026-06-01 · trial\n- **Topic:** a codex trial\n',
            encoding="utf-8",
        )
        rel = self._conv_path(life_os)
        resp = client.post(
            "/api/life-os/file/rename",
            json={"path": rel, "slug": "Use Personal Journal"},
        )
        assert resp.status_code == 200, resp.text
        text = self._index_md_path(life_os).read_text(encoding="utf-8")
        assert 'file="2026-06-01-1917-trial.md"' not in text
        assert 'file="2026-06-01-1917-use-personal-journal.md"' in text

    def test_rename_survives_missing_index_md(self, life_os_client):
        # A skill the external indexer hasn't reached yet must not block a
        # rename of its raw logs (same stance as #906's index.json handling).
        client, _, overrides = life_os_client
        life_os = overrides["life_os_dir"]
        assert not self._index_md_path(life_os).exists()
        rel = self._conv_path(life_os)
        resp = client.post(
            "/api/life-os/file/rename",
            json={"path": rel, "slug": "Use Personal Journal"},
        )
        assert resp.status_code == 200, resp.text

    def test_delete_prunes_index_md(self, life_os_client):
        # #971 delete-half: index.md must lose the deleted entry's whole
        # block too — otherwise it's left holding a permanently stale
        # <!-- idx file="..." --> pointing at a file that no longer exists,
        # indistinguishable from a legacy entry the pipeline should re-digest.
        client, _, overrides = life_os_client
        life_os = overrides["life_os_dir"]
        self._index_md_path(life_os).write_text(
            '# Conversation index — journal-daily\n\n'
            '<!-- idx file="2026-06-01-1917-trial.md" mtime=1 turns=4 sid="" agent="codex" -->\n'
            '### 2026-06-01 · trial\n'
            '- **Topic:** a codex trial\n'
            '- **Decisions:** none\n'
            '- **Open loops:** none\n\n'
            '<!-- idx file="2026-08-01-0900-ferry-booking.md" mtime=1 turns=1 sid="" agent="claude" -->\n'
            '### 2026-08-01 · ferry booking\n'
            '- **Topic:** booking the ferry\n'
            '- **Decisions:** took the 07:40\n'
            '- **Open loops:** confirm the return leg\n',
            encoding="utf-8",
        )
        rel = self._conv_path(life_os)
        resp = client.request("DELETE", f"/api/life-os/file?path={rel}")
        assert resp.status_code == 200, resp.text
        text = self._index_md_path(life_os).read_text(encoding="utf-8")
        assert 'file="2026-06-01-1917-trial.md"' not in text
        assert "a codex trial" not in text
        # The sibling entry must survive untouched.
        assert 'file="2026-08-01-0900-ferry-booking.md"' in text
        assert "booking the ferry" in text

    def test_delete_survives_missing_index_md(self, life_os_client):
        client, _, overrides = life_os_client
        life_os = overrides["life_os_dir"]
        assert not self._index_md_path(life_os).exists()
        rel = self._conv_path(life_os)
        resp = client.request("DELETE", f"/api/life-os/file?path={rel}")
        assert resp.status_code == 200, resp.text

    def test_rename_triggers_search_resync(self, life_os_client, monkeypatch):
        client, _, overrides = life_os_client
        life_os = overrides["life_os_dir"]
        life_os_index = self._stub_search_cli(monkeypatch)
        calls = []
        monkeypatch.setattr(
            life_os_index.subprocess, "run",
            lambda argv, **kwargs: calls.append(argv) or _completed(),
        )
        rel = self._conv_path(life_os)
        resp = client.post(
            "/api/life-os/file/rename",
            json={"path": rel, "slug": "Use Personal Journal"},
        )
        assert resp.status_code == 200, resp.text
        assert len(calls) == 1
        argv = calls[0]
        assert argv[:2] == ["py", "conversation_search.py"]
        assert "--rebuild" in argv
        assert str(life_os) in argv

    def test_delete_triggers_search_resync(self, life_os_client, monkeypatch):
        client, _, overrides = life_os_client
        life_os = overrides["life_os_dir"]
        life_os_index = self._stub_search_cli(monkeypatch)
        calls = []
        monkeypatch.setattr(
            life_os_index.subprocess, "run",
            lambda argv, **kwargs: calls.append(argv) or _completed(),
        )
        rel = self._conv_path(life_os)
        resp = client.request("DELETE", f"/api/life-os/file?path={rel}")
        assert resp.status_code == 200, resp.text
        assert len(calls) == 1
        assert "--rebuild" in calls[0]

    def test_rename_survives_search_resync_nonzero_exit(self, life_os_client, monkeypatch):
        # A resync that runs but fails (e.g. life_os_dir not opted into
        # fleet-config's capture pipeline) must not fail the rename either —
        # only logged, per search_conversations's own returncode convention.
        client, _, overrides = life_os_client
        life_os = overrides["life_os_dir"]
        life_os_index = self._stub_search_cli(monkeypatch)
        monkeypatch.setattr(
            life_os_index.subprocess, "run",
            lambda *a, **k: _completed("", 1, "project not found"),
        )
        rel = self._conv_path(life_os)
        resp = client.post(
            "/api/life-os/file/rename",
            json={"path": rel, "slug": "Use Personal Journal"},
        )
        assert resp.status_code == 200, resp.text

    def test_rename_survives_search_resync_failure(self, life_os_client, monkeypatch):
        # A locked/corrupt search db, or a slow machine, must not fail the
        # rename that triggered the resync — the launcher doesn't own that
        # pipeline's health.
        client, _, overrides = life_os_client
        life_os = overrides["life_os_dir"]
        life_os_index = self._stub_search_cli(monkeypatch)
        def raising_run(*a, **k):
            raise subprocess.TimeoutExpired(cmd="conversation_search.py", timeout=30)
        monkeypatch.setattr(life_os_index.subprocess, "run", raising_run)
        rel = self._conv_path(life_os)
        resp = client.post(
            "/api/life-os/file/rename",
            json={"path": rel, "slug": "Use Personal Journal"},
        )
        assert resp.status_code == 200, resp.text

    def test_rename_survives_no_fleet_config(self, life_os_client):
        # life_os_client's fleet_config_dir has no hooks/ script, so this
        # exercises the "no checkout at all" path explicitly.
        client, _, overrides = life_os_client
        life_os = overrides["life_os_dir"]
        rel = self._conv_path(life_os)
        resp = client.post(
            "/api/life-os/file/rename",
            json={"path": rel, "slug": "Use Personal Journal"},
        )
        assert resp.status_code == 200, resp.text
