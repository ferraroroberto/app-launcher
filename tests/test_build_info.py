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
