"""Fleet GitHub daily totals: backfill, incremental refresh, cache and failure states."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pytest

from src.clients import github as github_client
from src.progress import totals

UTC = timezone.utc
NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)


def _raw(commits=(), merged=(), opened=(), closed=(), remaining=4000) -> Dict[str, Any]:
    return {
        "commits": list(commits), "merged": list(merged),
        "opened": list(opened), "closed": list(closed), "remaining": remaining,
    }


class FakeReader:
    """Stands in for ``github_client.repo_history``; records each call's window."""

    def __init__(self, data: Dict[str, Dict[str, Any]]) -> None:
        self.data = data
        self.calls: List[tuple] = []
        self.fail: set = set()

    def __call__(self, owner: str, repo: str, since: Optional[datetime]) -> Dict[str, Any]:
        self.calls.append((repo, since))
        if repo in self.fail:
            raise github_client.GhError(f"{repo}: boom")
        return self.data[repo]


@pytest.fixture
def registry(monkeypatch):
    """Two readable repos, one with no remote, one with no checkout."""

    def _resolve(_fleet, _projects):
        readable = [
            {"name": "alpha", "owner": "me", "repo": "alpha"},
            {"name": "beta", "owner": "me", "repo": "beta"},
        ]
        skipped = [
            {"repo": "local-only", "reason": "no GitHub remote"},
            {"repo": "ghost", "reason": "no checkout"},
        ]
        return readable, skipped

    monkeypatch.setattr(totals, "resolve_repos", _resolve)


def _refresh(tmp_path, reader, now=NOW):
    return totals.refresh("fleet", "projects", path=tmp_path / "t.json", now=now, tz=UTC, reader=reader)


def test_backfill_then_incremental_replaces_the_window(tmp_path, registry):
    reader = FakeReader({
        "alpha": _raw(
            commits=[("2026-10-01T10:00:00Z", 10, 2), ("2026-11-03T09:00:00Z", 5, 1)],
            merged=["2026-11-03T09:30:00Z"], opened=["2026-11-02T08:00:00Z"],
            closed=["2026-11-03T18:00:00Z"],
        ),
        "beta": _raw(commits=[("2026-11-04T01:00:00Z", 7, 7)]),
    })
    first = _refresh(tmp_path, reader)

    assert [c[1] for c in reader.calls] == [None, None]  # backfill reads everything
    alpha = first["repos"]["alpha"]
    assert alpha["state"] == "ok"
    assert alpha["days"]["2026-10-01"] == {
        "lines_added": 10, "lines_removed": 2, "prs_merged": 0, "issues_opened": 0, "issues_closed": 0,
    }
    assert alpha["days"]["2026-11-03"] == {
        "lines_added": 5, "lines_removed": 1, "prs_merged": 1, "issues_opened": 0, "issues_closed": 1,
    }
    assert alpha["days"]["2026-11-02"]["issues_opened"] == 1
    assert first["repos"]["beta"]["days"]["2026-11-04"]["lines_added"] == 7

    # Second refresh a day later: only the window is re-read, and its days are
    # replaced (not added to), while days before it are kept.
    reader.calls.clear()
    reader.data["alpha"] = _raw(commits=[("2026-11-03T09:00:00Z", 5, 1), ("2026-11-04T09:00:00Z", 3, 0)])
    second = _refresh(tmp_path, reader, now=NOW + timedelta(days=1))

    since = reader.calls[0][1]
    assert since == datetime(2026, 10, 28, tzinfo=UTC)  # the cursor's day minus the overlap
    days = second["repos"]["alpha"]["days"]
    assert days["2026-10-01"]["lines_added"] == 10  # before the window: kept
    assert days["2026-11-03"] == {  # inside it: replaced, no double count
        "lines_added": 5, "lines_removed": 1, "prs_merged": 0, "issues_opened": 0, "issues_closed": 0,
    }
    assert days["2026-11-04"]["lines_added"] == 3
    assert "2026-11-02" not in days  # the window re-read found no event that day


def test_snapshot_serves_the_cache_without_any_github_call(tmp_path, registry, monkeypatch):
    _refresh(tmp_path, FakeReader({"alpha": _raw(commits=[("2026-11-03T09:00:00Z", 1, 1)]), "beta": _raw()}))

    def _no_github(*_a, **_k):
        raise AssertionError("snapshot must not call GitHub")

    monkeypatch.setattr(github_client, "_run_gh", _no_github)
    monkeypatch.setattr(github_client, "repo_history", _no_github)
    snap = totals.snapshot(tmp_path / "t.json")

    assert snap["repos"]["alpha"]["days"]["2026-11-03"]["lines_added"] == 1
    assert snap["refreshed_at"] == "2026-11-04T12:00:00Z"
    assert snap["in_flight"] is False


def test_snapshot_before_any_refresh_is_empty_not_zero(tmp_path):
    snap = totals.snapshot(tmp_path / "missing.json")
    assert snap["refreshed_at"] is None and snap["repos"] == {} and snap["error"] is None


def test_failed_refresh_keeps_days_and_flags_stale(tmp_path, registry):
    reader = FakeReader({"alpha": _raw(commits=[("2026-11-03T09:00:00Z", 5, 1)]), "beta": _raw()})
    _refresh(tmp_path, reader)

    reader.fail.add("alpha")
    snap = _refresh(tmp_path, reader, now=NOW + timedelta(days=1))

    alpha = snap["repos"]["alpha"]
    assert alpha["state"] == "stale"
    assert "boom" in alpha["error"]
    assert alpha["days"]["2026-11-03"]["lines_added"] == 5  # previous data survives
    assert snap["repos"]["beta"]["state"] == "ok"


def test_repo_never_read_is_unknown_never_zero(tmp_path, registry):
    reader = FakeReader({"alpha": _raw(), "beta": _raw()})
    reader.fail.add("beta")
    snap = _refresh(tmp_path, reader)

    assert snap["repos"]["alpha"]["state"] == "ok"
    beta = snap["repos"]["beta"]
    assert beta["state"] == "unknown"
    assert beta["days"] == {}
    assert beta["read_at"] is None


def test_interrupted_backfill_resumes_from_the_stored_cursors(tmp_path, registry):
    reader = FakeReader({"alpha": _raw(commits=[("2026-11-03T09:00:00Z", 2, 0)]), "beta": _raw()})
    reader.fail.add("beta")
    _refresh(tmp_path, reader)

    reader.fail.clear()
    reader.calls.clear()
    snap = _refresh(tmp_path, reader, now=NOW + timedelta(hours=1))

    windows = dict(reader.calls)
    assert windows["alpha"] is not None  # already read: incremental
    assert windows["beta"] is None  # never read: still a full backfill
    assert snap["repos"]["beta"]["state"] == "ok"


def test_all_repos_failing_does_not_claim_a_refresh(tmp_path, registry):
    reader = FakeReader({})
    reader.fail.update({"alpha", "beta"})
    snap = _refresh(tmp_path, reader)
    assert snap["refreshed_at"] is None


def test_skipped_repos_are_listed_with_their_reason(tmp_path, registry):
    snap = _refresh(tmp_path, FakeReader({"alpha": _raw(), "beta": _raw()}))
    assert snap["skipped"] == [
        {"repo": "local-only", "reason": "no GitHub remote"},
        {"repo": "ghost", "reason": "no checkout"},
    ]
    assert "local-only" not in snap["repos"]


def test_low_rate_limit_stops_reading_and_marks_the_rest(tmp_path, registry):
    reader = FakeReader({"alpha": _raw(remaining=50), "beta": _raw()})
    snap = _refresh(tmp_path, reader)

    assert [c[0] for c in reader.calls] == ["alpha"]  # beta was never asked
    assert snap["repos"]["beta"]["state"] == "unknown"
    assert "rate limit" in snap["repos"]["beta"]["error"]


def test_registry_unreadable_is_reported_and_keeps_saved_totals(tmp_path, registry, monkeypatch):
    _refresh(tmp_path, FakeReader({"alpha": _raw(commits=[("2026-11-03T09:00:00Z", 4, 0)]), "beta": _raw()}))

    def _broken(*_a):
        raise totals.TotalsError("The fleet-config folder is not set in Settings.")

    monkeypatch.setattr(totals, "resolve_repos", _broken)
    snap = _refresh(tmp_path, FakeReader({}), now=NOW + timedelta(days=1))

    assert "fleet-config folder" in snap["error"]
    assert snap["repos"]["alpha"]["days"]["2026-11-03"]["lines_added"] == 4


def test_unreadable_store_is_an_error_not_an_empty_success(tmp_path):
    (tmp_path / "t.json").write_text("{not json", encoding="utf-8")
    snap = totals.snapshot(tmp_path / "t.json")
    assert snap["repos"] == {} and "could not be read" in snap["error"]


def test_days_follow_the_local_calendar_not_utc(tmp_path, registry):
    tz = timezone(timedelta(hours=-5))
    reader = FakeReader({"alpha": _raw(commits=[("2026-11-04T02:00:00Z", 9, 0)]), "beta": _raw()})
    snap = totals.refresh("f", "p", path=tmp_path / "t.json", now=NOW, tz=tz, reader=reader)
    assert list(snap["repos"]["alpha"]["days"]) == ["2026-11-03"]


def test_a_second_refresh_while_one_runs_is_a_plain_read(tmp_path, registry):
    assert totals._run_lock.acquire(blocking=False)
    try:
        reader = FakeReader({})
        snap = _refresh(tmp_path, reader)
        assert reader.calls == [] and snap["repos"] == {}
    finally:
        totals._run_lock.release()


@pytest.mark.parametrize("url,expected", [
    ("https://github.com/me/app.git", ("me", "app")),
    ("https://user@github.com/me/app", ("me", "app")),
    ("git@github.com:me/app.git", ("me", "app")),
    ("ssh://git@github.com/me/app.git", ("me", "app")),
    ("https://gitlab.com/me/app.git", None),
    ("", None),
])
def test_parse_remote(url, expected):
    assert totals.parse_remote(url) == expected


def test_resolve_repos_skips_missing_checkout_and_non_github_remote(tmp_path, monkeypatch):
    for name in ("alpha", "lab"):
        (tmp_path / name).mkdir()
    monkeypatch.setattr(
        totals.fleet_scope, "list_repos",
        lambda _dir: {"available": True, "reason": "", "repos": [
            {"name": n, "status": "active"} for n in ("alpha", "lab", "ghost")]},
    )
    remotes = {"alpha": "git@github.com:me/alpha.git", "lab": None}
    monkeypatch.setattr(totals, "run_git", lambda repo, _args: remotes[repo.name])

    readable, skipped = totals.resolve_repos("fleet", str(tmp_path))

    assert readable == [{"name": "alpha", "owner": "me", "repo": "alpha"}]
    assert skipped == [
        {"repo": "lab", "reason": "no GitHub remote"},
        {"repo": "ghost", "reason": "no checkout"},
    ]


def test_resolve_repos_raises_when_the_registry_is_unavailable(monkeypatch):
    monkeypatch.setattr(
        totals.fleet_scope, "list_repos",
        lambda _dir: {"available": False, "reason": "no registry", "repos": []},
    )
    with pytest.raises(totals.TotalsError, match="no registry"):
        totals.resolve_repos("fleet", "projects")


# ---------------------------------------------------------------- the reader


def _page(nodes, cursor=None):
    return {"pageInfo": {"hasNextPage": bool(cursor), "endCursor": cursor}, "nodes": nodes}


def _reply(c=None, p=None, i=None, remaining=4990):
    repo = {}
    if c is not None:
        repo["c"] = {"target": {"history": c}}
    if p is not None:
        repo["p"] = p
    if i is not None:
        repo["i"] = i
    return json.dumps({"data": {"rateLimit": {"cost": 1, "remaining": remaining}, "repository": repo}})


def test_repo_history_pages_each_connection_and_stops_at_since(monkeypatch):
    calls: List[List[str]] = []
    replies = [
        _reply(
            c=_page([{"committedDate": "2026-11-03T09:00:00Z", "additions": 5, "deletions": 1}], "c1"),
            p=_page([{"mergedAt": "2026-11-03T09:00:00Z", "updatedAt": "2026-11-03T09:00:00Z"}], "p1"),
            i=_page([{"createdAt": "2026-11-02T08:00:00Z", "closedAt": None}]),
        ),
        # Page 2: commits end; the PR page ends before ``since``, so PRs stop too.
        _reply(
            c=_page([{"committedDate": "2026-11-02T09:00:00Z", "additions": 2, "deletions": 2}]),
            p=_page([{"mergedAt": None, "updatedAt": "2026-10-01T00:00:00Z"}], "p2"),
        ),
    ]

    def _fake(args, **_kw):
        calls.append(list(args))
        return replies[len(calls) - 1]

    monkeypatch.setattr(github_client, "_run_gh", _fake)
    out = github_client.repo_history("me", "alpha", datetime(2026, 11, 1, tzinfo=UTC))

    assert out["commits"] == [("2026-11-03T09:00:00Z", 5, 1), ("2026-11-02T09:00:00Z", 2, 2)]
    assert out["merged"] == ["2026-11-03T09:00:00Z"]
    assert out["opened"] == ["2026-11-02T08:00:00Z"] and out["closed"] == []
    assert out["remaining"] == 4990
    assert len(calls) == 2
    second = " ".join(calls[1])
    assert "cafter=c1" in second and "pafter=p1" in second
    assert "iafter" not in second  # finished after page one: not asked again
    assert "csince=2026-11-01T00:00:00Z" in " ".join(calls[0])


def test_repo_history_backfill_sends_no_since(monkeypatch):
    seen: List[List[str]] = []

    def _fake(args, **_kw):
        seen.append(list(args))
        return _reply(c=_page([]), p=_page([]), i=_page([]))

    monkeypatch.setattr(github_client, "_run_gh", _fake)
    github_client.repo_history("me", "alpha", None)
    assert not any(a.startswith(("csince", "isince")) for a in seen[0])


def test_repo_history_unreadable_repo_is_an_error(monkeypatch):
    monkeypatch.setattr(github_client, "_run_gh", lambda *_a, **_k: json.dumps({"data": {"repository": None}}))
    with pytest.raises(github_client.GhError, match="not readable"):
        github_client.repo_history("me", "alpha")


def test_repo_history_rejects_an_unsafe_name():
    with pytest.raises(github_client.GhError, match="unsafe"):
        github_client.repo_history("me", 'x") { evil')
