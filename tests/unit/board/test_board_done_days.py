"""Done lane's earlier days: the local-day window and GET /api/board/done."""

from __future__ import annotations

import json
import subprocess
from datetime import date, datetime, timedelta, timezone, tzinfo

from src.clients import github as github_client

_ZERO = timedelta(0)
_HOUR = timedelta(hours=1)


class _Cet(tzinfo):
    """Central Europe with the EU DST rule (the zone database is not installed
    on Windows): CEST from 01:00 UTC on the last Sunday of March until 01:00
    UTC on the last Sunday of October."""

    @staticmethod
    def _last_sunday(year: int, month: int) -> datetime:
        d = datetime(year, month, 31, 1)
        return d - timedelta(days=(d.weekday() + 1) % 7)

    def _is_dst_utc(self, utc: datetime) -> bool:
        naive = utc.replace(tzinfo=None)
        return self._last_sunday(naive.year, 3) <= naive < self._last_sunday(naive.year, 10)

    def utcoffset(self, dt):
        return 2 * _HOUR if self._is_dst_utc(dt.replace(tzinfo=None) - _HOUR) else _HOUR

    def dst(self, dt):
        return _HOUR if self.utcoffset(dt) == 2 * _HOUR else _ZERO

    def tzname(self, dt):
        return "CEST" if self.dst(dt) else "CET"


def _window(day: date):
    start, end = github_client.local_day_window(day, _Cet())
    return start.isoformat(), end.isoformat()


def test_local_day_window_is_the_local_calendar_day_in_utc():
    # Summer: local midnight is 22:00 UTC the evening before.
    assert _window(date(2026, 7, 2)) == (
        "2026-07-01T22:00:00+00:00", "2026-07-02T21:59:59+00:00")
    # Winter: one hour apart from UTC.
    assert _window(date(2026, 12, 2)) == (
        "2026-12-01T23:00:00+00:00", "2026-12-02T22:59:59+00:00")


def test_local_day_window_spans_25_and_23_hours_across_the_dst_changes():
    start, end = github_client.local_day_window(date(2026, 10, 25), _Cet())
    assert end - start == timedelta(hours=25) - timedelta(seconds=1)
    assert start.isoformat() == "2026-10-24T22:00:00+00:00"   # still CEST at midnight
    assert end.isoformat() == "2026-10-25T22:59:59+00:00"     # next midnight is CET

    start, end = github_client.local_day_window(date(2026, 3, 29), _Cet())
    assert end - start == timedelta(hours=23) - timedelta(seconds=1)
    assert start.isoformat() == "2026-03-28T23:00:00+00:00"
    assert end.isoformat() == "2026-03-29T21:59:59+00:00"


def test_consecutive_local_days_leave_no_gap_and_no_overlap():
    for first in (date(2026, 10, 24), date(2026, 10, 25), date(2026, 3, 28), date(2026, 3, 29)):
        _, end = github_client.local_day_window(first, _Cet())
        next_start, _ = github_client.local_day_window(first + timedelta(days=1), _Cet())
        assert next_start - end == timedelta(seconds=1), first


def test_search_done_on_asks_gh_for_the_days_window(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout="[]", stderr="")

    monkeypatch.setattr(github_client.subprocess, "run", fake_run)
    github_client.search_done_on("owner", date(2026, 10, 25), _Cet())
    argv = seen["argv"]
    assert argv[argv.index("--closed") + 1] == "2026-10-24T22:00:00Z..2026-10-25T22:59:59Z"


def test_api_done_returns_one_earlier_local_day(webapp_client, monkeypatch):
    client, _app, _overrides = webapp_client
    calls = []
    row = {
        "repository": {"nameWithOwner": "o/app-launcher"}, "number": 5, "title": "shipped",
        "url": "u5", "updatedAt": "2026-07-01T10:00:00Z", "closedAt": "2026-07-01T09:00:00Z",
        "labels": [],
    }

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps([row]), stderr="")

    monkeypatch.setattr(github_client.subprocess, "run", fake_run)
    body = client.get("/api/board/done?days_back=2").json()
    assert body["days_back"] == 2
    assert body["day"] == (date.today() - timedelta(days=2)).isoformat()
    assert [(i["repo"], i["number"], i["closed_at"]) for i in body["items"]] == [
        ("app-launcher", 5, "2026-07-01T09:00:00Z")]
    assert len(calls) == 1                      # one gh search per request


def test_api_done_rejects_a_day_outside_the_week_without_calling_gh(webapp_client, monkeypatch):
    client, _app, _overrides = webapp_client

    def no_gh(argv, **kwargs):
        raise AssertionError("gh must not run")

    monkeypatch.setattr(github_client.subprocess, "run", no_gh)
    for bad in ("0", "8", "-1", "x"):
        assert client.get(f"/api/board/done?days_back={bad}").status_code in (400, 422), bad


def test_api_done_surfaces_a_gh_failure_and_sits_behind_auth(webapp_client, monkeypatch):
    client, app, _overrides = webapp_client

    def failing(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="rate limited")

    monkeypatch.setattr(github_client.subprocess, "run", failing)
    res = client.get("/api/board/done?days_back=1")
    assert res.status_code == 502 and "rate limited" in res.json()["detail"]

    app.state.webapp_config.auth_token = "secret-token"
    assert client.get("/api/board/done?days_back=1").status_code == 401


def test_the_default_zone_is_the_hosts():
    start, end = github_client.local_day_window(date(2026, 7, 2))
    assert start.tzinfo == timezone.utc and end - start in (
        timedelta(hours=24) - timedelta(seconds=1),
        timedelta(hours=23) - timedelta(seconds=1),
        timedelta(hours=25) - timedelta(seconds=1),
    )
