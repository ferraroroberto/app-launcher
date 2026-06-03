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
