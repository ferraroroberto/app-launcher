"""Unit tests for :mod:`src.jobs` operational-signal helpers (issue #66).

Covers ``run_stats`` math, ``is_stuck`` heuristic, and
``consecutive_failed_runs`` — all derived from on-disk ``run.json``
files, no schtasks. Each test seeds a temp ``JOBS_RUNS_DIR`` so runs
are isolated.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from src import jobs as jobs_mod


@pytest.fixture
def temp_runs_dir(tmp_path, monkeypatch):
    """Redirect JOBS_RUNS_DIR + flush the per-job stats cache."""
    monkeypatch.setattr(jobs_mod, "JOBS_RUNS_DIR", tmp_path)
    jobs_mod.invalidate_stats_cache()
    yield tmp_path


def _seed_run(
    job_id: str,
    *,
    run_id: str,
    status: str,
    started_at: datetime,
    duration_seconds: float | None = None,
):
    """Write a run.json with consistent timestamps."""
    rd = jobs_mod.new_run_dir(job_id, run_id)
    finished_at = (
        started_at + timedelta(seconds=duration_seconds or 0.0)
        if status in {"success", "failed"} and duration_seconds is not None
        else None
    )
    fields = {
        "run_id": run_id,
        "status": status,
        "started_at": started_at.isoformat(timespec="seconds"),
    }
    if finished_at is not None:
        fields["finished_at"] = finished_at.isoformat(timespec="seconds")
    if duration_seconds is not None:
        fields["duration_seconds"] = duration_seconds
    jobs_mod.write_run_json(rd, **fields)
    return rd


# ============================================================== run_stats


class TestRunStats:
    def test_empty_when_no_runs(self, temp_runs_dir):
        stats = jobs_mod.run_stats("demo", fresh=True)
        assert stats["p50"] is None
        assert stats["p95"] is None
        assert stats["success_rate_30d"] is None
        assert stats["completed_count"] == 0
        assert stats["last7"] == []

    def test_p50_p95_over_completed(self, temp_runs_dir):
        # Durations: 1, 2, 3, 4, 100s → p50 ≈ 3, p95 ≈ 100.
        now = datetime.now()
        for i, d in enumerate([1.0, 2.0, 3.0, 4.0, 100.0]):
            _seed_run(
                "demo",
                run_id=f"2026010{i+1}T060000",
                status="success",
                started_at=now - timedelta(days=10 - i),
                duration_seconds=d,
            )
        stats = jobs_mod.run_stats("demo", fresh=True)
        assert stats["completed_count"] == 5
        assert stats["p50"] == 3.0
        assert stats["p95"] == 100.0

    def test_success_rate_only_counts_30d_window(self, temp_runs_dir):
        now = datetime.now()
        # 2 success + 1 fail within 30 d → 2/3.
        _seed_run("demo", run_id="20260101T060000", status="success",
                  started_at=now - timedelta(days=5), duration_seconds=1.0)
        _seed_run("demo", run_id="20260102T060000", status="success",
                  started_at=now - timedelta(days=10), duration_seconds=1.0)
        _seed_run("demo", run_id="20260103T060000", status="failed",
                  started_at=now - timedelta(days=15), duration_seconds=1.0)
        # 1 success outside the 30-d window → ignored.
        _seed_run("demo", run_id="20251201T060000", status="success",
                  started_at=now - timedelta(days=60), duration_seconds=1.0)
        stats = jobs_mod.run_stats("demo", fresh=True)
        assert stats["success_rate_30d"] == pytest.approx(2 / 3)

    def test_last7_is_oldest_left(self, temp_runs_dir):
        # Seed 10 runs — newest first on disk, but last7 should be the
        # last 7 oldest-first for left-to-right sparkline reading.
        now = datetime.now()
        statuses = ["success"] * 6 + ["failed"] * 4
        for i, status in enumerate(statuses):
            # Sortable id with a clean 2-digit ordinal so list_runs's
            # name-based sort matches insertion order.
            _seed_run(
                "demo",
                run_id=f"20260101T{i:02d}0000",
                status=status,
                started_at=now - timedelta(days=10 - i),
                duration_seconds=1.0,
            )
        # We seeded run_ids in ascending order, list_runs returns
        # newest-first. last7 reverses that → oldest-left.
        stats = jobs_mod.run_stats("demo", fresh=True)
        assert len(stats["last7"]) == 7
        statuses_in_spark = [r["status"] for r in stats["last7"]]
        run_ids_in_spark = [r["run_id"] for r in stats["last7"]]
        # Leftmost is older than rightmost — verify ordering by run_id.
        assert run_ids_in_spark == sorted(run_ids_in_spark)
        # 4 failed are at the right (newest).
        assert statuses_in_spark.count("failed") == 4
        assert statuses_in_spark[-1] == "failed"

    def test_running_not_counted_in_p_metrics(self, temp_runs_dir):
        now = datetime.now()
        _seed_run("demo", run_id="20260101T060000", status="success",
                  started_at=now - timedelta(days=1), duration_seconds=2.0)
        _seed_run("demo", run_id="20260102T060000", status="running",
                  started_at=now)
        stats = jobs_mod.run_stats("demo", fresh=True)
        assert stats["completed_count"] == 1
        assert stats["p50"] == 2.0


# ================================================================ is_stuck


class TestIsStuck:
    def test_false_when_no_runs(self, temp_runs_dir):
        assert jobs_mod.is_stuck("demo") is False

    def test_false_when_latest_is_finished(self, temp_runs_dir):
        now = datetime.now()
        _seed_run("demo", run_id="20260101T060000", status="success",
                  started_at=now - timedelta(seconds=10), duration_seconds=2.0)
        assert jobs_mod.is_stuck("demo") is False

    def test_floor_threshold_kicks_in_with_no_p95(self, temp_runs_dir):
        # No completed runs → p95 is None → floor (300 s) applies.
        now = datetime.now()
        _seed_run("demo", run_id="20260101T060000", status="running",
                  started_at=now - timedelta(seconds=100))
        assert jobs_mod.is_stuck("demo", floor_seconds=300.0) is False

    def test_floor_violation_marks_stuck(self, temp_runs_dir):
        now = datetime.now()
        _seed_run("demo", run_id="20260101T060000", status="running",
                  started_at=now - timedelta(seconds=400))
        assert (
            jobs_mod.is_stuck("demo", p95_factor=3.0, floor_seconds=300.0)
            is True
        )

    def test_p95_factor_threshold(self, temp_runs_dir):
        now = datetime.now()
        # p95 = 10 s × factor 3 = 30 s; floor of 1 s lets the factor win.
        for i in range(5):
            _seed_run(
                "demo",
                run_id=f"2026010{i+1}T060000",
                status="success",
                started_at=now - timedelta(days=5 - i),
                duration_seconds=10.0,
            )
        _seed_run("demo", run_id="20260106T060000", status="running",
                  started_at=now - timedelta(seconds=40))
        assert (
            jobs_mod.is_stuck("demo", p95_factor=3.0, floor_seconds=1.0)
            is True
        )
        # 25 s elapsed < 30 s threshold → not stuck.
        _seed_run("demo", run_id="20260107T060000", status="running",
                  started_at=now - timedelta(seconds=25))
        # Re-seed: newest run is the 25 s one.
        assert (
            jobs_mod.is_stuck("demo", p95_factor=3.0, floor_seconds=1.0)
            is False
        )


# ============================================== consecutive_failed_runs


class TestConsecutiveFailedRuns:
    def test_zero_when_no_runs(self, temp_runs_dir):
        assert jobs_mod.consecutive_failed_runs("demo") == 0

    def test_zero_when_newest_is_success(self, temp_runs_dir):
        now = datetime.now()
        _seed_run("demo", run_id="20260101T060000", status="failed",
                  started_at=now - timedelta(days=2), duration_seconds=1.0)
        _seed_run("demo", run_id="20260102T060000", status="success",
                  started_at=now - timedelta(days=1), duration_seconds=1.0)
        assert jobs_mod.consecutive_failed_runs("demo") == 0

    def test_counts_prefix(self, temp_runs_dir):
        now = datetime.now()
        _seed_run("demo", run_id="20260101T060000", status="success",
                  started_at=now - timedelta(days=4), duration_seconds=1.0)
        _seed_run("demo", run_id="20260102T060000", status="failed",
                  started_at=now - timedelta(days=3), duration_seconds=1.0)
        _seed_run("demo", run_id="20260103T060000", status="failed",
                  started_at=now - timedelta(days=2), duration_seconds=1.0)
        _seed_run("demo", run_id="20260104T060000", status="failed",
                  started_at=now - timedelta(days=1), duration_seconds=1.0)
        assert jobs_mod.consecutive_failed_runs("demo") == 3


# ============================================ bulk-cache parser sanity


class TestBulkParser:
    def test_filters_by_prefix(self):
        stdout = (
            "TaskName: \\AppLauncher\\demo\nNext Run Time: 2026-06-01 06:00:00\n\n"
            "TaskName: \\Foreign\\task\nNext Run Time: 2026-06-01 12:00:00\n\n"
        )
        out = jobs_mod._parse_bulk_query(stdout)
        assert "\\AppLauncher\\demo" in out
        assert out["\\AppLauncher\\demo"] == "2026-06-01 06:00:00"
        # Foreign task is filtered out.
        assert "\\Foreign\\task" not in out

    def test_n_a_collapses_to_none(self):
        stdout = (
            "TaskName: \\AppLauncher\\demo-1\nNext Run Time: N/A\n\n"
            "TaskName: \\AppLauncher\\demo-2\nNext Run Time: Disabled\n\n"
        )
        out = jobs_mod._parse_bulk_query(stdout)
        assert out["\\AppLauncher\\demo-1"] is None
        assert out["\\AppLauncher\\demo-2"] is None
