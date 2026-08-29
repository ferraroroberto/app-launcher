"""POST /api/claude-code/vscode/{id} + the /api/agents vscode_available flag (#802).

Covers the Coding-row VS Code button's backend: the sibling
``<name>.code-workspace`` file is created on first open with the shape the
existing ones already have, an existing file is never rewritten, the ``code``
CLI is spawned exactly once per tap, and an absent CLI or unknown project is
refused with a distinct status rather than a generic failure. The button's
placement and disabled state are pinned separately by the e2e suite
(``tests/e2e/test_coding_vscode_button.py``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.webapp.routers import claude_code as cc_router
from src import vscode_workspace


def _make_project(app, name: str) -> Path:
    projects_dir = Path(app.state.webapp_config.projects_dir)
    (projects_dir / name).mkdir()
    return projects_dir


@pytest.fixture
def fake_code(monkeypatch):
    """Pretend the ``code`` CLI is installed, and record every spawn.

    Patched on the *router's* module-level names, not on
    ``src.vscode_workspace`` — the router imports the functions directly, the
    same shape ``tests/conftest.py`` already patches ``open_local_terminal_window``
    per router for. Without this a unit run on a machine that really has VS
    Code would pop a real editor window.
    """
    spawned: list[Path] = []
    monkeypatch.setattr(cc_router, "is_vscode_installed", lambda: True)
    monkeypatch.setattr(
        cc_router,
        "open_workspace",
        lambda path: (spawned.append(Path(path)), 4321)[1],
    )
    return spawned


class TestOpenInVscodeEndpoint:
    def test_creates_workspace_then_spawns_code(self, webapp_client, fake_code):
        client, app, _ = webapp_client
        projects_dir = _make_project(app, "alphaproj")
        workspace = projects_dir / "alphaproj.code-workspace"
        assert not workspace.exists()

        resp = client.post("/api/claude-code/vscode/alphaproj")

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["created"] is True
        assert Path(body["workspace"]) == workspace
        assert body["pid"] == 4321
        # The file lands beside the project folder, not inside it, and names
        # the folder relatively — the shape every workspace on disk uses.
        assert json.loads(workspace.read_text(encoding="utf-8")) == {
            "folders": [{"path": "alphaproj"}]
        }
        assert fake_code == [workspace]

    def test_existing_workspace_is_opened_not_rewritten(self, webapp_client, fake_code):
        """The user's own folder list / settings / extension recommendations
        live in this file — a second tap must not flatten them."""
        client, app, _ = webapp_client
        projects_dir = _make_project(app, "betaproj")
        workspace = projects_dir / "betaproj.code-workspace"
        custom = '{\n\t"folders": [{"path": "betaproj"}],\n\t"settings": {"a": 1}\n}\n'
        workspace.write_text(custom, encoding="utf-8")

        resp = client.post("/api/claude-code/vscode/betaproj")

        assert resp.status_code == 200
        assert resp.json()["created"] is False
        assert workspace.read_text(encoding="utf-8") == custom
        assert fake_code == [workspace]

    def test_unknown_project_is_404(self, webapp_client, fake_code):
        client, _, _ = webapp_client
        resp = client.post("/api/claude-code/vscode/not-on-disk")
        assert resp.status_code == 404
        assert fake_code == []

    def test_missing_cli_is_503_and_creates_nothing(self, webapp_client, monkeypatch):
        """A stale greyed-out button (or a direct POST) must not leave a
        workspace file behind for an editor that can't be launched."""
        client, app, _ = webapp_client
        projects_dir = _make_project(app, "gammaproj")
        monkeypatch.setattr(cc_router, "is_vscode_installed", lambda: False)

        resp = client.post("/api/claude-code/vscode/gammaproj")

        assert resp.status_code == 503
        assert "code" in resp.json()["detail"]
        assert not (projects_dir / "gammaproj.code-workspace").exists()

    def test_spawn_failure_is_500_not_a_crash(self, webapp_client, monkeypatch):
        client, app, _ = webapp_client
        _make_project(app, "deltaproj")
        monkeypatch.setattr(cc_router, "is_vscode_installed", lambda: True)

        def _boom(path):
            raise OSError("CreateProcess failed")

        monkeypatch.setattr(cc_router, "open_workspace", _boom)
        resp = client.post("/api/claude-code/vscode/deltaproj")
        assert resp.status_code == 500
        assert "CreateProcess failed" in resp.json()["detail"]


class TestAgentsPayload:
    def test_agents_endpoint_carries_vscode_available(self, webapp_client):
        client, _, _ = webapp_client
        body = client.get("/api/agents").json()
        # The SPA reads the field by type, so it must be a real bool — not a
        # truthy path string or a missing key.
        assert isinstance(body["vscode_available"], bool)


class TestWorkspaceHelpers:
    def test_workspace_path_is_a_sibling_of_the_project(self, tmp_path):
        assert vscode_workspace.workspace_path_for(tmp_path, "proj") == (
            tmp_path / "proj.code-workspace"
        )

    def test_generated_file_matches_the_on_disk_convention(self, tmp_path):
        """Tab-indented, trailing newline — byte-identical to the workspace
        files VS Code itself has already written under projects_dir."""
        path, created = vscode_workspace.ensure_workspace_file(tmp_path, "proj")
        assert created is True
        assert path.read_text(encoding="utf-8") == (
            '{\n\t"folders": [\n\t\t{\n\t\t\t"path": "proj"\n\t\t}\n\t]\n}\n'
        )

    def test_ensure_is_idempotent(self, tmp_path):
        vscode_workspace.ensure_workspace_file(tmp_path, "proj")
        path, created = vscode_workspace.ensure_workspace_file(tmp_path, "proj")
        assert created is False
        assert path.exists()
