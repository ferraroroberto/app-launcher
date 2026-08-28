"""``src/git_utils.py`` — the shared ``git`` subprocess runner (issue #794).

Exercises :func:`run_git` and :func:`resolve_default_ref` directly; the
consuming modules (``build_info``, ``scanner``, ``session_host_paths``)
keep their own tests for the higher-level behaviour built on top.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from src import git_utils


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    no_hooks = tmp_path / "no-hooks"
    no_hooks.mkdir()
    run = lambda *args: subprocess.run(  # noqa: E731
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    )
    run("init", "-q", "-b", "main")
    run("config", "core.hooksPath", str(no_hooks))
    run("config", "user.email", "test@example.com")
    run("config", "user.name", "Test")
    (repo / "f.txt").write_text("v1", encoding="utf-8")
    run("add", ".")
    run("commit", "-q", "-m", "base")
    return repo


class TestRunGit:
    def test_returns_stripped_stdout_on_success(self, tmp_path):
        repo = _init_repo(tmp_path)
        out = git_utils.run_git(repo, ["rev-parse", "--short", "HEAD"])
        assert isinstance(out, str) and out
        assert "\n" not in out

    def test_returns_none_for_non_repo_dir(self, tmp_path):
        assert git_utils.run_git(tmp_path, ["rev-parse", "--short", "HEAD"]) is None

    def test_returns_none_when_git_raises(self, monkeypatch, tmp_path):
        def _raise(*_args, **_kwargs):
            raise OSError("git not found")

        monkeypatch.setattr(subprocess, "run", _raise)
        assert git_utils.run_git(tmp_path, ["rev-parse", "--short", "HEAD"]) is None

    def test_warn_on_failure_logs_at_warning(self, tmp_path, caplog):
        import logging

        with caplog.at_level(logging.WARNING, logger="src.git_utils"):
            git_utils.run_git(tmp_path, ["rev-parse", "--short", "HEAD"], warn_on_failure=True)
        assert any("run_git" in r.message for r in caplog.records)

    def test_default_quiet_failure_does_not_warn(self, tmp_path, caplog):
        import logging

        with caplog.at_level(logging.WARNING, logger="src.git_utils"):
            git_utils.run_git(tmp_path, ["rev-parse", "--short", "HEAD"])
        assert not any(r.levelno >= logging.WARNING for r in caplog.records)


class TestResolveDefaultRef:
    def test_resolves_origin_head_when_set(self, tmp_path):
        repo = _init_repo(tmp_path)
        remote = tmp_path / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True, capture_output=True)
        subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "push", "-q", "origin", "main"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "remote", "set-head", "origin", "main"], cwd=repo, check=True, capture_output=True)

        ref = git_utils.resolve_default_ref(repo, fallback_refs=("origin/main", "origin/master"))
        assert ref == "origin/main"

    def test_falls_back_to_local_branch_when_no_origin(self, tmp_path):
        repo = _init_repo(tmp_path)
        ref = git_utils.resolve_default_ref(repo, fallback_refs=("refs/heads/main", "refs/heads/master"))
        assert ref == "refs/heads/main"

    def test_returns_none_when_nothing_resolves(self, tmp_path):
        assert git_utils.resolve_default_ref(tmp_path, fallback_refs=("origin/main",)) is None
