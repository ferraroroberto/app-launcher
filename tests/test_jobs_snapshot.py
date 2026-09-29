"""The in-memory jobs runtime snapshot behind ``/api/jobs`` and ``/api/board`` (#1324).

Both polls used to walk every job's run history on every request (reap, then
``latest_run``: ``iterdir`` plus a ``run.json`` read per retained run), which
under disk contention took the Jobs tab 8 s to open. These pin the contract
that replaced it:

* a warm snapshot answers both endpoints without reading a single run record;
* a run written in this process (start, kill, pin, reap) is visible on the
  very next read, not a tick later;
* a snapshot older than ``MAX_AGE_SECONDS`` is rebuilt before it answers,
  never served as current, and every ``/api/jobs`` answer carries its age;
* the background tick walks only while the polls are asking;
* ``GET /`` serves a cached body with an ETag and answers a revalidation 304.

The conftest's ``_fresh_jobs_snapshot`` forces a rebuild per read for every
other test; these put the real ages back.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta

import pytest

from src import board, jobs_history, jobs_reap, jobs_snapshot, jobs_stats
from tests.test_board import _seed_job


@pytest.fixture
def real_ages(monkeypatch):
    monkeypatch.setattr(jobs_snapshot, "MAX_AGE_SECONDS", 10.0)
    monkeypatch.setattr(jobs_snapshot, "DEMAND_WINDOW_SECONDS", 60.0)


def _failed_today(now: datetime) -> dict:
    return {
        "status": "failed",
        "started_at": (now - timedelta(hours=1)).isoformat(timespec="seconds"),
        "finished_at": (now - timedelta(minutes=50)).isoformat(timespec="seconds"),
        "exit_code": 1,
    }


def _forbid_run_reads(monkeypatch) -> list:
    """Make any run-history read on the request path fail loudly."""
    calls: list = []

    def _boom(*args, **kwargs):
        calls.append(args)
        raise AssertionError("a GET handler walked the run history (#1324)")

    # Every module that binds the walk by name, not just its owner.
    for module in (jobs_history, jobs_stats, jobs_reap):
        monkeypatch.setattr(module, "list_runs", _boom)
    for module in (jobs_history, jobs_reap):
        monkeypatch.setattr(module, "read_run", _boom)
    return calls


def test_warm_snapshot_serves_both_polls_without_reading_a_run(
    webapp_client, real_ages, monkeypatch
):
    client, _app, overrides = webapp_client
    local_now = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
    _seed_job(overrides, "pipeline", _failed_today(local_now))

    first = client.get("/api/jobs").json()  # cold: builds the snapshot
    assert first["jobs"][0]["last_run"]["status"] == "failed"

    calls = _forbid_run_reads(monkeypatch)
    for _ in range(3):
        resp = client.get("/api/jobs")
        assert resp.status_code == 200
        body = resp.json()
        assert body["jobs"][0]["last_run"]["status"] == "failed"
        assert body["jobs"][0]["stats"] == first["jobs"][0]["stats"]
        assert body["snapshot"]["age_seconds"] <= jobs_snapshot.MAX_AGE_SECONDS
    cards = board.jobs_attention(now=local_now)
    assert [(c["job_id"], c["state"]) for c in cards] == [("pipeline", "failed")]
    assert client.get("/api/board").status_code == 200
    assert calls == []


def test_a_run_written_here_is_visible_on_the_next_read(webapp_client, real_ages):
    client, _app, overrides = webapp_client
    local_now = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
    _seed_job(overrides, "pipeline", _failed_today(local_now))
    assert client.get("/api/jobs").json()["jobs"][0]["running"] is False

    run_dir = jobs_history.new_run_dir("pipeline", "20260702T100000")
    jobs_history.write_run_json(
        run_dir, run_id="20260702T100000", job_id="pipeline", status="running",
        started_at=local_now.isoformat(timespec="seconds"),
    )

    job = client.get("/api/jobs").json()["jobs"][0]
    assert job["running"] is True
    assert job["last_run"]["run_id"] == "20260702T100000"


def test_a_stale_snapshot_is_rebuilt_before_it_answers(
    webapp_client, real_ages, monkeypatch
):
    client, _app, overrides = webapp_client
    local_now = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
    _seed_job(overrides, "pipeline", _failed_today(local_now))
    client.get("/api/jobs")

    # A run the detached executor finished in its own process: no dirty mark.
    run_dir = overrides["tmp_jobs_runs_dir"] / "pipeline" / "20260702T110000"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps({
        "run_id": "20260702T110000", "job_id": "pipeline", "status": "success",
        "started_at": local_now.isoformat(timespec="seconds"), "exit_code": 0,
    }), encoding="utf-8")
    # Within MAX_AGE the snapshot still answers, and says how old it is.
    fresh = client.get("/api/jobs").json()
    assert fresh["jobs"][0]["last_run"]["status"] == "failed"
    assert fresh["snapshot"]["age_seconds"] <= jobs_snapshot.MAX_AGE_SECONDS

    monkeypatch.setattr(jobs_snapshot, "MAX_AGE_SECONDS", 0.0)
    snap = jobs_snapshot._snapshot
    snap.built_monotonic -= 1.0  # older than the (now zero) limit
    body = client.get("/api/jobs").json()
    assert body["jobs"][0]["last_run"]["run_id"] == "20260702T110000"
    assert jobs_snapshot._snapshot is not snap


def test_a_job_whose_history_is_unreadable_raises_on_jobs_and_cards_on_board(
    webapp_client, real_ages, monkeypatch
):
    client, _app, overrides = webapp_client
    local_now = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
    _seed_job(overrides, "pipeline", _failed_today(local_now))

    def _unreadable(job_id):
        raise PermissionError("locked")

    monkeypatch.setattr(jobs_history, "list_runs", _unreadable)
    cards = board.jobs_attention(now=local_now)
    assert [(c["job_id"], c["state"]) for c in cards] == [("pipeline", "unreadable")]
    with pytest.raises(PermissionError):
        client.get("/api/jobs")


def test_the_tick_walks_only_while_the_polls_are_asking(real_ages, monkeypatch):
    builds: list = []
    monkeypatch.setattr(jobs_snapshot, "TICK_SECONDS", 0.01)
    monkeypatch.setattr(jobs_snapshot, "rebuild", lambda reason, **kw: builds.append(reason))

    async def _run_for(seconds: float) -> None:
        task = asyncio.create_task(jobs_snapshot.tick_forever())
        await asyncio.sleep(seconds)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_run_for(0.1))
    assert builds == [], "an unwatched launcher must not walk the disk"

    jobs_snapshot._last_demand = jobs_snapshot.time.monotonic()
    asyncio.run(_run_for(0.1))
    assert builds and set(builds) == {"tick"}


def test_index_is_cached_with_an_etag_and_revalidates_304(webapp_client, monkeypatch):
    client, app, _overrides = webapp_client
    from app.webapp.routers import misc

    first = client.get("/")
    assert first.status_code == 200
    etag = first.headers["etag"]
    assert first.headers["cache-control"] == "no-cache, must-revalidate"

    reads: list = []
    real_read_text = misc.Path.read_text

    def _counting_read_text(self, *args, **kwargs):
        reads.append(self)
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(misc.Path, "read_text", _counting_read_text)
    again = client.get("/")
    assert again.status_code == 200 and again.text == first.text
    assert [p for p in reads if p.name == "index.html"] == [], "served from cache"

    revalidated = client.get("/", headers={"If-None-Match": etag})
    assert revalidated.status_code == 304
    assert revalidated.content == b""
    assert revalidated.headers["etag"] == etag
    assert client.get("/", headers={"If-None-Match": '"other"'}).status_code == 200
