"""Dismissing a failed job's Board card (#1669).

The store (``src.jobs_dismissed``), its effect on ``board.jobs_attention`` and
the ``POST /api/jobs/<id>/runs/<rid>/dismiss`` route.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from src import board, jobs_dismissed

from tests.test_board import _seed_job


def _failed_run(local_now: datetime, run_id: str, exit_code: int = 1) -> dict:
    return {
        "run_id": run_id,
        "status": "failed",
        "started_at": (local_now - timedelta(hours=1)).isoformat(timespec="seconds"),
        "finished_at": (local_now - timedelta(minutes=50)).isoformat(timespec="seconds"),
        "exit_code": exit_code,
    }


def _local_noon() -> datetime:
    return datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)


def _add_run(overrides: dict, job_id: str, run: dict) -> None:
    run_dir = overrides["tmp_jobs_runs_dir"] / job_id / run["run_id"]
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(
        json.dumps({"job_id": job_id, **run}), encoding="utf-8"
    )


# ------------------------------------------------------------------ store


def test_dismiss_round_trips_and_is_idempotent(tmp_path):
    path = tmp_path / "dismissed.json"
    assert jobs_dismissed.read_dismissed(path) == {}
    jobs_dismissed.dismiss("photos", "20260717T090000", path)
    jobs_dismissed.dismiss("photos", "20260717T090000", path)
    jobs_dismissed.dismiss("other", "20260717T100000", path)
    assert jobs_dismissed.read_dismissed(path) == {
        "photos": ["20260717T090000"],
        "other": ["20260717T100000"],
    }


def test_a_job_keeps_only_its_newest_dismissals(tmp_path):
    path = tmp_path / "dismissed.json"
    total = jobs_dismissed.MAX_PER_JOB + 3
    for n in range(total):
        jobs_dismissed.dismiss("photos", f"run{n:03d}", path)
    kept = jobs_dismissed.read_dismissed(path)["photos"]
    assert len(kept) == jobs_dismissed.MAX_PER_JOB
    assert kept[-1] == f"run{total - 1:03d}"


def test_a_corrupt_file_dismisses_nothing(tmp_path):
    path = tmp_path / "dismissed.json"
    path.write_text("{not json", encoding="utf-8")
    assert jobs_dismissed.read_dismissed(path) == {}
    path.write_text(json.dumps({"photos": "20260717T090000", "x": [1, None]}), encoding="utf-8")
    assert jobs_dismissed.read_dismissed(path) == {}


# --------------------------------------------------------- jobs_attention


def test_dismissed_failed_run_raises_no_card(webapp_client):
    _client, _app, overrides = webapp_client
    local_now = _local_noon()
    _seed_job(overrides, "photos", _failed_run(local_now, "20260717T090000"))
    assert [c["run_id"] for c in board.jobs_attention(now=local_now)] == ["20260717T090000"]

    jobs_dismissed.dismiss("photos", "20260717T090000")

    assert board.jobs_attention(now=local_now) == []


def test_dismissed_unconfirmed_run_raises_no_card(webapp_client):
    _client, _app, overrides = webapp_client
    local_now = _local_noon()
    _seed_job(overrides, "photos", _failed_run(local_now, "20260717T090000", exit_code=122))
    assert [c["state"] for c in board.jobs_attention(now=local_now)] == ["unconfirmed"]

    jobs_dismissed.dismiss("photos", "20260717T090000")

    assert board.jobs_attention(now=local_now) == []


def test_a_newer_failed_run_raises_a_fresh_card(webapp_client):
    _client, _app, overrides = webapp_client
    local_now = _local_noon()
    _seed_job(overrides, "photos", _failed_run(local_now, "20260717T090000"))
    jobs_dismissed.dismiss("photos", "20260717T090000")
    assert board.jobs_attention(now=local_now) == []

    newer = _failed_run(local_now + timedelta(minutes=5), "20260717T120500")
    _add_run(overrides, "photos", newer)
    from src import jobs_snapshot
    jobs_snapshot.reset()

    cards = board.jobs_attention(now=local_now)
    assert [(c["job_id"], c["run_id"]) for c in cards] == [("photos", "20260717T120500")]


def test_dismissal_is_per_job(webapp_client):
    _client, _app, overrides = webapp_client
    local_now = _local_noon()
    _seed_job(overrides, "photos", _failed_run(local_now, "20260717T090000"))
    _seed_job(overrides, "backup", _failed_run(local_now, "20260717T090000"))

    jobs_dismissed.dismiss("photos", "20260717T090000")

    assert [c["job_id"] for c in board.jobs_attention(now=local_now)] == ["backup"]


def test_a_stuck_run_cannot_be_dismissed_away(webapp_client):
    _client, _app, overrides = webapp_client
    local_now = datetime.now()
    run_id = "20260717T090000"
    _seed_job(overrides, "wedged", {
        "run_id": run_id,
        "status": "running",
        "started_at": (local_now - timedelta(minutes=30)).isoformat(timespec="seconds"),
    })
    jobs_dismissed.dismiss("wedged", run_id)

    assert [(c["job_id"], c["state"]) for c in board.jobs_attention()] == [("wedged", "stuck")]


# ------------------------------------------------------------------ route


def test_dismiss_route_records_the_run_and_the_card_goes(webapp_client):
    client, _app, overrides = webapp_client
    local_now = datetime.now()
    _seed_job(overrides, "photos", _failed_run(local_now, "20260717T090000"))
    # "Finished today" is judged on the real clock here: a run finished a
    # moment ago is today's whatever time the test runs.
    run_json = overrides["tmp_jobs_runs_dir"] / "photos" / "20260717T090000" / "run.json"
    record = json.loads(run_json.read_text(encoding="utf-8"))
    record["finished_at"] = local_now.isoformat(timespec="seconds")
    run_json.write_text(json.dumps(record), encoding="utf-8")
    assert len(board.jobs_attention()) == 1

    resp = client.post("/api/jobs/photos/runs/20260717T090000/dismiss")

    assert resp.status_code == 200
    assert resp.json() == {
        "dismissed": True, "job_id": "photos", "run_id": "20260717T090000",
    }
    assert jobs_dismissed.read_dismissed() == {"photos": ["20260717T090000"]}
    assert board.jobs_attention() == []


def test_dismiss_route_refuses_what_is_not_a_finished_failure(webapp_client):
    client, _app, overrides = webapp_client
    local_now = datetime.now()
    _seed_job(overrides, "running-job", {
        "run_id": "20260717T090000",
        "status": "running",
        "started_at": local_now.isoformat(timespec="seconds"),
    })
    _seed_job(overrides, "fine-job", {
        "run_id": "20260717T090000",
        "status": "success",
        "finished_at": local_now.isoformat(timespec="seconds"),
        "exit_code": 0,
    })

    assert client.post("/api/jobs/running-job/runs/20260717T090000/dismiss").status_code == 409
    assert client.post("/api/jobs/fine-job/runs/20260717T090000/dismiss").status_code == 409
    assert client.post("/api/jobs/nope/runs/20260717T090000/dismiss").status_code == 404
    assert client.post("/api/jobs/fine-job/runs/no-such-run/dismiss").status_code == 404
    assert jobs_dismissed.read_dismissed() == {}
