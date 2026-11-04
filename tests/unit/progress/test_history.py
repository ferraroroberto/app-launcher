"""GET /api/progress: bucketing, cache-first reads and each half failing on its own."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any, Dict, List

import pytest

from src.clients import hub_token_history
from src.progress import buckets, history, tokens, totals

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)


def _store(days_by_repo: Dict[str, Dict[str, Dict[str, int]]], **extra: Any) -> Dict[str, Any]:
    repos = {
        name: {"owner": "me", "repo": name, "cursor": "2026-11-04T10:00:00Z", "read_at": "2026-11-04T10:00:00Z",
               "error": None, "days": days}
        for name, days in days_by_repo.items()
    }
    return {"version": 1, "refreshed_at": "2026-11-04T10:00:00Z", "error": None, "repos": repos, "skipped": [], **extra}


def _day(added=0, removed=0, merged=0, opened=0, closed=0) -> Dict[str, int]:
    return {"lines_added": added, "lines_removed": removed, "prs_merged": merged,
            "issues_opened": opened, "issues_closed": closed}


def _cell(tokens_in: int, tokens_out: int) -> Dict[str, Any]:
    return {"state": "measured", "tokens_in": tokens_in, "output_tokens": tokens_out}


def _hub(rows: Dict[str, Dict[str, Any]], state: str = "ok", refreshing: bool = False, error=None) -> Dict[str, Any]:
    days = [
        {"date": d, "vendors": {"grok": {"state": "not_measured"}, **vendors}} for d, vendors in sorted(rows.items())
    ]
    return {
        "from": min(rows), "to": max(rows), "days": days,
        "cache": {"state": state, "computed_at": "2026-11-04T11:00:00+00:00", "age_seconds": 3600.0,
                  "stale": state == "stale", "refreshing": refreshing, "last_error": error},
    }


class FakeHub:
    """Stands in for ``hub_token_history.fetch``; records each call."""

    def __init__(self, body: Dict[str, Any]) -> None:
        self.body = body
        self.calls: List[tuple] = []
        self.error: hub_token_history.HubTokenHistoryError | None = None

    def __call__(self, base_url, first, last, *, refresh=False):
        self.calls.append((first, last, refresh))
        if self.error:
            raise self.error
        return self.body


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    tokens.reset()
    monkeypatch.setattr(totals, "TOTALS_FILE", tmp_path / "progress-totals.json")
    started: List[tuple] = []
    monkeypatch.setattr(totals, "start_refresh", lambda *a, **k: started.append(a) or True)
    monkeypatch.setattr(totals, "_in_flight", False)
    yield started
    tokens.reset()


def _save(store: Dict[str, Any]) -> None:
    totals.TOTALS_FILE.write_text(json.dumps(store), encoding="utf-8")


def _read(hub: FakeHub, monkeypatch, *, bucket="day", first=date(2026, 11, 1), last=date(2026, 11, 4), refresh=False):
    monkeypatch.setattr(hub_token_history, "fetch", hub)
    return history.read(
        bucket=bucket, first=first, last=last, hub_url="http://hub", fleet_config_dir="f", projects_dir="p",
        refresh=refresh, now=NOW,
    )


class TestBuckets:
    def test_day_week_month_plans_cover_the_range_with_empty_buckets(self):
        days = buckets.bucket_plan(date(2026, 10, 30), date(2026, 11, 2), "day")
        assert [b["start"] for b in days] == ["2026-10-30", "2026-10-31", "2026-11-01", "2026-11-02"]
        # 2026-11-02 is a Monday; the range opens on a Friday of the week before.
        weeks = buckets.bucket_plan(date(2026, 10, 30), date(2026, 11, 9), "week")
        assert [(b["start"], b["end"]) for b in weeks] == [
            ("2026-10-26", "2026-11-01"), ("2026-11-02", "2026-11-08"), ("2026-11-09", "2026-11-15"),
        ]
        assert weeks[0]["days"] == ["2026-10-30", "2026-10-31", "2026-11-01"]
        months = buckets.bucket_plan(date(2026, 10, 30), date(2026, 12, 2), "month")
        assert [(b["start"], b["end"]) for b in months] == [
            ("2026-10-01", "2026-10-31"), ("2026-11-01", "2026-11-30"), ("2026-12-01", "2026-12-31"),
        ]
        assert len(months[1]["days"]) == 30 and months[2]["days"] == ["2026-12-01", "2026-12-02"]

    def test_a_leap_february_ends_on_the_29th(self):
        assert buckets.bucket_end(date(2028, 2, 1), "month") == date(2028, 2, 29)

    def test_range_defaults_to_thirty_days_ending_today(self):
        assert buckets.parse_range(None, None, today=date(2026, 11, 4)) == (date(2026, 10, 6), date(2026, 11, 4))

    @pytest.mark.parametrize(
        ("from_", "to"),
        [("2026-11-05", "2026-11-04"), ("nope", None), (None, "2026-13-01"), ("2024-01-01", "2026-11-04")],
    )
    def test_bad_ranges_are_refused_with_a_sentence(self, from_, to):
        with pytest.raises(ValueError, match="."):
            buckets.parse_range(from_, to, today=date(2026, 11, 4))


class TestCombined:
    def test_week_bucket_sums_github_and_tokens_by_their_own_days(self, monkeypatch):
        _save(_store({
            "alpha": {"2026-11-02": _day(10, 2, 1, 1, 0), "2026-11-03": _day(5, 1, 0, 0, 1)},
            "beta": {"2026-11-03": _day(100, 50, 2, 0, 0)},
        }))
        hub = FakeHub(_hub({
            "2026-11-02": {"claude": _cell(100, 10)},
            "2026-11-03": {"claude": _cell(200, 20), "codex": _cell(7, 3)},
        }))
        out = _read(hub, monkeypatch, bucket="week", first=date(2026, 11, 2), last=date(2026, 11, 3))
        (week,) = out["buckets"]
        assert week["start"] == "2026-11-02" and week["end"] == "2026-11-08"
        assert week["github"] == _day(115, 53, 3, 1, 1)
        assert week["tokens"]["claude"] == {"state": "measured", "tokens_in": 300, "tokens_out": 30, "unknown_days": 0}
        assert week["tokens"]["codex"]["unknown_days"] == 1
        assert week["tokens"]["grok"]["state"] == "not_measured" and week["tokens"]["grok"]["tokens_in"] is None
        assert [r["repo"] for r in out["repos"]] == ["alpha", "beta"]
        assert out["repos"][1]["lines_added"] == 100
        assert out["github"]["state"] == "ok" and out["tokens"]["state"] == "ok"
        assert out["github"]["age_seconds"] == 7200.0
        assert out["refreshing"] is False

    def test_repo_totals_are_limited_to_the_range(self, monkeypatch):
        _save(_store({"alpha": {"2026-10-01": _day(999), "2026-11-02": _day(4)}}))
        out = _read(FakeHub(_hub({"2026-11-02": {}})), monkeypatch, first=date(2026, 11, 1), last=date(2026, 11, 4))
        assert out["repos"][0]["lines_added"] == 4

    def test_a_day_with_no_activity_is_a_zero_but_never_read_is_null(self, monkeypatch, tmp_path):
        _save(_store({"alpha": {"2026-11-02": _day(4)}}))
        out = _read(FakeHub(_hub({"2026-11-02": {}})), monkeypatch)
        assert out["buckets"][0]["github"] == _day()  # a read repo with no activity that day
        totals.TOTALS_FILE.unlink()
        out = _read(FakeHub(_hub({"2026-11-02": {}})), monkeypatch)
        assert out["github"]["state"] == "unknown"
        assert all(b["github"] is None for b in out["buckets"])
        assert out["repos"] == []

    def test_unmeasured_token_days_stay_unknown_not_zero(self, monkeypatch):
        _save(_store({"alpha": {}}))
        hub = FakeHub(_hub({
            "2026-11-01": {"claude": {"state": "unknown"}},
            "2026-11-02": {"claude": _cell(50, 5)},
        }))
        out = _read(hub, monkeypatch, bucket="month", first=date(2026, 11, 1), last=date(2026, 11, 2))
        assert out["buckets"][0]["tokens"]["claude"] == {
            "state": "measured", "tokens_in": 50, "tokens_out": 5, "unknown_days": 1,
        }
        out = _read(hub, monkeypatch, first=date(2026, 11, 1), last=date(2026, 11, 2))
        first_day, second_day = out["buckets"]
        assert first_day["tokens"]["claude"] == {"state": "unknown", "tokens_in": None, "tokens_out": None, "unknown_days": 1}
        assert second_day["tokens"]["claude"]["tokens_in"] == 50

    def test_an_empty_hub_cache_is_unknown_with_a_reason(self, monkeypatch):
        _save(_store({"alpha": {}}))
        body = _hub({"2026-11-04": {"claude": {"state": "unknown"}}}, state="empty")
        out = _read(FakeHub(body), monkeypatch)
        assert out["tokens"]["state"] == "unknown" and "not computed" in out["tokens"]["reason"]

    def test_a_failed_hub_rebuild_is_stale_with_its_error(self, monkeypatch):
        _save(_store({"alpha": {}}))
        body = _hub({"2026-11-04": {"claude": _cell(1, 1)}}, state="stale", error="disk full")
        out = _read(FakeHub(body), monkeypatch)
        assert out["tokens"]["state"] == "stale" and out["tokens"]["reason"] == "disk full"
        assert out["buckets"][-1]["tokens"]["claude"]["tokens_in"] == 1


class TestCacheFirst:
    def test_a_plain_read_starts_no_refresh_and_asks_the_hub_for_the_cache(self, monkeypatch, _isolated):
        _save(_store({"alpha": {}}))
        hub = FakeHub(_hub({"2026-11-04": {}}))
        _read(hub, monkeypatch)
        assert _isolated == []
        assert hub.calls == [("2026-11-01", "2026-11-04", False)]

    def test_refresh_starts_both_and_still_answers_from_the_cache(self, monkeypatch, _isolated):
        _save(_store({"alpha": {"2026-11-04": _day(3)}}))
        hub = FakeHub(_hub({"2026-11-04": {"claude": _cell(9, 1)}}))
        out = _read(hub, monkeypatch, refresh=True)
        assert len(_isolated) == 1
        assert hub.calls == [("2026-11-01", "2026-11-04", True)]
        assert out["refreshing"] is True
        assert out["github"]["refreshing"] is True and out["tokens"]["refreshing"] is True
        assert out["buckets"][-1]["github"]["lines_added"] == 3  # the cache, not a recalculation

    def test_a_refresh_already_running_is_reported_on_a_plain_read(self, monkeypatch):
        _save(_store({"alpha": {}}))
        monkeypatch.setattr(totals, "_in_flight", True)
        out = _read(FakeHub(_hub({"2026-11-04": {}})), monkeypatch)
        assert out["refreshing"] is True and out["tokens"]["refreshing"] is False

    def test_the_hub_still_rebuilding_keeps_refreshing_true(self, monkeypatch):
        _save(_store({"alpha": {}}))
        out = _read(FakeHub(_hub({"2026-11-04": {}}, refreshing=True)), monkeypatch)
        assert out["refreshing"] is True

    def test_a_failed_github_refresh_keeps_the_last_days_and_flags_stale(self, monkeypatch):
        store = _store({"alpha": {"2026-11-04": _day(8)}})
        store["repos"]["alpha"]["error"] = "GhError: boom"
        _save(store)
        out = _read(FakeHub(_hub({"2026-11-04": {}})), monkeypatch)
        assert out["github"]["state"] == "stale"
        assert out["buckets"][-1]["github"]["lines_added"] == 8
        assert out["repos"][0]["state"] == "stale"


class TestDegradedHalves:
    def test_hub_down_leaves_the_github_half_whole(self, monkeypatch):
        _save(_store({"alpha": {"2026-11-04": _day(8)}}))
        hub = FakeHub({})
        hub.error = hub_token_history.HubTokenHistoryError("local-llm-hub unreachable at http://hub", status=503)
        out = _read(hub, monkeypatch)
        assert out["tokens"]["state"] == "unavailable" and "unreachable" in out["tokens"]["reason"]
        assert all(b["tokens"] is None for b in out["buckets"])
        assert out["github"]["state"] == "ok"
        assert out["buckets"][-1]["github"]["lines_added"] == 8

    def test_an_old_hub_without_the_route_says_to_restart_it(self, monkeypatch):
        _save(_store({"alpha": {}}))
        hub = FakeHub({})
        hub.error = hub_token_history.HubTokenHistoryError("Not Found", status=404)
        out = _read(hub, monkeypatch)
        assert out["tokens"]["state"] == "unavailable" and "restart the hub" in out["tokens"]["reason"]

    def test_hub_going_down_after_a_good_read_serves_the_last_answer_as_stale(self, monkeypatch):
        _save(_store({"alpha": {}}))
        good = FakeHub(_hub({"2026-11-04": {"claude": _cell(40, 4)}}))
        _read(good, monkeypatch)
        down = FakeHub({})
        down.error = hub_token_history.HubTokenHistoryError("unreachable", status=503)
        out = _read(down, monkeypatch)
        assert out["tokens"]["state"] == "stale" and "last answer" in out["tokens"]["reason"]
        assert out["buckets"][-1]["tokens"]["claude"]["tokens_in"] == 40
        other_range = _read(down, monkeypatch, first=date(2026, 10, 1))
        assert other_range["tokens"]["state"] == "unavailable"

    def test_an_unconfigured_hub_is_unavailable_without_a_call(self, monkeypatch):
        _save(_store({"alpha": {}}))
        hub = FakeHub(_hub({"2026-11-04": {}}))
        monkeypatch.setattr(hub_token_history, "fetch", hub)
        out = history.read(
            bucket="day", first=date(2026, 11, 1), last=date(2026, 11, 4), hub_url="",
            fleet_config_dir="f", projects_dir="p", now=NOW,
        )
        assert out["tokens"]["state"] == "unavailable" and hub.calls == []

    def test_github_unreadable_leaves_the_token_half_whole(self, monkeypatch):
        def boom(*_a, **_k):
            raise OSError("disk gone")

        monkeypatch.setattr(totals, "snapshot", boom)
        out = _read(FakeHub(_hub({"2026-11-04": {"claude": _cell(5, 1)}})), monkeypatch)
        assert out["github"]["state"] == "unavailable" and "disk gone" in out["github"]["reason"]
        assert all(b["github"] is None for b in out["buckets"])
        assert out["buckets"][-1]["tokens"]["claude"]["tokens_in"] == 5

    def test_an_unreadable_saved_file_is_unavailable_not_empty(self, monkeypatch):
        totals.TOTALS_FILE.write_text("{not json", encoding="utf-8")
        out = _read(FakeHub(_hub({"2026-11-04": {}})), monkeypatch)
        assert out["github"]["state"] == "unavailable" and out["github"]["reason"]

    def test_a_repo_never_read_is_listed_unknown_beside_the_ones_that_were(self, monkeypatch):
        store = _store({"alpha": {"2026-11-04": _day(2)}})
        store["repos"]["beta"] = {"owner": "me", "repo": "beta", "days": {}}
        _save(store)
        out = _read(FakeHub(_hub({"2026-11-04": {}})), monkeypatch)
        assert out["github"]["unknown_repos"] == ["beta"]
        assert [r["repo"] for r in out["repos"]] == ["alpha"]


class TestRoute:
    def test_it_answers_with_both_halves(self, webapp_client, monkeypatch):
        client, _app, _ = webapp_client
        _save(_store({"alpha": {"2026-11-04": _day(3)}}))
        monkeypatch.setattr(hub_token_history, "fetch", FakeHub(_hub({"2026-11-04": {"claude": _cell(2, 1)}})))
        resp = client.get("/api/progress", params={"bucket": "week", "from": "2026-11-02", "to": "2026-11-04"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["bucket"] == "week" and body["github"]["state"] == "ok"
        assert body["buckets"][0]["github"]["lines_added"] == 3

    def test_it_defaults_to_thirty_daily_buckets(self, webapp_client, monkeypatch):
        client, _app, _ = webapp_client
        monkeypatch.setattr(hub_token_history, "fetch", FakeHub(_hub({"2026-11-04": {}})))
        body = client.get("/api/progress").json()
        assert body["bucket"] == "day" and len(body["buckets"]) == 30

    @pytest.mark.parametrize(
        "params",
        [{"bucket": "year"}, {"from": "2026-11-05", "to": "2026-11-01"}, {"from": "x"}, {"from": "2020-01-01"}],
    )
    def test_bad_input_is_422(self, webapp_client, params):
        client, _app, _ = webapp_client
        resp = client.get("/api/progress", params=params)
        assert resp.status_code == 422 and resp.json()["detail"]

    def test_hub_down_still_answers_200(self, webapp_client, monkeypatch):
        client, _app, _ = webapp_client
        hub = FakeHub({})
        hub.error = hub_token_history.HubTokenHistoryError("unreachable", status=503)
        monkeypatch.setattr(hub_token_history, "fetch", hub)
        resp = client.get("/api/progress", params={"from": "2026-11-01", "to": "2026-11-04"})
        assert resp.status_code == 200 and resp.json()["tokens"]["state"] == "unavailable"
