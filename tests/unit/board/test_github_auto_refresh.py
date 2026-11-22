"""The open Board's GitHub refresh: one server-side, single-flight refresh per TTL.

A visible Board tab's poll carries the view header; the server, not each tab,
decides when the ``gh`` sweep runs (``src/board/view_refresh.py``). The CI
sweep's default-branch read is remembered per repo, so a refresh spawns no
``git`` for a repo whose ``origin/HEAD`` did not move. Clocks are injected;
nothing here waits on real time. Synthetic repos and owners only.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from src.board import ci_runs, view_refresh
from src.clients import github as github_client

TTL = view_refresh.REFRESH_TTL_SECONDS


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _HeldSpawn:
    """Keeps the refresh 'running' until the test lets it finish."""

    def __init__(self) -> None:
        self.pending: list = []

    def __call__(self, target) -> None:
        self.pending.append(target)

    def finish(self) -> None:
        self.pending.pop(0)()


def _gate():
    clock, spawn = _Clock(), _HeldSpawn()
    return view_refresh.ViewRefresh(clock=clock, spawn=spawn), clock, spawn


def test_a_never_filled_cache_starts_one_refresh_and_overlapping_views_share_it():
    gate, _clock, spawn = _gate()
    runs: list = []
    assert gate.maybe_start(None, lambda: runs.append(1)) is True
    for _ in range(5):  # five tabs polling while it runs
        assert gate.maybe_start(None, lambda: runs.append(1)) is False
    assert len(spawn.pending) == 1
    spawn.finish()
    assert runs == [1]


def test_one_attempt_per_ttl_even_when_the_refresh_failed():
    gate, clock, spawn = _gate()

    def failing() -> None:
        raise RuntimeError("gh down")

    assert gate.maybe_start(None, failing) is True
    spawn.finish()  # the failure is logged, not raised, and frees the gate
    clock.now += TTL - 1
    assert gate.maybe_start(None, failing) is False, "a failed refresh is not retried every poll"
    clock.now += 1
    assert gate.maybe_start(None, failing) is True


def test_a_cache_younger_than_the_ttl_is_left_alone():
    gate, clock, spawn = _gate()
    assert gate.maybe_start(TTL - 1, lambda: None) is False
    assert gate.maybe_start(TTL + 1, lambda: None) is True
    spawn.finish()
    clock.now += TTL
    assert gate.maybe_start(30.0, lambda: None) is False, "a ↻ tap just filled it"


def test_board_polls_with_the_view_header_start_one_refresh_for_every_tab(webapp_client, monkeypatch):
    client, app, _overrides = webapp_client
    clock, spawn = _Clock(), _HeldSpawn()
    app.state.board_view_refresh = view_refresh.ViewRefresh(clock=clock, spawn=spawn)
    refreshed: list = []
    monkeypatch.setattr(view_refresh, "refresh_all", lambda cfg: refreshed.append(cfg))

    assert client.get("/api/board").status_code == 200
    assert spawn.pending == [], "a poll without the view header (a hidden page) starts nothing"

    for _ in range(3):  # three tabs' polls inside one refresh
        assert client.get("/api/board", headers={view_refresh.VIEW_HEADER: "1"}).status_code == 200
    assert len(spawn.pending) == 1
    spawn.finish()
    assert len(refreshed) == 1

    clock.now += TTL
    client.get("/api/board", headers={view_refresh.VIEW_HEADER: "1"})
    assert len(spawn.pending) == 1, "the never-filled cache is due again a TTL later"


def test_the_refresh_tap_always_runs(webapp_client, monkeypatch):
    client, _app, _overrides = webapp_client
    calls: list = []

    def _nothing_open(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="[]", stderr="")

    monkeypatch.setattr(github_client.subprocess, "run", _nothing_open)
    first = client.post("/api/board/github/refresh").json()
    assert first["fetched_at"] and calls
    seen = len(calls)
    client.post("/api/board/github/refresh")
    assert len(calls) > seen


def _repo_with_origin_head(root: Path) -> Path:
    head = root / "repo" / ".git" / "refs" / "remotes" / "origin" / "HEAD"
    head.parent.mkdir(parents=True)
    head.write_text("ref: refs/remotes/origin/main\n", encoding="utf-8")
    return root / "repo"


def _count_git(monkeypatch, answer: str = "origin/main") -> list:
    calls: list = []

    def fake_run_git(repo, args, **_kw):
        calls.append(args)
        return answer

    monkeypatch.setattr(ci_runs, "run_git", fake_run_git)
    return calls


def test_the_default_branch_is_read_once_until_its_ref_file_changes(tmp_path, monkeypatch):
    ci_runs.reset_ci_cache()
    repo = _repo_with_origin_head(tmp_path)
    calls = _count_git(monkeypatch)
    for tick in (0.0, 60.0, 600.0):
        assert ci_runs.default_branch(repo, now=tick) == "main"
    assert len(calls) == 1, "an unchanged origin/HEAD spawns no second git"

    head = repo / ".git" / "refs" / "remotes" / "origin" / "HEAD"
    stat = head.stat()
    os.utime(head, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10_000_000_000))
    assert ci_runs.default_branch(repo, now=601.0) == "main"
    assert len(calls) == 2, "a rewritten origin/HEAD is read again at once"

    assert ci_runs.default_branch(repo, now=601.0 + ci_runs.DEFAULT_BRANCH_TTL_SECONDS) == "main"
    assert len(calls) == 3, "the TTL re-reads even an unchanged ref"


def test_an_unset_origin_head_is_remembered_until_it_appears(tmp_path, monkeypatch):
    ci_runs.reset_ci_cache()
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    calls = _count_git(monkeypatch, answer="")
    assert ci_runs.default_branch(repo, now=0.0) is None
    assert ci_runs.default_branch(repo, now=10.0) is None
    assert len(calls) == 1

    _repo_with_origin_head(tmp_path)
    monkeypatch.setattr(ci_runs, "run_git", lambda *_a, **_k: (calls.append(1), "origin/trunk")[1])
    assert ci_runs.default_branch(repo, now=20.0) == "trunk"
    assert len(calls) == 2
