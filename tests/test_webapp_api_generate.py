"""/api/claude-code/generate — workspace ↔ remote.bat sync surface."""

from __future__ import annotations

from src.bat_generator import GenerateResult


class TestGetGeneratePreview:
    def test_returns_workspaces_and_orphans_keys(self, webapp_client):
        """Tmp projects_dir is empty → both lists empty. Shape still
        present so the SPA can render the empty-state without crashing."""
        client, _, _ = webapp_client
        resp = client.get("/api/claude-code/generate")
        assert resp.status_code == 200
        body = resp.json()
        assert "projects_dir" in body
        assert body["workspaces"] == []
        assert body["orphans"] == []


class TestPostGenerate:
    def test_happy_path_returns_result_shape(self, webapp_client, monkeypatch):
        client, _, _ = webapp_client
        from app.webapp.routers import claude_code as claude_code_router
        # run_generate is the real worker — mock it so we don't write bats
        # to the tmp projects_dir.
        canned = GenerateResult(
            created=["foo-remote.bat"],
            overwritten=[],
            ws_created=[],
            errors=[],
        )
        monkeypatch.setattr(claude_code_router, "run_generate", lambda **_: canned)
        resp = client.post(
            "/api/claude-code/generate",
            json={"overwrite": [], "create_ws": []},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["created"] == ["foo-remote.bat"]
        assert body["overwritten"] == []
        assert body["ws_created"] == []
        assert body["errors"] == []

    def test_passes_selections_through(self, webapp_client, monkeypatch):
        """The endpoint just forwards the user's overwrite/create_ws
        selections to run_generate. Catch any future drift that drops
        them silently."""
        client, _, _ = webapp_client
        from app.webapp.routers import claude_code as claude_code_router
        captured = {}

        def fake_run(*, projects_dir, flags, overwrite_names, create_ws_names):
            captured["overwrite"] = overwrite_names
            captured["create_ws"] = create_ws_names
            return GenerateResult()

        monkeypatch.setattr(claude_code_router, "run_generate", fake_run)
        resp = client.post(
            "/api/claude-code/generate",
            json={"overwrite": ["a", "b"], "create_ws": ["c"]},
        )
        assert resp.status_code == 200
        assert captured["overwrite"] == {"a", "b"}
        assert captured["create_ws"] == {"c"}
