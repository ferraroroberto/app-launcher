"""/api/apps surface — list, scan, save, rename, delete."""

from __future__ import annotations

import json
from datetime import datetime

from src.registry import AppEntry, Registry, save_registry


def _seed_registry(tmp_registry_path, apps):
    """Helper: persist a synthetic registry to the tmp path the conftest
    pointed DEFAULT_REGISTRY_PATH at, so subsequent ``load_registry()``
    calls inside route handlers pick it up."""
    save_registry(
        Registry(scan_root="C:\\stub", apps=apps), path=tmp_registry_path
    )


class TestGetApps:
    def test_empty_registry_returns_empty_list(self, webapp_client):
        client, _, _ = webapp_client
        resp = client.get("/api/apps")
        assert resp.status_code == 200
        body = resp.json()
        assert body["apps"] == []

    def test_lists_seeded_entries(self, webapp_client):
        """Bat rows come from the registry; claude-code rows are scanned
        live from projects_dir — both surface in /api/apps. Claude-code
        rows carry the bare on-disk folder name (issue #45)."""
        client, _, overrides = webapp_client
        _seed_registry(
            overrides["tmp_registry_path"],
            [
                AppEntry(
                    id="alpha",
                    name="Alpha",
                    kind="streamlit",
                    bat_path="C:\\stub\\alpha.bat",
                    added_at=datetime.now().isoformat(),
                ),
            ],
        )
        (overrides["tmp_projects_dir"] / "beta").mkdir()
        resp = client.get("/api/apps")
        assert resp.status_code == 200
        names = {a["name"] for a in resp.json()["apps"]}
        # "beta" stays "beta" — no prettification for claude-code rows.
        assert names == {"Alpha", "beta"}


class TestClaudeCodeDiscovery:
    """The Claude Code tab lists projects_dir's child directories live —
    no scan step, no persistence in apps.json (issue #44)."""

    def test_child_dirs_appear_as_claude_code_rows(self, webapp_client):
        client, _, overrides = webapp_client
        for name in ("proj-one", "proj-two"):
            (overrides["tmp_projects_dir"] / name).mkdir()
        apps = client.get("/api/apps").json()["apps"]
        cc = [a for a in apps if a["kind"] == "claude-code"]
        assert {a["project_dir"] for a in cc} == {
            str(overrides["tmp_projects_dir"] / "proj-one"),
            str(overrides["tmp_projects_dir"] / "proj-two"),
        }

    def test_stale_claude_code_rows_in_registry_are_ignored(self, webapp_client):
        """An older apps.json may still carry claude-code rows — the API
        must not surface them; only the live directory scan counts."""
        client, _, overrides = webapp_client
        _seed_registry(
            overrides["tmp_registry_path"],
            [
                AppEntry(
                    id="ghost",
                    name="Ghost",
                    kind="claude-code",
                    project_dir="C:\\nowhere\\ghost",
                    added_at=datetime.now().isoformat(),
                ),
            ],
        )
        apps = client.get("/api/apps").json()["apps"]
        assert all(a["name"] != "Ghost" for a in apps)

    def test_projects_ignore_hides_matching_dirs(self, webapp_client):
        client, _, overrides = webapp_client
        for name in ("keep-me", "archive", "scratch-old"):
            (overrides["tmp_projects_dir"] / name).mkdir()
        client.post(
            "/api/config", json={"projects_ignore": ["archive", "*-old"]}
        )
        apps = client.get("/api/apps").json()["apps"]
        cc_names = {a["name"] for a in apps if a["kind"] == "claude-code"}
        assert cc_names == {"keep-me"}

    def test_vcs_and_build_dirs_always_skipped(self, webapp_client):
        client, _, overrides = webapp_client
        for name in (".git", "node_modules", "__pycache__", "real-project"):
            (overrides["tmp_projects_dir"] / name).mkdir()
        apps = client.get("/api/apps").json()["apps"]
        cc_names = {a["name"] for a in apps if a["kind"] == "claude-code"}
        assert cc_names == {"real-project"}

    def test_launch_resolves_live_claude_code_dir(
        self, webapp_client, monkeypatch
    ):
        """A claude-code row isn't in the registry — launch must resolve
        it against the live projects_dir scan, by its slugified id."""
        client, _, overrides = webapp_client
        from app.webapp.routers import apps as apps_router

        (overrides["tmp_projects_dir"] / "live-proj").mkdir()
        captured: dict = {}

        def fake_spawn(project_dir, name, flags, port, kind="pty", agent="claude"):
            captured["project_dir"] = str(project_dir)
            captured["kind"] = kind
            captured["agent"] = agent
            return {"session_id": "s1", "kind": kind, "agent": agent}

        monkeypatch.setattr(apps_router, "spawn_claude_session", fake_spawn)
        # slugify("live-proj") == "live-proj"; remote mode avoids the
        # PC mirror window.
        resp = client.post(
            "/api/apps/live-proj/launch", json={"mode": "remote"}
        )
        assert resp.status_code == 200
        assert captured["project_dir"] == str(
            overrides["tmp_projects_dir"] / "live-proj"
        )
        assert captured["kind"] == "remote"
        # No `agent` in the body → defaults to Claude Code.
        assert captured["agent"] == "claude"

    def test_launch_pty_forwards_phone_dimensions(
        self, webapp_client, monkeypatch
    ):
        """A streamed (pty) launch threads the phone's rows/cols through to
        spawn_claude_session so the PTY spawns at the right size (issue
        #126). Remote launches carry no PTY, so they stay on the default."""
        client, _, overrides = webapp_client
        from app.webapp.routers import apps as apps_router

        (overrides["tmp_projects_dir"] / "live-proj").mkdir()
        # No PC mirror window in the test.
        monkeypatch.setattr(
            apps_router, "open_local_terminal_window", lambda *a, **k: None
        )
        captured: dict = {}

        def fake_spawn(
            project_dir, name, flags, port, kind="pty", agent="claude",
            rows=40, cols=120,
        ):
            captured["kind"] = kind
            captured["rows"] = rows
            captured["cols"] = cols
            return {"session_id": "s1", "kind": kind, "agent": agent}

        monkeypatch.setattr(apps_router, "spawn_claude_session", fake_spawn)
        resp = client.post(
            "/api/apps/live-proj/launch", json={"rows": 50, "cols": 42}
        )
        assert resp.status_code == 200
        assert captured["kind"] == "pty"
        assert captured["rows"] == 50
        assert captured["cols"] == 42

    def test_launch_with_antigravity_agent(self, webapp_client, monkeypatch):
        """The Antigravity button posts agent=antigravity — it must be
        threaded to spawn_claude_session with empty (non-Claude) flags."""
        client, _, overrides = webapp_client
        from app.webapp.routers import apps as apps_router

        (overrides["tmp_projects_dir"] / "live-proj").mkdir()
        # Pretend the `agy` CLI is installed so the launch isn't rejected.
        monkeypatch.setattr(
            apps_router.agents, "is_installed", lambda agent_id: True
        )
        captured: dict = {}

        def fake_spawn(project_dir, name, flags, port, kind="pty", agent="claude"):
            captured["flags"] = flags
            captured["agent"] = agent
            return {"session_id": "s1", "kind": kind, "agent": agent}

        monkeypatch.setattr(apps_router, "spawn_claude_session", fake_spawn)
        resp = client.post(
            "/api/apps/live-proj/launch",
            json={"mode": "remote", "agent": "antigravity"},
        )
        assert resp.status_code == 200
        assert captured["agent"] == "antigravity"
        # All-default config → Antigravity launches bare.
        assert captured["flags"] == ""
        assert resp.json()["agent"] == "antigravity"

    def test_launch_antigravity_honours_config_toggles(
        self, webapp_client, monkeypatch
    ):
        """Antigravity launch flags come from the persisted Coding-options
        toggles — enabling sandbox surfaces --sandbox on the spawn."""
        client, _, overrides = webapp_client
        from app.webapp.routers import apps as apps_router

        (overrides["tmp_projects_dir"] / "live-proj").mkdir()
        monkeypatch.setattr(
            apps_router.agents, "is_installed", lambda agent_id: True
        )
        client.post("/api/config", json={"antigravity_sandbox": True})
        captured: dict = {}

        def fake_spawn(project_dir, name, flags, port, kind="pty", agent="claude"):
            captured["flags"] = flags
            return {"session_id": "s1", "kind": kind, "agent": agent}

        monkeypatch.setattr(apps_router, "spawn_claude_session", fake_spawn)
        resp = client.post(
            "/api/apps/live-proj/launch",
            json={"mode": "remote", "agent": "antigravity"},
        )
        assert resp.status_code == 200
        assert "--sandbox" in captured["flags"]

    def test_launch_with_copilot_agent(self, webapp_client, monkeypatch):
        """The GitHub Copilot button posts agent=copilot — threaded to
        spawn_claude_session, and its --allow-all toggle composes in."""
        client, _, overrides = webapp_client
        from app.webapp.routers import apps as apps_router

        (overrides["tmp_projects_dir"] / "live-proj").mkdir()
        # Pretend the `copilot` CLI is installed so the launch isn't rejected.
        monkeypatch.setattr(
            apps_router.agents, "is_installed", lambda agent_id: True
        )
        captured: dict = {}

        def fake_spawn(project_dir, name, flags, port, kind="pty", agent="claude"):
            captured["flags"] = flags
            captured["agent"] = agent
            return {"session_id": "s1", "kind": kind, "agent": agent}

        monkeypatch.setattr(apps_router, "spawn_claude_session", fake_spawn)
        # All-default config → Copilot launches bare.
        resp = client.post(
            "/api/apps/live-proj/launch",
            json={"mode": "remote", "agent": "copilot"},
        )
        assert resp.status_code == 200
        assert captured["agent"] == "copilot"
        assert captured["flags"] == ""
        assert resp.json()["agent"] == "copilot"
        # Enabling the toggle surfaces --allow-all on the next launch.
        client.post("/api/config", json={"copilot_skip_permissions": True})
        client.post(
            "/api/apps/live-proj/launch",
            json={"mode": "remote", "agent": "copilot"},
        )
        assert captured["flags"] == "--allow-all"

    def test_launch_with_codex_agent(self, webapp_client, monkeypatch):
        """The Codex button posts agent=codex — threaded to
        spawn_claude_session with the composed reasoning + permission
        flags, and the skip toggle swaps in the all-bypass switch."""
        client, _, overrides = webapp_client
        from app.webapp.routers import apps as apps_router

        (overrides["tmp_projects_dir"] / "live-proj").mkdir()
        # Pretend the `codex` CLI is installed so the launch isn't rejected.
        monkeypatch.setattr(
            apps_router.agents, "is_installed", lambda agent_id: True
        )
        captured: dict = {}

        def fake_spawn(project_dir, name, flags, port, kind="pty", agent="claude"):
            captured["flags"] = flags
            captured["agent"] = agent
            return {"session_id": "s1", "kind": kind, "agent": agent}

        monkeypatch.setattr(apps_router, "spawn_claude_session", fake_spawn)
        # All-default config → high reasoning + sandboxed auto.
        resp = client.post(
            "/api/apps/live-proj/launch",
            json={"mode": "remote", "agent": "codex"},
        )
        assert resp.status_code == 200
        assert captured["agent"] == "codex"
        assert "--ask-for-approval never --sandbox workspace-write" in captured["flags"]
        assert "model_reasoning_effort=high" in captured["flags"]
        assert resp.json()["agent"] == "codex"
        # Skip permissions swaps the sandboxed-auto pair for the bypass.
        client.post("/api/config", json={"codex_permission_mode": "skip"})
        client.post(
            "/api/apps/live-proj/launch",
            json={"mode": "remote", "agent": "codex"},
        )
        assert "--dangerously-bypass-approvals-and-sandbox" in captured["flags"]
        assert "--ask-for-approval" not in captured["flags"]

    def test_launch_resume_forces_pty_with_token(
        self, webapp_client, monkeypatch
    ):
        """Resume (issue #151) reopens the agent's picker: it forces a
        streamed pty even when mode=remote is also sent, and the spawn's
        flags begin with the agent's resume token (`--resume` for Claude)."""
        client, _, overrides = webapp_client
        from app.webapp.routers import apps as apps_router

        (overrides["tmp_projects_dir"] / "live-proj").mkdir()
        # Resume forces pty, which would otherwise open the PC mirror window.
        monkeypatch.setattr(
            apps_router, "open_local_terminal_window", lambda *a, **k: None
        )
        captured: dict = {}

        def fake_spawn(
            project_dir, name, flags, port, kind="pty", agent="claude",
            rows=40, cols=120,
        ):
            captured["flags"] = flags
            captured["kind"] = kind
            captured["agent"] = agent
            return {"session_id": "s1", "kind": kind, "agent": agent}

        monkeypatch.setattr(apps_router, "spawn_claude_session", fake_spawn)
        # mode=remote is ignored when resume is set — Resume wins.
        resp = client.post(
            "/api/apps/live-proj/launch",
            json={"mode": "remote", "resume": True},
        )
        assert resp.status_code == 200
        assert captured["kind"] == "pty"
        assert captured["agent"] == "claude"
        assert captured["flags"].startswith("--resume ")
        assert resp.json()["mode"] == "pty"

    def test_launch_resume_codex_drops_incompatible_flags(
        self, webapp_client, monkeypatch
    ):
        """`codex resume` rejects --ask-for-approval/--sandbox, so a Codex
        resume carries only the subcommand + the reasoning config override."""
        client, _, overrides = webapp_client
        from app.webapp.routers import apps as apps_router

        (overrides["tmp_projects_dir"] / "live-proj").mkdir()
        monkeypatch.setattr(
            apps_router.agents, "is_installed", lambda agent_id: True
        )
        monkeypatch.setattr(
            apps_router, "open_local_terminal_window", lambda *a, **k: None
        )
        captured: dict = {}

        def fake_spawn(
            project_dir, name, flags, port, kind="pty", agent="claude",
            rows=40, cols=120,
        ):
            captured["flags"] = flags
            captured["kind"] = kind
            return {"session_id": "s1", "kind": kind, "agent": agent}

        monkeypatch.setattr(apps_router, "spawn_claude_session", fake_spawn)
        resp = client.post(
            "/api/apps/live-proj/launch",
            json={"resume": True, "agent": "codex"},
        )
        assert resp.status_code == 200
        assert captured["kind"] == "pty"
        assert captured["flags"] == "resume -c model_reasoning_effort=high"
        assert "--ask-for-approval" not in captured["flags"]
        assert "--sandbox" not in captured["flags"]

    def test_launch_resume_antigravity_continues_most_recent(
        self, webapp_client, monkeypatch
    ):
        """agy has no picker flag, so its Resume maps to --continue
        (reopen the most recent conversation)."""
        client, _, overrides = webapp_client
        from app.webapp.routers import apps as apps_router

        (overrides["tmp_projects_dir"] / "live-proj").mkdir()
        monkeypatch.setattr(
            apps_router.agents, "is_installed", lambda agent_id: True
        )
        monkeypatch.setattr(
            apps_router, "open_local_terminal_window", lambda *a, **k: None
        )
        captured: dict = {}

        def fake_spawn(
            project_dir, name, flags, port, kind="pty", agent="claude",
            rows=40, cols=120,
        ):
            captured["flags"] = flags
            return {"session_id": "s1", "kind": kind, "agent": agent}

        monkeypatch.setattr(apps_router, "spawn_claude_session", fake_spawn)
        resp = client.post(
            "/api/apps/live-proj/launch",
            json={"resume": True, "agent": "antigravity"},
        )
        assert resp.status_code == 200
        assert captured["flags"].startswith("--continue")

    def test_launch_codex_not_installed_rejected(
        self, webapp_client, monkeypatch
    ):
        """When `codex` isn't on PATH the launch is refused —
        defence-in-depth behind the disabled UI button."""
        client, _, overrides = webapp_client
        from app.webapp.routers import apps as apps_router

        (overrides["tmp_projects_dir"] / "live-proj").mkdir()
        monkeypatch.setattr(
            apps_router.agents, "is_installed", lambda agent_id: False
        )
        resp = client.post(
            "/api/apps/live-proj/launch", json={"agent": "codex"}
        )
        assert resp.status_code == 400
        assert "not installed" in resp.json()["detail"]

    def test_launch_copilot_not_installed_rejected(
        self, webapp_client, monkeypatch
    ):
        """When `copilot` isn't on PATH the launch is refused —
        defence-in-depth behind the disabled UI button."""
        client, _, overrides = webapp_client
        from app.webapp.routers import apps as apps_router

        (overrides["tmp_projects_dir"] / "live-proj").mkdir()
        monkeypatch.setattr(
            apps_router.agents, "is_installed", lambda agent_id: False
        )
        resp = client.post(
            "/api/apps/live-proj/launch", json={"agent": "copilot"}
        )
        assert resp.status_code == 400
        assert "not installed" in resp.json()["detail"]

    def test_launch_unknown_agent_rejected(self, webapp_client):
        client, _, overrides = webapp_client
        (overrides["tmp_projects_dir"] / "live-proj").mkdir()
        resp = client.post(
            "/api/apps/live-proj/launch", json={"agent": "bogus"}
        )
        assert resp.status_code == 400

    def test_launch_antigravity_not_installed_rejected(
        self, webapp_client, monkeypatch
    ):
        """When `agy` isn't on PATH the Antigravity launch is refused —
        defence-in-depth behind the disabled UI button."""
        client, _, overrides = webapp_client
        from app.webapp.routers import apps as apps_router

        (overrides["tmp_projects_dir"] / "live-proj").mkdir()
        monkeypatch.setattr(
            apps_router.agents, "is_installed", lambda agent_id: False
        )
        resp = client.post(
            "/api/apps/live-proj/launch", json={"agent": "antigravity"}
        )
        assert resp.status_code == 400
        assert "not installed" in resp.json()["detail"]


class TestScanApps:
    def test_returns_new_key_with_list(self, webapp_client, monkeypatch):
        client, _, _ = webapp_client
        # discover_new is imported at module level into routers/apps.py
        from app.webapp.routers import apps as apps_router
        monkeypatch.setattr(apps_router, "discover_new", lambda **_: [])
        resp = client.post("/api/apps/scan")
        assert resp.status_code == 200
        assert resp.json() == {"new": []}


class TestSaveApps:
    def test_400_on_empty_ids(self, webapp_client):
        client, _, _ = webapp_client
        resp = client.post("/api/apps/save", json={"ids": []})
        assert resp.status_code == 400

    def test_persists_selected_ids(self, webapp_client, monkeypatch):
        client, _, _ = webapp_client
        from app.webapp.routers import apps as apps_router
        candidate = AppEntry(
            id="freshapp",
            name="Fresh App",
            kind="streamlit",
            bat_path="C:\\stub\\fresh.bat",
            added_at=datetime.now().isoformat(),
        )
        monkeypatch.setattr(apps_router, "discover_new", lambda **_: [candidate])
        resp = client.post("/api/apps/save", json={"ids": ["freshapp"]})
        assert resp.status_code == 200
        added = resp.json()["added"]
        assert len(added) == 1
        assert added[0]["id"] == "freshapp"


class TestRenameApp:
    def test_404_on_unknown_id(self, webapp_client):
        client, _, _ = webapp_client
        resp = client.patch("/api/apps/nope", json={"name": "Whatever"})
        assert resp.status_code == 404

    def test_400_on_empty_name(self, webapp_client, overrides=None):
        client, _, overrides = webapp_client
        _seed_registry(
            overrides["tmp_registry_path"],
            [
                AppEntry(
                    id="alpha",
                    name="Alpha",
                    kind="streamlit",
                    bat_path="C:\\stub\\alpha.bat",
                    added_at=datetime.now().isoformat(),
                ),
            ],
        )
        resp = client.patch("/api/apps/alpha", json={"name": "   "})
        assert resp.status_code == 400

    def test_round_trips(self, webapp_client):
        client, _, overrides = webapp_client
        _seed_registry(
            overrides["tmp_registry_path"],
            [
                AppEntry(
                    id="alpha",
                    name="Alpha",
                    kind="streamlit",
                    bat_path="C:\\stub\\alpha.bat",
                    added_at=datetime.now().isoformat(),
                ),
            ],
        )
        resp = client.patch("/api/apps/alpha", json={"name": "Renamed"})
        assert resp.status_code == 200
        assert resp.json()["app"]["name"] == "Renamed"


class TestLaunchAppTracksSpawn:
    """Non-claude-code launches must register the spawn with app_runtime
    so the Running apps panel can list + stop them (issue #35)."""

    def test_launch_bat_records_spawn(self, webapp_client, monkeypatch):
        client, _, overrides = webapp_client
        from app.webapp.routers import apps as apps_router

        _seed_registry(
            overrides["tmp_registry_path"],
            [
                AppEntry(
                    id="alpha",
                    name="Alpha",
                    kind="streamlit",
                    bat_path="C:\\stub\\alpha.bat",
                    added_at=datetime.now().isoformat(),
                ),
            ],
        )
        monkeypatch.setattr(apps_router, "spawn_bat", lambda _path: 54321)
        recorded: list[tuple] = []
        monkeypatch.setattr(
            apps_router.app_runtime,
            "record_spawn",
            lambda *a: recorded.append(a),
        )

        resp = client.post("/api/apps/alpha/launch")
        assert resp.status_code == 200
        assert recorded == [("alpha", "Alpha", "streamlit", 54321)]


class TestDeleteApp:
    def test_404_on_unknown_id(self, webapp_client):
        client, _, _ = webapp_client
        resp = client.delete("/api/apps/nope")
        assert resp.status_code == 404

    def test_removes_existing_entry(self, webapp_client):
        client, _, overrides = webapp_client
        _seed_registry(
            overrides["tmp_registry_path"],
            [
                AppEntry(
                    id="alpha",
                    name="Alpha",
                    kind="streamlit",
                    bat_path="C:\\stub\\alpha.bat",
                    added_at=datetime.now().isoformat(),
                ),
            ],
        )
        resp = client.delete("/api/apps/alpha")
        assert resp.status_code == 200
        assert resp.json()["removed"] == "alpha"
        # And it's gone from /api/apps.
        listing = client.get("/api/apps").json()
        assert all(a["id"] != "alpha" for a in listing["apps"])
