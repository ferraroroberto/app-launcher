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
        captured: dict = {}

        def fake_spawn(
            project_dir, name, flags, port, kind="pty", agent="claude",
            rows=40, cols=120, history_lines=None,
        ):
            captured["kind"] = kind
            captured["rows"] = rows
            captured["cols"] = cols
            captured["history_lines"] = history_lines
            return {"session_id": "s1", "kind": kind, "agent": agent}

        monkeypatch.setattr(apps_router, "spawn_claude_session", fake_spawn)
        resp = client.post(
            "/api/apps/live-proj/launch", json={"rows": 50, "cols": 42}
        )
        assert resp.status_code == 200
        assert captured["kind"] == "pty"
        assert captured["rows"] == 50
        assert captured["cols"] == 42
        # The Settings-tab scrollback-depth config (issue #435 follow-up)
        # rides every PTY launch — the default conftest config's value,
        # not a hardcoded/omitted one.
        assert captured["history_lines"] == 10_000

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

    def test_launch_with_grok_agent(self, webapp_client, monkeypatch):
        """The Grok button posts agent=grok — threaded to
        spawn_claude_session with the composed permission + reasoning
        flags (#626 launched it bare; #667 gave it knobs)."""
        client, _, overrides = webapp_client
        from app.webapp.routers import apps as apps_router

        (overrides["tmp_projects_dir"] / "live-proj").mkdir()
        # Pretend the `grok` CLI is installed so the launch isn't rejected.
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
            json={"mode": "remote", "agent": "grok"},
        )
        assert resp.status_code == 200
        assert captured["agent"] == "grok"
        # Default config → auto permission + high reasoning (#667).
        assert captured["flags"] == "--permission-mode auto --reasoning-effort high"
        assert resp.json()["agent"] == "grok"
        # Resume prepends grok's bare --resume (most-recent session, #626)
        # and keeps the knobs — build_resume_flags routes through the same
        # builder, so a resumed launch is configured identically.
        client.post(
            "/api/apps/live-proj/launch",
            json={"mode": "remote", "agent": "grok", "resume": True},
        )
        assert captured["flags"].startswith("--resume")
        assert "--permission-mode auto" in captured["flags"]
        assert "--reasoning-effort high" in captured["flags"]

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
        assert "--model gpt-5.6-luna" in captured["flags"]
        assert "model_reasoning_effort=xhigh" in captured["flags"]
        assert resp.json()["agent"] == "codex"
        # Skip permissions swaps the sandboxed-auto pair for the bypass.
        client.post("/api/config", json={"codex_permission_mode": "skip"})
        client.post(
            "/api/apps/live-proj/launch",
            json={"mode": "remote", "agent": "codex"},
        )
        assert "--dangerously-bypass-approvals-and-sandbox" in captured["flags"]
        assert "--ask-for-approval" not in captured["flags"]

    def test_launch_resume_only_streams_pty_with_token(
        self, webapp_client, monkeypatch
    ):
        """Resume without Detached (issue #151) reopens the agent's picker in
        a streamed pty, and the spawn's flags begin with the agent's resume
        token (`--resume` for Claude)."""
        client, _, overrides = webapp_client
        from app.webapp.routers import apps as apps_router

        (overrides["tmp_projects_dir"] / "live-proj").mkdir()
        captured: dict = {}

        def fake_spawn(
            project_dir, name, flags, port, kind="pty", agent="claude",
            rows=40, cols=120, history_lines=None,
        ):
            captured["flags"] = flags
            captured["kind"] = kind
            captured["agent"] = agent
            return {"session_id": "s1", "kind": kind, "agent": agent}

        monkeypatch.setattr(apps_router, "spawn_claude_session", fake_spawn)
        resp = client.post(
            "/api/apps/live-proj/launch", json={"resume": True}
        )
        assert resp.status_code == 200
        assert captured["kind"] == "pty"
        assert captured["agent"] == "claude"
        assert captured["flags"].startswith("--resume ")
        assert resp.json()["mode"] == "pty"

    def test_launch_resume_with_detached_opens_remote_console(
        self, webapp_client, monkeypatch
    ):
        """Detached + Resume (issue #157) honours mode=remote: the picker
        renders in a detached console window rather than forcing a pty. The
        spawn is kind="remote" with the agent's resume token in its flags."""
        client, _, overrides = webapp_client
        from app.webapp.routers import apps as apps_router

        (overrides["tmp_projects_dir"] / "live-proj").mkdir()
        captured: dict = {}

        def fake_spawn(
            project_dir, name, flags, port, kind="pty", agent="claude",
            rows=40, cols=120, history_lines=None,
        ):
            captured["flags"] = flags
            captured["kind"] = kind
            captured["agent"] = agent
            return {"session_id": "s1", "kind": kind, "agent": agent}

        monkeypatch.setattr(apps_router, "spawn_claude_session", fake_spawn)
        resp = client.post(
            "/api/apps/live-proj/launch",
            json={"mode": "remote", "resume": True},
        )
        assert resp.status_code == 200
        assert captured["kind"] == "remote"
        assert captured["agent"] == "claude"
        assert captured["flags"].startswith("--resume ")
        assert resp.json()["mode"] == "remote"

    def test_launch_resume_codex_drops_incompatible_flags(
        self, webapp_client, monkeypatch
    ):
        """`codex resume` rejects --ask-for-approval/--sandbox, so a Codex
        resume carries the subcommand, exact model, and reasoning override."""
        client, _, overrides = webapp_client
        from app.webapp.routers import apps as apps_router

        (overrides["tmp_projects_dir"] / "live-proj").mkdir()
        monkeypatch.setattr(
            apps_router.agents, "is_installed", lambda agent_id: True
        )
        captured: dict = {}

        def fake_spawn(
            project_dir, name, flags, port, kind="pty", agent="claude",
            rows=40, cols=120, history_lines=None,
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
        assert captured["flags"] == (
            "resume --model gpt-5.6-luna -c model_reasoning_effort=xhigh"
        )
        assert "--ask-for-approval" not in captured["flags"]
        assert "--sandbox" not in captured["flags"]

    def test_launch_codex_honours_explicit_matching_model(
        self, webapp_client, monkeypatch
    ):
        client, _, overrides = webapp_client
        from app.webapp.routers import apps as apps_router

        (overrides["tmp_projects_dir"] / "live-proj").mkdir()
        monkeypatch.setattr(apps_router.agents, "is_installed", lambda _: True)
        captured: dict = {}

        def fake_spawn(
            project_dir, name, flags, port, kind="pty", agent="claude",
            rows=40, cols=120, history_lines=None,
        ):
            captured["flags"] = flags
            return {"session_id": "s1", "kind": kind, "agent": agent}

        monkeypatch.setattr(apps_router, "spawn_claude_session", fake_spawn)
        resp = client.post(
            "/api/apps/live-proj/launch",
            json={"agent": "codex", "model": "codex:gpt-5.6-terra"},
        )
        assert resp.status_code == 200
        assert "--model gpt-5.6-terra" in captured["flags"]

        bad = client.post(
            "/api/apps/live-proj/launch",
            json={"agent": "claude", "model": "codex:gpt-5.6-terra"},
        )
        assert bad.status_code == 400

    def test_pty_launch_forwards_the_config_derived_spawn_arguments(
        self, webapp_client, monkeypatch
    ):
        """#1003 — the two ``cfg``-derived spawn arguments reach the spawn.

        ``spawn_launcher_session`` now owns ``cfg.session_host_port`` and
        ``history_lines=cfg.terminal_history_lines`` on behalf of all five
        launch call sites. Nothing asserted either one before: several tests
        *capture* ``history_lines`` and none checks it, so dropping it (or
        passing the wrong port) left the suite green. This pins both, and it
        passes against the pre-helper code too — which is the point, since
        the extraction is meant to change nothing.
        """
        client, app, overrides = webapp_client
        from app.webapp.routers import apps as apps_router

        (overrides["tmp_projects_dir"] / "live-proj").mkdir()
        monkeypatch.setattr(apps_router.agents, "is_installed", lambda _: True)
        cfg = app.state.webapp_config
        cfg.terminal_history_lines = 7321
        captured: dict = {}

        def fake_spawn(
            project_dir, name, flags, port, kind="pty", agent="claude",
            rows=40, cols=120, history_lines=None, label="",
        ):
            captured.update(
                port=port, kind=kind, history_lines=history_lines,
                rows=rows, cols=cols, label=label,
            )
            return {"session_id": "s1", "kind": kind, "agent": agent}

        monkeypatch.setattr(apps_router, "spawn_claude_session", fake_spawn)
        resp = client.post(
            "/api/apps/live-proj/launch",
            json={"agent": "claude", "mode": "pty", "rows": 51, "cols": 99},
        )
        assert resp.status_code == 200, resp.text
        assert captured["kind"] == "pty"
        assert captured["port"] == cfg.session_host_port
        assert captured["history_lines"] == 7321
        assert (captured["rows"], captured["cols"]) == (51, 99)
        # A normal session carries no role label; only the chief does.
        assert captured["label"] == ""

    def test_launch_rejects_out_of_catalog_model(self, webapp_client, monkeypatch):
        """#1007 — a model whose provider prefix matches but whose value is
        not in the catalog is a 400, not a silent fallback.

        ``build_claude_flags`` emits no ``--model`` at all for a value it does
        not recognise *and* discards the persisted model (because
        ``model_override is not None``), so this used to return 200 and launch
        the session on the CLI's own default — exactly what a phone holding a
        pre-rename cached bundle would send. The Life OS (#540) and Board
        (#505) launch routes already 400 here.
        """
        client, _, overrides = webapp_client
        from app.webapp.routers import apps as apps_router

        (overrides["tmp_projects_dir"] / "live-proj").mkdir()
        monkeypatch.setattr(apps_router.agents, "is_installed", lambda _: True)
        captured: dict = {}

        def fake_spawn(
            project_dir, name, flags, port, kind="pty", agent="claude",
            rows=40, cols=120, history_lines=None,
        ):
            captured["flags"] = flags
            return {"session_id": "s1", "kind": kind, "agent": agent}

        monkeypatch.setattr(apps_router, "spawn_claude_session", fake_spawn)

        resp = client.post(
            "/api/apps/live-proj/launch",
            json={"agent": "claude", "model": "claude:retired-alias"},
        )
        assert resp.status_code == 400, resp.text
        assert "retired-alias" in resp.json()["detail"]
        # The launch was refused outright, not run on a fallback model.
        assert "flags" not in captured, captured

        # Codex is guarded by the same catalog.
        bad_codex = client.post(
            "/api/apps/live-proj/launch",
            json={"agent": "codex", "model": "codex:gpt-4-legacy"},
        )
        assert bad_codex.status_code == 400, bad_codex.text

        # A real catalog value still launches, with its --model intact.
        ok = client.post(
            "/api/apps/live-proj/launch",
            json={"agent": "claude", "model": "claude:sonnet"},
        )
        assert ok.status_code == 200, ok.text
        assert "--model sonnet" in captured["flags"]

    def test_launch_rejects_a_model_the_agent_cannot_apply(
        self, webapp_client, monkeypatch
    ):
        """#1044 — a model for an agent with no per-launch model flag is a
        400, not an accepted-and-dropped 200.

        Antigravity, Copilot, Pi and Grok never receive a per-launch
        ``--model``: three send none at all and Pi's comes from the persisted
        ``pi_model`` alone. The route used to validate only the provider
        *prefix*, so ``copilot:<anything>`` passed, fell through to the
        cfg-only builder and launched a session on whatever the CLI picked —
        a 200 indistinguishable from the caller getting what it asked for.

        Which agents those are is read off the registry
        (``Agent.per_launch_model``), so a seventh agent inherits the guard
        without anyone remembering to extend a list here.
        """
        client, _, overrides = webapp_client
        from app.webapp.routers import apps as apps_router
        from src import agents as agents_mod

        (overrides["tmp_projects_dir"] / "live-proj").mkdir()
        monkeypatch.setattr(apps_router.agents, "is_installed", lambda _: True)
        captured: dict = {}

        def fake_spawn(
            project_dir, name, flags, port, kind="pty", agent="claude",
            rows=40, cols=120, history_lines=None,
        ):
            captured["flags"] = flags
            return {"session_id": "s1", "kind": kind, "agent": agent}

        monkeypatch.setattr(apps_router, "spawn_claude_session", fake_spawn)

        cannot_apply = sorted(
            a for a, spec in agents_mod.AGENTS.items()
            if not spec.per_launch_model
        )
        assert cannot_apply, "registry says every agent takes a per-launch model"

        for agent in cannot_apply:
            captured.clear()
            resp = client.post(
                "/api/apps/live-proj/launch",
                json={"agent": agent, "model": f"{agent}:some-model"},
            )
            assert resp.status_code == 400, f"{agent}: {resp.text}"
            detail = resp.json()["detail"]
            assert "some-model" in detail, f"{agent}: {detail}"
            # Refused outright — never launched on a model nobody asked for.
            assert "flags" not in captured, f"{agent}: {captured}"

            # Resume takes the same path, and used to drop it just as quietly.
            captured.clear()
            resumed = client.post(
                "/api/apps/live-proj/launch",
                json={
                    "agent": agent, "resume": True,
                    "model": f"{agent}:some-model",
                },
            )
            assert resumed.status_code == 400, f"{agent} resume: {resumed.text}"
            assert "flags" not in captured, f"{agent} resume: {captured}"

            # Control: the same launch without a model is untouched. No
            # first-party bundle has ever sent one for these agents, so this
            # is the only shape the phone actually posts.
            captured.clear()
            ok = client.post(
                "/api/apps/live-proj/launch", json={"agent": agent},
            )
            assert ok.status_code == 200, f"{agent}: {ok.text}"
            assert "flags" in captured, f"{agent}: launch did not spawn"

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
        captured: dict = {}

        def fake_spawn(
            project_dir, name, flags, port, kind="pty", agent="claude",
            rows=40, cols=120, history_lines=None,
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


class TestLaunchSpawnErrorMapping:
    """The spawn failure → HTTP mapping shared by every launch route.

    Issue #689 lifted this out of six copy-pasted call sites into
    ``_helpers.spawn_session_or_400``; nothing pinned it before, so a drift
    in the surviving copy would have been silent. The session-host's own
    status must survive (a 409 "already running" is not a 400), and a
    plain OSError from the spawn must land as 400.
    """

    def test_session_host_error_status_is_passed_through(
        self, webapp_client, monkeypatch
    ):
        client, _, overrides = webapp_client
        from src.session_client import SessionHostError
        from app.webapp.routers import apps as apps_router

        (overrides["tmp_projects_dir"] / "live-proj").mkdir()

        def boom(*_a, **_kw):
            raise SessionHostError("session already running", status=409)

        monkeypatch.setattr(apps_router, "spawn_claude_session", boom)
        resp = client.post("/api/apps/live-proj/launch", json={})
        assert resp.status_code == 409
        assert resp.json()["detail"] == "session already running"

    def test_os_error_maps_to_400(self, webapp_client, monkeypatch):
        client, _, overrides = webapp_client
        from app.webapp.routers import apps as apps_router

        (overrides["tmp_projects_dir"] / "live-proj").mkdir()

        def boom(*_a, **_kw):
            raise OSError("project dir vanished")

        monkeypatch.setattr(apps_router, "spawn_claude_session", boom)
        # Remote mode takes the other call site in the same route.
        resp = client.post(
            "/api/apps/live-proj/launch", json={"mode": "remote"}
        )
        assert resp.status_code == 400
        assert resp.json()["detail"] == "project dir vanished"


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


class TestPatchAppAutostart:
    """Registered Trays autostart toggle (issue #456 part 2/2) — an
    independent field on the same PATCH endpoint, postable with no
    ``name`` in the body at all."""

    def _seed_tray(self, overrides):
        _seed_registry(
            overrides["tmp_registry_path"],
            [
                AppEntry(
                    id="tray-app",
                    name="Tray App",
                    kind="tray",
                    bat_path="C:\\stub\\tray.bat",
                    added_at=datetime.now().isoformat(),
                ),
            ],
        )

    def test_defaults_to_false(self, webapp_client, overrides=None):
        client, _, overrides = webapp_client
        self._seed_tray(overrides)
        resp = client.get("/api/apps")
        app = next(a for a in resp.json()["apps"] if a["id"] == "tray-app")
        assert app["autostart"] is False

    def test_enable_round_trips(self, webapp_client, overrides=None):
        client, _, overrides = webapp_client
        self._seed_tray(overrides)
        resp = client.patch("/api/apps/tray-app", json={"autostart": True})
        assert resp.status_code == 200
        assert resp.json()["app"]["autostart"] is True
        # Survives a fresh GET, not just the response body.
        apps = client.get("/api/apps").json()["apps"]
        assert next(a for a in apps if a["id"] == "tray-app")["autostart"] is True

    def test_disable_round_trips(self, webapp_client, overrides=None):
        client, _, overrides = webapp_client
        self._seed_tray(overrides)
        client.patch("/api/apps/tray-app", json={"autostart": True})
        resp = client.patch("/api/apps/tray-app", json={"autostart": False})
        assert resp.status_code == 200
        assert resp.json()["app"]["autostart"] is False

    def test_404_on_unknown_id(self, webapp_client):
        client, _, _ = webapp_client
        resp = client.patch("/api/apps/nope", json={"autostart": True})
        assert resp.status_code == 404

    def test_400_when_body_has_neither_field(self, webapp_client, overrides=None):
        client, _, overrides = webapp_client
        self._seed_tray(overrides)
        resp = client.patch("/api/apps/tray-app", json={})
        assert resp.status_code == 400

    def test_autostart_does_not_require_name(self, webapp_client, overrides=None):
        """The Registered Trays toggle never sends `name` — must not 400
        the way a rename-only PATCH with a blank name does."""
        client, _, overrides = webapp_client
        self._seed_tray(overrides)
        resp = client.patch("/api/apps/tray-app", json={"autostart": True})
        assert resp.status_code == 200


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
        monkeypatch.setattr(
            apps_router, "spawn_bat", lambda _path, stealth=False: 54321
        )
        recorded: list[tuple] = []
        monkeypatch.setattr(
            apps_router.app_runtime,
            "record_spawn",
            lambda *a: recorded.append(a),
        )

        resp = client.post("/api/apps/alpha/launch")
        assert resp.status_code == 200
        assert recorded == [("alpha", "Alpha", "streamlit", 54321)]

    def test_launch_tray_does_not_record_spawn(self, webapp_client, monkeypatch):
        """A tray.bat's real long-lived process detaches from spawn_bat's
        PID via tray_lifecycle.ps1's Start-Process (issue #456 follow-up) —
        tracking it in Running apps would offer a port that can never
        resolve and a Stop button that can't reach the real process.
        Port listeners already resolves it correctly once it's up."""
        client, _, overrides = webapp_client
        from app.webapp.routers import apps as apps_router

        _seed_registry(
            overrides["tmp_registry_path"],
            [
                AppEntry(
                    id="home-automation-tray",
                    name="Home Automation",
                    kind="tray",
                    bat_path="C:\\stub\\home-automation\\tray.bat",
                    added_at=datetime.now().isoformat(),
                ),
            ],
        )
        monkeypatch.setattr(
            apps_router, "spawn_bat", lambda _path, stealth=False: 54321
        )
        recorded: list[tuple] = []
        monkeypatch.setattr(
            apps_router.app_runtime,
            "record_spawn",
            lambda *a: recorded.append(a),
        )

        resp = client.post("/api/apps/home-automation-tray/launch")
        assert resp.status_code == 200
        assert recorded == []


class TestLaunchBatStealthMode:
    """The Apps/Trays ⚡ vs 🚫👁 pair (issue #790).

    The route's job is to turn an optional body flag into ``spawn_bat``'s
    ``stealth=`` argument. The default matters as much as the opt-in: a
    phone still serving an older cached PWA bundle posts no body at all,
    and must keep launching *visibly* rather than silently going invisible.
    """

    def _seed_and_capture(self, overrides, monkeypatch):
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
        calls: list[bool] = []

        def _fake_spawn(_path, stealth=False):
            calls.append(stealth)
            return 54321

        monkeypatch.setattr(apps_router, "spawn_bat", _fake_spawn)
        monkeypatch.setattr(
            apps_router.app_runtime, "record_spawn", lambda *a: None
        )
        return calls

    def test_bodyless_post_launches_visibly(self, webapp_client, monkeypatch):
        """An older cached client sends no body — it must not go stealth."""
        client, _, overrides = webapp_client
        calls = self._seed_and_capture(overrides, monkeypatch)
        resp = client.post("/api/apps/alpha/launch")
        assert resp.status_code == 200
        assert calls == [False]
        assert resp.json()["stealth"] is False

    def test_explicit_false_launches_visibly(self, webapp_client, monkeypatch):
        client, _, overrides = webapp_client
        calls = self._seed_and_capture(overrides, monkeypatch)
        resp = client.post("/api/apps/alpha/launch", json={"stealth": False})
        assert resp.status_code == 200
        assert calls == [False]

    def test_stealth_true_is_forwarded(self, webapp_client, monkeypatch):
        client, _, overrides = webapp_client
        calls = self._seed_and_capture(overrides, monkeypatch)
        resp = client.post("/api/apps/alpha/launch", json={"stealth": True})
        assert resp.status_code == 200
        assert calls == [True]
        assert resp.json()["stealth"] is True


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
