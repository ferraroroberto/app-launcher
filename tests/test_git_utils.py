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


class TestGitEnvOptionalLocks:
    """``GIT_OPTIONAL_LOCKS=0`` — the fix for the stranded 0-byte
    ``.git/index.lock`` that froze 23 repos at one timestamp (#1111,
    ferraroroberto/fleet-config#939).

    Both halves are asserted, because the dangerous regression is not losing
    the variable — it is "cleaning it up" the other way and suppressing a
    *real* write lock, which would turn a refusal into silent index
    corruption. The variable must stop the optional lock and leave the
    mandatory one exactly as it was.
    """

    def test_git_env_sets_the_variable_over_os_environ(self):
        env = git_utils.git_env({"EXISTING": "kept"})
        assert env["GIT_OPTIONAL_LOCKS"] == "0"
        assert env["EXISTING"] == "kept", "git_env must extend the base env, not replace it"

    def test_run_git_does_not_take_the_optional_index_lock(self, tmp_path):
        """`git status` under the wrapper never creates `.git/index.lock`.

        Watched from a thread rather than checked afterwards: the lock is
        taken and released *within* the run (measured at ~4-6 ms, ~75% of the
        way through), so a check after the call returns would pass even
        against the unfixed code. Only observing during the run can tell the
        two apart -- and this test does fail against pre-fix `git_utils.py`.
        """
        import threading

        repo = _init_repo(tmp_path)
        (repo / "dirty.txt").write_text("untracked", encoding="utf-8")
        lock = repo / ".git" / "index.lock"

        seen: list[bool] = []
        stop = threading.Event()

        def watch() -> None:
            while not stop.is_set():
                if lock.exists():
                    seen.append(True)
                    return

        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()
        try:
            out = git_utils.run_git(repo, ["status", "--porcelain=v2", "--branch"])
        finally:
            stop.set()
            watcher.join(timeout=5)

        assert out is not None, "the status call itself must still work"
        assert not seen, "run_git took the optional index lock -- GIT_OPTIONAL_LOCKS=0 is not reaching git"
        assert not lock.exists()

    def test_a_real_write_still_refuses_over_a_held_lock(self, tmp_path):
        """The property that must NOT regress: only *optional* locks are
        suppressed. A genuine index write still takes the real lock and still
        fails when one is already held."""
        repo = _init_repo(tmp_path)
        (repo / "new.txt").write_text("hi", encoding="utf-8")
        (repo / ".git" / "index.lock").write_text("", encoding="utf-8")

        result = subprocess.run(
            ["git", "-C", str(repo), "add", "new.txt"],
            capture_output=True, text=True, env=git_utils.git_env(),
        )
        assert result.returncode != 0
        assert "index.lock" in (result.stderr or "")
