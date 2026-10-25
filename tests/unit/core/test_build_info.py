"""``src/build_info.py`` — issue #615's shared process-identity helper.

Exercises the failure-degrades-to-"unknown" contract directly (not just via
the webapp/session-host endpoints that consume it), since both of those
endpoints depend on this never raising.
"""

from __future__ import annotations

import subprocess

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
    assert set(identity.keys()) == {"git_sha", "built_at"}
    assert isinstance(identity["git_sha"], str) and identity["git_sha"]
    assert isinstance(identity["built_at"], str) and identity["built_at"]


# --- #825: a transient git failure must not freeze "unknown" for the process ---
#
# `build_identity()` is called exactly once per process, at import. The
# session-host is deliberately excluded from `tray.bat --restart` (to protect
# live PTYs), so a single unlucky `git` call at its start used to leave
# `git_sha: "unknown"` — and with it the whole staleness check — unavailable for
# that process's entire life. Observed: 8 days.


def test_build_identity_retries_a_transient_failure(monkeypatch):
    """One failure then success must yield the real sha, not "unknown"."""
    calls = []

    def _flaky(project_root=build_info.PROJECT_ROOT):
        calls.append(project_root)
        return "unknown" if len(calls) == 1 else "abc1234"

    monkeypatch.setattr(build_info, "resolve_git_sha", _flaky)
    monkeypatch.setattr(build_info._time, "sleep", lambda _s: None)

    identity = build_info.build_identity()

    assert identity["git_sha"] == "abc1234"
    assert len(calls) == 2, "should have retried exactly once past the failure"


def test_build_identity_gives_up_and_reports_unknown(monkeypatch):
    """A persistent failure still reports "unknown" — never a guess — and the
    retry is bounded rather than looping forever at process start."""
    calls = []
    monkeypatch.setattr(
        build_info, "resolve_git_sha",
        lambda project_root=build_info.PROJECT_ROOT: (calls.append(1), "unknown")[1],
    )
    monkeypatch.setattr(build_info._time, "sleep", lambda _s: None)

    identity = build_info.build_identity()

    assert identity["git_sha"] == "unknown"
    assert len(calls) == build_info.CAPTURE_ATTEMPTS


def test_build_identity_does_not_retry_or_sleep_on_success(monkeypatch):
    """The normal path pays nothing: one resolve, no backoff."""
    calls = []
    slept = []
    monkeypatch.setattr(
        build_info, "resolve_git_sha",
        lambda project_root=build_info.PROJECT_ROOT: (calls.append(1), "deadbee")[1],
    )
    monkeypatch.setattr(build_info._time, "sleep", lambda s: slept.append(s))

    assert build_info.build_identity()["git_sha"] == "deadbee"
    assert len(calls) == 1
    assert slept == [], "a successful capture must not sleep"


def test_build_identity_still_captures_at_call_time_not_live_git(monkeypatch):
    """Guard the semantic the retry must not trade away: the value is whatever
    resolved during *this* call. Re-resolving later would let a process running
    old code report a newer HEAD as a confident "fresh" — worse than unknown."""
    monkeypatch.setattr(
        build_info, "resolve_git_sha",
        lambda project_root=build_info.PROJECT_ROOT: "first01",
    )
    first = build_info.build_identity()
    monkeypatch.setattr(
        build_info, "resolve_git_sha",
        lambda project_root=build_info.PROJECT_ROOT: "second2",
    )
    second = build_info.build_identity()

    assert first["git_sha"] == "first01"
    assert second["git_sha"] == "second2"
