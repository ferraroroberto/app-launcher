"""src.scanner.git_status — branch + clean/dirty flags (issue #115).

Unlike github_repo_url (a plain .git/config read), git_status shells out
to real git, so these build real repos in tmp_path and skip cleanly where
git isn't on PATH.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from src.scanner import GitStatus, git_status

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git not on PATH"
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo(repo: Path, default: str = "main") -> None:
    """A repo with one commit on a known default branch."""
    repo.mkdir(parents=True, exist_ok=True)
    # -b is git >= 2.28; pin the default branch name so the test is
    # independent of the host's init.defaultBranch.
    _git(repo, "init", "-b", default)
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("hi\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    # --no-verify: the dev box has a global pre-commit hook (author-email
    # allowlist) inherited via core.hooksPath; the throwaway test repo
    # must not be subject to it.
    _git(repo, "commit", "--no-verify", "-m", "init")


def test_clean_repo_on_default_branch(tmp_path: Path):
    _init_repo(tmp_path, default="main")
    gs = git_status(tmp_path)
    assert gs.is_git is True
    assert gs.branch == "main"
    assert gs.default_branch == "main"
    assert gs.on_default_branch is True
    assert gs.dirty is False


def test_dirty_repo_is_flagged(tmp_path: Path):
    _init_repo(tmp_path, default="main")
    # An untracked file makes the tree dirty (porcelain reports "? ...").
    (tmp_path / "scratch.txt").write_text("wip\n", encoding="utf-8")
    gs = git_status(tmp_path)
    assert gs.is_git is True
    assert gs.dirty is True
    assert gs.on_default_branch is True  # still on main, just dirty


def test_off_default_branch_is_flagged(tmp_path: Path):
    _init_repo(tmp_path, default="main")
    _git(tmp_path, "checkout", "-b", "feature/x")
    gs = git_status(tmp_path)
    assert gs.is_git is True
    assert gs.branch == "feature/x"
    assert gs.default_branch == "main"  # main still exists as a local branch
    assert gs.on_default_branch is False
    assert gs.dirty is False


def test_master_default_branch_resolves(tmp_path: Path):
    _init_repo(tmp_path, default="master")
    _git(tmp_path, "checkout", "-b", "wip")
    gs = git_status(tmp_path)
    assert gs.default_branch == "master"
    assert gs.on_default_branch is False


def test_non_git_folder(tmp_path: Path):
    gs = git_status(tmp_path)
    assert gs == GitStatus(
        is_git=False, branch=None, default_branch=None, dirty=False
    )
    assert gs.on_default_branch is True  # ambiguous → never yellow


def test_on_default_branch_property_when_default_unknown():
    # A repo on a branch but with no resolvable default must not read as
    # "off main" — that would paint it yellow on ambiguous data.
    gs = GitStatus(is_git=True, branch="dev", default_branch=None, dirty=False)
    assert gs.on_default_branch is True


def test_route_fan_out_leaves_the_default_thread_pool_free(webapp_client, monkeypatch):
    """#1264: the route fanned one git_status per repo onto the loop's
    default executor, so while it ran every other ``asyncio.to_thread``
    caller (``/api/version``, sessions, config writes) queued behind it.
    With 64 repos at 0.3 s each in flight, a trivial ``to_thread`` call on
    the same loop must still come back promptly."""
    import asyncio
    import time

    import httpx

    from app.webapp.routers import claude_code
    from src.scanner import ProjectDir

    _, app, _ = webapp_client
    repos = [ProjectDir(id=f"p{i}", name=f"p{i}", project_dir=f"C:/fake/p{i}") for i in range(64)]
    monkeypatch.setattr(claude_code, "scan_project_dirs", lambda *a, **k: repos)

    def slow_status(_path):
        time.sleep(0.3)
        return GitStatus(is_git=False, branch=None, default_branch=None, dirty=False)

    monkeypatch.setattr(claude_code, "git_status", slow_status)

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            fan_out = asyncio.create_task(client.get("/api/claude-code/git-status"))
            await asyncio.sleep(0.1)
            started = time.perf_counter()
            await asyncio.to_thread(lambda: None)
            waited = time.perf_counter() - started
            return waited, (await fan_out).json()["projects"]

    waited, projects = asyncio.run(run())
    assert len(projects) == 64
    assert waited < 0.2, (
        f"a to_thread call waited {waited:.2f}s behind the git-status fan-out: "
        "it is occupying the shared default thread pool"
    )
