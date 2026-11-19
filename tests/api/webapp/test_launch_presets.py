"""Launch presets: the config list, and a launch's per-launch ``effort`` and ``context``."""

from __future__ import annotations

import pytest

PRESET = {"name": "Opus deep", "agent": "claude", "model": "opus", "effort": "xhigh"}
CODEX_PRESET = {"name": "Luna wide", "agent": "codex", "model": "gpt-6-luna",
                "effort": "high", "context": "extended"}


@pytest.fixture
def spawn_args(monkeypatch):
    """Stands in for the Coding launch route's ``spawn_claude_session``."""
    from app.webapp.routers import apps as apps_router

    captured: dict = {}

    def fake_spawn(project_dir, name, flags, port, kind="pty", agent="claude",
                   rows=40, cols=120, history_lines=None):
        captured.update(flags=flags, kind=kind, agent=agent)
        return {"session_id": "s1", "kind": kind, "agent": agent}

    monkeypatch.setattr(apps_router, "spawn_claude_session", fake_spawn)
    monkeypatch.setattr(apps_router.agents, "is_installed", lambda _: True)
    return captured


@pytest.fixture
def project(webapp_client):
    client, _, overrides = webapp_client
    (overrides["tmp_projects_dir"] / "live-proj").mkdir()
    return client


class TestPresetConfig:
    def test_none_by_default(self, webapp_client):
        client, _, _ = webapp_client
        assert client.get("/api/config").json()["launch_presets"] == []

    def test_save_assigns_an_id_and_round_trips(self, webapp_client):
        client, app, _ = webapp_client
        resp = client.post("/api/config", json={"launch_presets": [PRESET]})
        assert resp.status_code == 200, resp.text
        saved = client.get("/api/config").json()["launch_presets"]
        assert len(saved) == 1
        assert saved[0]["id"] and saved[0]["name"] == "Opus deep"
        assert app.state.webapp_config.launch_presets == saved

    def test_rename_and_remove_keep_the_id(self, webapp_client):
        client, _, _ = webapp_client
        client.post("/api/config", json={"launch_presets": [PRESET]})
        saved = client.get("/api/config").json()["launch_presets"]
        renamed = [dict(saved[0], name="  Opus   max ")]
        assert client.post("/api/config", json={"launch_presets": renamed}).status_code == 200
        after = client.get("/api/config").json()["launch_presets"]
        assert after[0]["id"] == saved[0]["id"] and after[0]["name"] == "Opus max"
        client.post("/api/config", json={"launch_presets": []})
        assert client.get("/api/config").json()["launch_presets"] == []

    @pytest.mark.parametrize("bad, fragment", [
        (dict(PRESET, effort="turbo"), "not offered"),
        (dict(PRESET, model="retired-alias"), "unsupported"),
        (dict(PRESET, agent="pi"), "per-launch model"),
        (dict(PRESET, name="  "), "needs a name"),
        (dict(PRESET, name="x" * 41), "at most 40"),
    ])
    def test_refuses_what_the_catalog_does_not_offer(self, webapp_client, bad, fragment):
        client, app, _ = webapp_client
        resp = client.post("/api/config", json={"launch_presets": [bad]})
        assert resp.status_code == 400, resp.text
        assert fragment in resp.json()["detail"]
        assert app.state.webapp_config.launch_presets == []

    def test_refuses_a_duplicate_name_and_a_non_list(self, webapp_client):
        client, _, _ = webapp_client
        twin = dict(PRESET, name="OPUS DEEP")
        assert client.post("/api/config", json={"launch_presets": [PRESET, twin]}).status_code == 400
        assert client.post("/api/config", json={"launch_presets": "opus"}).status_code == 400

    def test_a_saved_preset_survives_a_model_leaving_the_catalog(self, webapp_client, monkeypatch):
        client, app, _ = webapp_client
        client.post("/api/config", json={"launch_presets": [PRESET]})
        from src import webapp_config as wc
        from src.launching import presets

        reloaded = wc.load_webapp_config(wc.save_webapp_config(app.state.webapp_config))
        assert reloaded.launch_presets == app.state.webapp_config.launch_presets
        assert presets.load_launch_presets({"launch_presets": [{"id": "a", "name": "n"}]}) == []


class TestPresetContext:
    def test_catalog_offers_a_tier_only_where_the_model_has_one(self, webapp_client):
        client, _, _ = webapp_client
        catalog = client.get("/api/config").json()["model_catalog"]
        assert all("contexts" not in model for model in catalog["claude"])
        assert all(model["contexts"][0]["value"] == "extended" for model in catalog["codex"])

    def test_a_context_round_trips(self, webapp_client):
        client, _, _ = webapp_client
        assert client.post("/api/config", json={"launch_presets": [CODEX_PRESET]}).status_code == 200
        saved = client.get("/api/config").json()["launch_presets"]
        assert saved[0]["context"] == "extended"

    def test_no_context_leaves_the_row_without_the_key(self, webapp_client):
        client, _, _ = webapp_client
        client.post("/api/config", json={"launch_presets": [dict(CODEX_PRESET, context="")]})
        assert "context" not in client.get("/api/config").json()["launch_presets"][0]

    @pytest.mark.parametrize("bad", [
        dict(PRESET, context="extended"),
        dict(CODEX_PRESET, context="huge"),
    ])
    def test_refuses_a_tier_the_model_does_not_offer(self, webapp_client, bad):
        client, app, _ = webapp_client
        resp = client.post("/api/config", json={"launch_presets": [bad]})
        assert resp.status_code == 400, resp.text
        assert "context" in resp.json()["detail"]
        assert app.state.webapp_config.launch_presets == []


class TestCodingLaunchContext:
    def test_the_tier_reaches_the_codex_flags(self, project, spawn_args):
        resp = project.post("/api/apps/live-proj/launch",
                            json={"agent": "codex", "model": "codex:gpt-6-luna",
                                  "effort": "high", "context": "extended"})
        assert resp.status_code == 200, resp.text
        assert "-c model_context_window=872000" in spawn_args["flags"]
        assert "model_reasoning_effort=high" in spawn_args["flags"]

    def test_resume_carries_the_tier(self, project, spawn_args):
        resp = project.post("/api/apps/live-proj/launch",
                            json={"agent": "codex", "model": "codex:gpt-6-luna",
                                  "context": "extended", "resume": True})
        assert resp.status_code == 200, resp.text
        assert "model_context_window=872000" in spawn_args["flags"]

    def test_default_launch_sends_no_window(self, project, spawn_args):
        resp = project.post("/api/apps/live-proj/launch",
                            json={"agent": "codex", "model": "codex:gpt-6-luna"})
        assert resp.status_code == 200, resp.text
        assert "model_context_window" not in spawn_args["flags"]

    @pytest.mark.parametrize("body", [
        {"agent": "claude", "model": "claude:opus", "context": "extended"},
        {"agent": "codex", "model": "codex:gpt-6-luna", "context": "huge"},
        {"agent": "codex", "context": "extended"},
        {"agent": "codex", "model": "codex:gpt-6-luna", "context": 3},
    ])
    def test_a_tier_the_model_cannot_take_is_a_400(self, project, spawn_args, body):
        resp = project.post("/api/apps/live-proj/launch", json=body)
        assert resp.status_code == 400, resp.text
        assert "flags" not in spawn_args


class TestCodingLaunchEffort:
    def test_effort_reaches_the_claude_flags_and_is_echoed(self, project, spawn_args):
        resp = project.post("/api/apps/live-proj/launch",
                            json={"agent": "claude", "model": "claude:opus", "effort": "max"})
        assert resp.status_code == 200, resp.text
        assert "--model opus" in spawn_args["flags"]
        assert "--effort max" in spawn_args["flags"]
        assert resp.json()["effort"] == "max"

    def test_effort_reaches_the_codex_flags(self, project, spawn_args):
        resp = project.post("/api/apps/live-proj/launch",
                            json={"agent": "codex", "model": "codex:gpt-6-luna", "effort": "low"})
        assert resp.status_code == 200, resp.text
        assert "model_reasoning_effort=low" in spawn_args["flags"]

    def test_resume_carries_the_effort(self, project, spawn_args):
        resp = project.post("/api/apps/live-proj/launch",
                            json={"agent": "claude", "model": "claude:sonnet",
                                  "effort": "low", "resume": True})
        assert resp.status_code == 200, resp.text
        assert spawn_args["flags"].startswith("--resume")
        assert "--effort low" in spawn_args["flags"]

    def test_default_launch_is_unchanged(self, project, spawn_args):
        resp = project.post("/api/apps/live-proj/launch",
                            json={"agent": "claude", "model": "claude:opus"})
        assert resp.status_code == 200, resp.text
        assert "effort" not in resp.json()

    @pytest.mark.parametrize("body", [
        {"agent": "claude", "model": "claude:opus", "effort": "ultra"},
        {"agent": "claude", "effort": "high"},
        {"agent": "claude", "model": "claude:opus", "effort": 3},
    ])
    def test_an_effort_the_model_cannot_take_is_a_400(self, project, spawn_args, body):
        resp = project.post("/api/apps/live-proj/launch", json=body)
        assert resp.status_code == 400, resp.text
        assert "flags" not in spawn_args


class TestLifeOsLaunchEffort:
    @pytest.fixture(autouse=True)
    def _loopback_client(self, monkeypatch):
        """The launch is passkey-gated; treat the TestClient host as loopback."""
        from src import middleware

        monkeypatch.setattr(
            middleware.trust, "LOOPBACK_HOSTS",
            frozenset({"testclient", "127.0.0.1", "::1", "localhost"}),
        )

    def _spawn(self, monkeypatch):
        from app.webapp.routers import life_os_spawn

        captured: dict = {}

        def fake_spawn(project_dir, name, flags, port, kind, agent,
                       rows=40, cols=120, history_lines=None):
            captured.update(flags=flags, kind=kind, agent=agent)
            return {"session_id": "r1", "kind": kind}

        monkeypatch.setattr(life_os_spawn, "spawn_claude_session", fake_spawn)
        return captured

    def test_recap_launch_applies_the_effort(self, life_os_client, monkeypatch):
        client, _, _ = life_os_client
        captured = self._spawn(monkeypatch)
        resp = client.post("/api/life-os/recap/launch",
                           json={"mode": "pty", "model": "claude:opus", "effort": "xhigh"})
        assert resp.status_code == 200, resp.text
        assert "--model opus" in captured["flags"]
        assert "--effort xhigh" in captured["flags"]
        assert resp.json()["effort"] == "xhigh"

    def test_codex_recap_launch_applies_the_effort(self, life_os_client, monkeypatch):
        client, _, _ = life_os_client
        captured = self._spawn(monkeypatch)
        resp = client.post("/api/life-os/recap/launch",
                           json={"mode": "pty", "model": "codex:gpt-6-astra", "effort": "low"})
        assert resp.status_code == 200, resp.text
        assert "model_reasoning_effort=low" in captured["flags"]

    def test_refuses_an_effort_the_model_does_not_offer(self, life_os_client, monkeypatch):
        client, _, _ = life_os_client
        captured = self._spawn(monkeypatch)
        resp = client.post("/api/life-os/recap/launch",
                           json={"mode": "pty", "model": "claude:opus", "effort": "ultra"})
        assert resp.status_code == 400, resp.text
        assert "flags" not in captured

    def test_without_an_effort_the_saved_one_stands(self, life_os_client, monkeypatch):
        client, _, _ = life_os_client
        captured = self._spawn(monkeypatch)
        resp = client.post("/api/life-os/recap/launch", json={"mode": "pty", "model": "claude:opus"})
        assert resp.status_code == 200, resp.text
        assert "effort" not in resp.json()
        assert "--model opus" in captured["flags"]

    def test_codex_recap_launch_applies_the_context_tier(self, life_os_client, monkeypatch):
        client, _, _ = life_os_client
        captured = self._spawn(monkeypatch)
        resp = client.post("/api/life-os/recap/launch",
                           json={"mode": "pty", "model": "codex:gpt-6-luna", "context": "extended"})
        assert resp.status_code == 200, resp.text
        assert "model_context_window=872000" in captured["flags"]

    def test_refuses_a_tier_on_a_claude_model(self, life_os_client, monkeypatch):
        client, _, _ = life_os_client
        captured = self._spawn(monkeypatch)
        resp = client.post("/api/life-os/recap/launch",
                           json={"mode": "pty", "model": "claude:opus", "context": "extended"})
        assert resp.status_code == 400, resp.text
        assert "flags" not in captured
