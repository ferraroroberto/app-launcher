"""``src/build_info.py`` — issue #615's shared process-identity helper.

Exercises the failure-degrades-to-"unknown" contract directly (not just via
the webapp/session-host endpoints that consume it), since both of those
endpoints depend on this never raising.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from src import build_info


def test_resolve_git_sha_returns_short_sha_for_this_repo():
    sha = build_info.resolve_git_sha()
    assert isinstance(sha, str) and sha
    assert sha != "unknown"
    assert len(sha) <= 12  # short-sha, not a full 40-char hash


def test_resolve_git_sha_unknown_for_non_repo_dir(tmp_path):
    assert build_info.resolve_git_sha(tmp_path) == "unknown"


def test_resolve_git_sha_unknown_when_git_missing(monkeypatch, tmp_path):
    def _raise(*_args, **_kwargs):
        raise OSError("git not found")
    monkeypatch.setattr(subprocess, "run", _raise)
    assert build_info.resolve_git_sha(tmp_path) == "unknown"


def test_build_identity_shape():
    identity = build_info.build_identity()
    assert set(identity.keys()) == {"git_sha", "captured_at"}
    assert isinstance(identity["git_sha"], str) and identity["git_sha"]
    assert isinstance(identity["captured_at"], str) and identity["captured_at"]


def test_resolve_deployed_sha_returns_short_sha_for_this_repo(tmp_path):
    """#655: must not depend on the ambient CI checkout's ``origin`` remote
    state -- GitHub Actions' shallow PR checkout can leave both
    ``origin/HEAD`` and an ``origin/main`` tracking ref unresolvable, which
    is a property of that checkout, not a defect in this function. Exercise
    it against a throwaway repo we control instead (same fixture pattern as
    ``test_resolve_deployed_sha_differs_from_checkout_branch_tip`` below)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    no_hooks = tmp_path / "no-hooks"
    no_hooks.mkdir()

    def run(*args):
        return subprocess.run(
            ["git", *args], cwd=repo, check=True, capture_output=True, text=True
        )

    run("init", "-q", "-b", "main")
    run("config", "core.hooksPath", str(no_hooks))
    run("config", "user.email", "test@example.com")
    run("config", "user.name", "Test")
    (repo / "f.txt").write_text("v1", encoding="utf-8")
    run("add", ".")
    run("commit", "-q", "-m", "base")

    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True, capture_output=True)
    run("remote", "add", "origin", str(remote))
    run("push", "-q", "origin", "main")
    run("remote", "set-head", "origin", "main")

    sha = build_info.resolve_deployed_sha(repo)
    assert isinstance(sha, str) and sha
    assert sha != "unknown"
    assert len(sha) <= 12


def test_resolve_deployed_sha_unknown_for_non_repo_dir(tmp_path):
    assert build_info.resolve_deployed_sha(tmp_path) == "unknown"


def test_resolve_deployed_sha_unknown_when_git_missing(monkeypatch, tmp_path):
    def _raise(*_args, **_kwargs):
        raise OSError("git not found")
    monkeypatch.setattr(subprocess, "run", _raise)
    assert build_info.resolve_deployed_sha(tmp_path) == "unknown"


def test_resolve_deployed_sha_differs_from_checkout_branch_tip(tmp_path):
    """#641: on a feature branch that hasn't merged, the checkout's own
    branch tip (``resolve_git_sha``) and the resolved deployed ref
    (``resolve_deployed_sha``, ``origin/main``) must be free to diverge —
    the whole point of having two separate functions."""
    repo = tmp_path / "repo"
    repo.mkdir()
    no_hooks = tmp_path / "no-hooks"
    no_hooks.mkdir()

    def run(*args):
        return subprocess.run(
            ["git", *args], cwd=repo, check=True, capture_output=True, text=True
        )

    run("init", "-q", "-b", "main")
    run("config", "core.hooksPath", str(no_hooks))
    run("config", "user.email", "test@example.com")
    run("config", "user.name", "Test")
    (repo / "f.txt").write_text("v1", encoding="utf-8")
    run("add", ".")
    run("commit", "-q", "-m", "base")

    # A bare remote so origin/HEAD + origin/main resolve without a network.
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True, capture_output=True)
    run("remote", "add", "origin", str(remote))
    run("push", "-q", "origin", "main")
    run("remote", "set-head", "origin", "main")

    main_sha = build_info.resolve_deployed_sha(repo)

    run("checkout", "-q", "-b", "feature")
    (repo / "f.txt").write_text("v2", encoding="utf-8")
    run("add", ".")
    run("commit", "-q", "-m", "feature work, not pushed")
    branch_tip = build_info.resolve_git_sha(repo)
    deployed = build_info.resolve_deployed_sha(repo)

    assert branch_tip != main_sha
    assert deployed == main_sha
