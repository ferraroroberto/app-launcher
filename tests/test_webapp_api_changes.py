"""The Coding row menu's three project routes (#977):

    GET  /api/claude-code/changes/{id}        working-tree file list
    GET  /api/claude-code/changes/{id}/diff   one file's unified diff
    POST /api/claude-code/folder/{id}         open in Explorer

Status contract: unknown project 404, non-git folder 409, hostile diff path
400, no Explorer 503. The git parsing itself is pinned in
``tests/test_git_changes.py``; here the repos are real but tiny so the
routes are exercised end to end through the FastAPI app.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.webapp.routers import claude_code as cc_router


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _make_project(app, name: str, *, git: bool = True) -> Path:
    project = Path(app.state.webapp_config.projects_dir) / name
    project.mkdir()
    if git:
        _git(project, "init", "-q", "-b", "main")
        _git(project, "config", "user.email", "t@example.com")
        _git(project, "config", "user.name", "t")
        _git(project, "config", "core.autocrlf", "false")
        _git(project, "config", "core.hooksPath", str(project.parent / "no-hooks"))
        (project / "a.txt").write_text("one\n", encoding="utf-8")
        _git(project, "add", "a.txt")
        _git(project, "commit", "-q", "-m", "init")
    return project


class TestChangesList:
    def test_lists_files_with_counts(self, webapp_client):
        client, app, _ = webapp_client
        project = _make_project(app, "alpha")
        (project / "a.txt").write_text("one\ntwo\n", encoding="utf-8")
        (project / "new.txt").write_text("n\n", encoding="utf-8")

        resp = client.get("/api/claude-code/changes/alpha")

        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == "alpha" and body["name"] == "alpha"
        assert body["branch"] == "main"
        assert [f["path"] for f in body["files"]] == ["a.txt", "new.txt"]
        assert body["files"][0]["status"] == "M"
        assert body["files"][1]["status"] == "U"
        assert body["counts"] == {"files": 2, "additions": 2, "deletions": 0}

    def test_clean_tree_is_an_empty_list_not_an_error(self, webapp_client):
        client, app, _ = webapp_client
        _make_project(app, "beta")
        body = client.get("/api/claude-code/changes/beta").json()
        assert body["files"] == [] and body["counts"]["files"] == 0

    def test_unknown_project_is_404(self, webapp_client):
        client, _, _ = webapp_client
        assert client.get("/api/claude-code/changes/nope").status_code == 404

    def test_non_git_folder_is_409(self, webapp_client):
        client, app, _ = webapp_client
        _make_project(app, "plain", git=False)
        resp = client.get("/api/claude-code/changes/plain")
        assert resp.status_code == 409
        assert "git" in resp.json()["detail"]


class TestFileDiff:
    def test_returns_the_unified_diff(self, webapp_client):
        client, app, _ = webapp_client
        project = _make_project(app, "gamma")
        (project / "a.txt").write_text("one\nTWO\n", encoding="utf-8")

        body = client.get("/api/claude-code/changes/gamma/diff", params={"path": "a.txt"}).json()

        assert body["path"] == "a.txt"
        assert "+TWO" in body["diff"]
        assert body["binary"] is False and body["truncated"] is False

    @pytest.mark.parametrize("bad", ["../secret", "/etc/passwd", "C:\\x", "", "a/../.."])
    def test_hostile_path_is_400(self, webapp_client, bad):
        client, app, _ = webapp_client
        _make_project(app, "delta")
        resp = client.get("/api/claude-code/changes/delta/diff", params={"path": bad})
        assert resp.status_code == 400, bad

    def test_non_git_folder_is_409(self, webapp_client):
        client, app, _ = webapp_client
        _make_project(app, "plain2", git=False)
        resp = client.get("/api/claude-code/changes/plain2/diff", params={"path": "x"})
        assert resp.status_code == 409


class TestOpenFolder:
    def test_spawns_explorer_once(self, webapp_client, monkeypatch):
        client, app, _ = webapp_client
        project = _make_project(app, "eps", git=False)
        opened: list = []
        monkeypatch.setattr(cc_router, "open_folder", lambda p: (opened.append(Path(p)), 99)[1])

        resp = client.post("/api/claude-code/folder/eps")

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True and body["pid"] == 99
        assert Path(body["path"]) == project
        assert opened == [project]

    def test_unknown_project_is_404(self, webapp_client, monkeypatch):
        client, _, _ = webapp_client
        monkeypatch.setattr(cc_router, "open_folder", lambda p: 1)
        assert client.post("/api/claude-code/folder/nope").status_code == 404

    def test_no_explorer_is_503_with_the_reason(self, webapp_client, monkeypatch):
        client, app, _ = webapp_client
        _make_project(app, "zeta", git=False)

        def _boom(p):
            raise OSError("open folder is Windows-only")

        monkeypatch.setattr(cc_router, "open_folder", _boom)
        resp = client.post("/api/claude-code/folder/zeta")
        assert resp.status_code == 503
        assert "Windows-only" in resp.json()["detail"]
