"""Stranded-run reconciliation (issue #591).

A run record is written ``status: "running"`` before the child spawns and
only reaches a terminal status once the executor's own finalise runs after
``proc.wait()``. If the executor itself dies first, nothing ever finalises
the record and it stays "running" forever even though the tracked pid is
long gone. ``src.jobs_reap`` automates the same reconciliation the webapp's
kill route already does by hand for an orphan pid.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src import diagnostics
from src import jobs as jobs_mod
from src import jobs_history as jobs_history_mod
from src import jobs_queue as jobs_queue_mod
from src import jobs_reap as jobs_reap_mod
from src.jobs_config import Job


# --------------------------------------------------------------- fixtures


@pytest.fixture
def isolated_jobs(tmp_path, monkeypatch):
    # JOBS_RUNS_DIR / JOBS_QUEUE_PATH are owned by src.jobs_history /
    # src.jobs_queue respectively (issue #315 split) — patch there, not on
    # the src.jobs facade, mirroring tests/test_run_job_mutex.py.
    monkeypatch.setattr(jobs_history_mod, "JOBS_RUNS_DIR", tmp_path)
    monkeypatch.setattr(jobs_queue_mod, "JOBS_QUEUE_PATH", tmp_path / "_queue.json")
    return tmp_path


def _seed_run(runs_root, job_id, run_id, **fields):
    rd = runs_root / job_id / run_id
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "run.json").write_text(
        json.dumps({"run_id": run_id, "job_id": job_id, **fields}),
        encoding="utf-8",
    )
    return rd


# ------------------------------------------------------- diagnostics.is_pid_alive


class _FakeProc:
    def __init__(self, running=True, create_time=1000.0, deny_create_time=False):
        self._running = running
        self._create_time = create_time
        self._deny_create_time = deny_create_time

    def is_running(self):
        return self._running

    def create_time(self):
        if self._deny_create_time:
            raise diagnostics.psutil.AccessDenied()
        return self._create_time


class _FakePsutil:
    """Minimal stand-in exposing just what is_pid_alive touches."""

    def __init__(self, exists=True, proc=None):
        self._exists = exists
        self._proc = proc or _FakeProc()
        # Real exception classes so isinstance/except in the code under
        # test still work against this fake.
        self.NoSuchProcess = diagnostics.psutil.NoSuchProcess if diagnostics.psutil else Exception
        self.AccessDenied = diagnostics.psutil.AccessDenied if diagnostics.psutil else Exception

    def pid_exists(self, pid):
        return self._exists

    def Process(self, pid):
        if not self._exists:
            raise self.NoSuchProcess(pid)
        return self._proc


class TestIsPidAlive:
    def test_dead_pid_is_not_alive(self, monkeypatch):
        monkeypatch.setattr(diagnostics, "psutil", _FakePsutil(exists=False))
        assert diagnostics.is_pid_alive(4242) is False

    def test_live_pid_no_hint_trusted_as_alive(self, monkeypatch):
        monkeypatch.setattr(diagnostics, "psutil", _FakePsutil(exists=True))
        assert diagnostics.is_pid_alive(4242) is True

    def test_live_pid_matching_create_time_is_alive(self, monkeypatch):
        proc = _FakeProc(create_time=1000.0)
        monkeypatch.setattr(diagnostics, "psutil", _FakePsutil(proc=proc))
        assert diagnostics.is_pid_alive(4242, create_time_hint=1000.3) is True

    def test_live_pid_mismatched_create_time_is_reused_pid(self, monkeypatch):
        # Windows recycled the pid — the live process is NOT the one we
        # spawned, so it must be treated as dead.
        proc = _FakeProc(create_time=5000.0)
        monkeypatch.setattr(diagnostics, "psutil", _FakePsutil(proc=proc))
        assert diagnostics.is_pid_alive(4242, create_time_hint=1000.0) is False

    def test_not_running_process_is_not_alive(self, monkeypatch):
        proc = _FakeProc(running=False)
        monkeypatch.setattr(diagnostics, "psutil", _FakePsutil(proc=proc))
        assert diagnostics.is_pid_alive(4242) is False

    def test_no_psutil_assumes_alive(self, monkeypatch):
        monkeypatch.setattr(diagnostics, "psutil", None)
        assert diagnostics.is_pid_alive(4242, create_time_hint=1.0) is True

    def test_create_time_query_denied_trusts_alive(self, monkeypatch):
        # Process exists and is running but create_time() is unreadable
        # (e.g. a privileged process) — keep it rather than guess.
        proc = _FakeProc(deny_create_time=True)
        monkeypatch.setattr(diagnostics, "psutil", _FakePsutil(proc=proc))
        assert diagnostics.is_pid_alive(4242, create_time_hint=1000.0) is True


# -------------------------------------------------------------- finalize_dead_runs


class TestFinalizeDeadRuns:
    def test_noop_when_no_pid_recorded(self, isolated_jobs, monkeypatch):
        # The tiny window between the "running" record landing and the
        # child's pid being persisted — must not be mistaken for stranded.
        _seed_run(
            isolated_jobs, "solo", "20260101T000000",
            status="running",
            started_at=datetime.now().isoformat(timespec="seconds"),
        )
        job = Job(id="solo", name="Solo", script_path="C:/nowhere/x.py")
        assert jobs_reap_mod.finalize_dead_runs(job) == []
        record = jobs_mod.read_run(isolated_jobs / "solo" / "20260101T000000")
        assert record["status"] == "running"

    def test_noop_when_status_already_terminal(self, isolated_jobs, monkeypatch):
        _seed_run(
            isolated_jobs, "solo", "20260101T000000",
            status="success", pid=4242,
            started_at=datetime.now().isoformat(timespec="seconds"),
        )
        job = Job(id="solo", name="Solo", script_path="C:/nowhere/x.py")
        assert jobs_reap_mod.finalize_dead_runs(job) == []

    def test_noop_when_pid_alive(self, isolated_jobs, monkeypatch):
        _seed_run(
            isolated_jobs, "solo", "20260101T000000",
            status="running", pid=4242,
            started_at=datetime.now().isoformat(timespec="seconds"),
        )
        monkeypatch.setattr(jobs_reap_mod, "is_pid_alive", lambda *a, **k: True)
        job = Job(id="solo", name="Solo", script_path="C:/nowhere/x.py")
        assert jobs_reap_mod.finalize_dead_runs(job) == []
        record = jobs_mod.read_run(isolated_jobs / "solo" / "20260101T000000")
        assert record["status"] == "running"

    def test_reaps_a_genuinely_dead_pid_with_output_log_evidence(
        self, isolated_jobs, monkeypatch
    ):
        """output.log's mtime is the real end-time evidence — never the
        moment reaping happens to run (issue #747)."""
        started = (datetime.now() - timedelta(minutes=5)).isoformat(timespec="seconds")
        rd = _seed_run(
            isolated_jobs, "solo", "20260101T000000",
            status="running", pid=4242, started_at=started,
        )
        (rd / "output.log").write_text("job output\n", encoding="utf-8")
        monkeypatch.setattr(jobs_reap_mod, "is_pid_alive", lambda *a, **k: False)
        job = Job(id="solo", name="Solo", script_path="C:/nowhere/x.py")

        reaped = jobs_reap_mod.finalize_dead_runs(job)

        assert len(reaped) == 1
        record = reaped[0]
        assert record["status"] == "failed"
        assert record["reaped"] is True
        assert record.get("finished_at")
        assert record["duration_seconds"] > 0
        assert "end_time_unknown" not in record
        assert "killed" not in record  # distinct from an explicit user kill
        # Persisted, not just returned.
        on_disk = jobs_mod.read_run(isolated_jobs / "solo" / "20260101T000000")
        assert on_disk["status"] == "failed"

    def test_reaps_with_no_output_log_marks_end_time_unknown(
        self, isolated_jobs, monkeypatch
    ):
        """No output.log at all → no evidence of a real end time. The
        launcher must never fabricate one — record the fact as its own
        state instead (issue #747)."""
        started = (datetime.now() - timedelta(minutes=5)).isoformat(timespec="seconds")
        _seed_run(
            isolated_jobs, "solo", "20260101T000000",
            status="running", pid=4242, started_at=started,
        )
        monkeypatch.setattr(jobs_reap_mod, "is_pid_alive", lambda *a, **k: False)
        job = Job(id="solo", name="Solo", script_path="C:/nowhere/x.py")

        reaped = jobs_reap_mod.finalize_dead_runs(job)

        assert len(reaped) == 1
        record = reaped[0]
        assert record["status"] == "failed"
        assert record["reaped"] is True
        assert record["end_time_unknown"] is True
        assert "finished_at" not in record
        assert "duration_seconds" not in record

    def test_reaps_when_output_log_predates_started_at_marks_unknown(
        self, isolated_jobs, monkeypatch
    ):
        """A stale/leftover output.log older than this run's own start is
        not evidence of anything — treat it the same as no evidence."""
        started = datetime.now().isoformat(timespec="seconds")
        rd = _seed_run(
            isolated_jobs, "solo", "20260101T000000",
            status="running", pid=4242, started_at=started,
        )
        stale_log = rd / "output.log"
        stale_log.write_text("leftover\n", encoding="utf-8")
        old_time = (datetime.now() - timedelta(hours=1)).timestamp()
        os.utime(stale_log, (old_time, old_time))
        monkeypatch.setattr(jobs_reap_mod, "is_pid_alive", lambda *a, **k: False)
        job = Job(id="solo", name="Solo", script_path="C:/nowhere/x.py")

        reaped = jobs_reap_mod.finalize_dead_runs(job)

        assert reaped[0]["end_time_unknown"] is True
        assert "finished_at" not in reaped[0]

    def test_reap_fires_the_same_failure_alert_a_normal_finalise_would(
        self, isolated_jobs, monkeypatch
    ):
        """Issue #747: nobody was told for 14 hours because the reap path
        bypassed the executor's notification hook entirely — it must fire
        the same alert channels a live-detected failure would."""
        started = (datetime.now() - timedelta(minutes=5)).isoformat(timespec="seconds")
        _seed_run(
            isolated_jobs, "solo", "20260101T000000",
            status="running", pid=4242, started_at=started,
        )
        monkeypatch.setattr(jobs_reap_mod, "is_pid_alive", lambda *a, **k: False)
        spy = MagicMock()
        monkeypatch.setattr(jobs_reap_mod, "notify_failure", spy)
        job = Job(id="solo", name="Solo", script_path="C:/nowhere/x.py")

        jobs_reap_mod.finalize_dead_runs(job)

        spy.assert_called_once()
        args, kwargs = spy.call_args
        assert args[1] is job
        assert kwargs["status"] == "failed"
        assert kwargs["reaped"] is True
        assert kwargs["exit_code"] is None

    def test_reaps_legacy_record_with_no_create_time_hint_when_pid_dead(
        self, isolated_jobs, monkeypatch
    ):
        """The literal issue #591 example: an old record with just a bare
        dead pid and no pid_create_time. is_pid_alive's pid_exists()==False
        path doesn't need the hint at all."""
        _seed_run(
            isolated_jobs, "reporting-daily", "20260725T091929",
            status="running", pid=32772,
            started_at=datetime.now().isoformat(timespec="seconds"),
        )
        monkeypatch.setattr(diagnostics, "psutil", _FakePsutil(exists=False))
        job = Job(id="reporting-daily", name="Reporting", script_path="C:/nowhere/x.py")

        reaped = jobs_reap_mod.finalize_dead_runs(job)
        assert len(reaped) == 1
        assert reaped[0]["status"] == "failed"
        assert reaped[0]["reaped"] is True

    def test_reaps_a_superseded_historical_record_not_just_the_latest(
        self, isolated_jobs, monkeypatch
    ):
        """The literal issue #591 scenario: reporting-daily's stranded
        "running" record (09:19) was superseded by a newer completed run
        (09:31) by the time this was fixed — latest_run() no longer sees
        it, but it must still be resolved (acceptance criteria), not left
        showing "running" forever in its own run detail view."""
        now = datetime.now()
        _seed_run(
            isolated_jobs, "reporting-daily", "20260725T091929",
            status="running", pid=32772,
            started_at=(now - timedelta(hours=1)).isoformat(timespec="seconds"),
        )
        _seed_run(
            isolated_jobs, "reporting-daily", "20260725T093142",
            status="failed", pid=50876, exit_code=1,
            started_at=(now - timedelta(minutes=40)).isoformat(timespec="seconds"),
            finished_at=(now - timedelta(minutes=38)).isoformat(timespec="seconds"),
        )
        monkeypatch.setattr(jobs_reap_mod, "is_pid_alive", lambda *a, **k: False)
        job = Job(id="reporting-daily", name="Reporting", script_path="C:/nowhere/x.py")

        # latest_run alone would miss the older stranded record entirely.
        assert jobs_mod.latest_run("reporting-daily")["run_id"] == "20260725T093142"

        reaped = jobs_reap_mod.finalize_dead_runs(job)

        assert [r["run_id"] for r in reaped] == ["20260725T091929"]
        old = jobs_mod.read_run(isolated_jobs / "reporting-daily" / "20260725T091929")
        assert old["status"] == "failed"
        assert old["reaped"] is True
        # The newer, already-terminal record is untouched.
        newer = jobs_mod.read_run(isolated_jobs / "reporting-daily" / "20260725T093142")
        assert newer["exit_code"] == 1
        assert "reaped" not in newer

    def test_passes_pid_create_time_through_as_the_liveness_hint(
        self, isolated_jobs, monkeypatch
    ):
        _seed_run(
            isolated_jobs, "solo", "20260101T000000",
            status="running", pid=4242, pid_create_time=1234.5,
            started_at=datetime.now().isoformat(timespec="seconds"),
        )
        spy = MagicMock(return_value=False)
        monkeypatch.setattr(jobs_reap_mod, "is_pid_alive", spy)
        job = Job(id="solo", name="Solo", script_path="C:/nowhere/x.py")
        jobs_reap_mod.finalize_dead_runs(job)
        spy.assert_called_once_with(4242, 1234.5)

    def test_does_not_drain_mutex_queue(self, isolated_jobs, monkeypatch):
        """finalize_dead_runs is the drain-less half — used by
        mutex_collision, which must not spawn a sibling mid-sweep off
        stale collision data."""
        _seed_run(
            isolated_jobs, "solo", "20260101T000000",
            status="running", pid=4242, mutex_group="chrome",
            started_at=datetime.now().isoformat(timespec="seconds"),
        )
        jobs_mod.enqueue_mutex("chrome", {
            "job_id": "sibling", "run_id": "20260101T000010", "trigger": "manual",
        })
        monkeypatch.setattr(jobs_reap_mod, "is_pid_alive", lambda *a, **k: False)
        job = Job(
            id="solo", name="Solo", script_path="C:/nowhere/x.py",
            mutex_group="chrome",
        )
        jobs_reap_mod.finalize_dead_runs(job)
        assert jobs_mod.peek_mutex_queue("chrome") == [
            {"job_id": "sibling", "run_id": "20260101T000010", "trigger": "manual"}
        ]


# -------------------------------------------------------------- reap_stranded_runs


class TestReapStrandedRuns:
    def test_drains_mutex_group_after_finalising(self, isolated_jobs, monkeypatch):
        _seed_run(
            isolated_jobs, "solo", "20260101T000000",
            status="running", pid=4242, mutex_group="chrome",
            started_at=datetime.now().isoformat(timespec="seconds"),
        )
        _seed_run(
            isolated_jobs, "sibling", "20260101T000010",
            status="queued",
            started_at=datetime.now().isoformat(timespec="seconds"),
        )
        jobs_mod.enqueue_mutex("chrome", {
            "job_id": "sibling", "run_id": "20260101T000010", "trigger": "manual",
        })
        monkeypatch.setattr(jobs_reap_mod, "is_pid_alive", lambda *a, **k: False)
        spawn = MagicMock(return_value=99999)
        monkeypatch.setattr(jobs_queue_mod, "spawn_run_job_detached", spawn)
        job = Job(
            id="solo", name="Solo", script_path="C:/nowhere/x.py",
            mutex_group="chrome",
        )

        reaped = jobs_reap_mod.reap_stranded_runs(job)

        assert len(reaped) == 1
        assert reaped[0]["status"] == "failed"
        # The queue drained — the sibling was spawned and popped.
        assert spawn.called
        assert spawn.call_args.args[0] == "sibling"
        assert jobs_mod.peek_mutex_queue("chrome") == []

    def test_no_mutex_group_skips_drain_cleanly(self, isolated_jobs, monkeypatch):
        _seed_run(
            isolated_jobs, "solo", "20260101T000000",
            status="running", pid=4242,
            started_at=datetime.now().isoformat(timespec="seconds"),
        )
        monkeypatch.setattr(jobs_reap_mod, "is_pid_alive", lambda *a, **k: False)
        job = Job(id="solo", name="Solo", script_path="C:/nowhere/x.py")
        reaped = jobs_reap_mod.reap_stranded_runs(job)
        assert len(reaped) == 1
        assert reaped[0]["status"] == "failed"

    def test_nothing_reaped_skips_drain(self, isolated_jobs, monkeypatch):
        """No stranded record → no drain call at all, even with a queued
        sibling sitting there (nothing changed, so nothing to trigger on)."""
        jobs_mod.enqueue_mutex("chrome", {
            "job_id": "sibling", "run_id": "20260101T000010", "trigger": "manual",
        })
        spawn = MagicMock()
        monkeypatch.setattr(jobs_queue_mod, "spawn_run_job_detached", spawn)
        job = Job(
            id="solo", name="Solo", script_path="C:/nowhere/x.py",
            mutex_group="chrome",
        )
        reaped = jobs_reap_mod.reap_stranded_runs(job)
        assert reaped == []
        assert not spawn.called
        assert len(jobs_mod.peek_mutex_queue("chrome")) == 1


# --------------------------------------------- downstream consumers see the reap


class TestDownstreamConsumersAfterReap:
    def test_consecutive_failed_runs_resumes_past_reconciled_record(
        self, isolated_jobs, monkeypatch
    ):
        now = datetime.now()
        _seed_run(
            isolated_jobs, "flaky", "20260101T000300",
            status="running", pid=4242,
            started_at=(now - timedelta(minutes=1)).isoformat(timespec="seconds"),
        )
        _seed_run(
            isolated_jobs, "flaky", "20260101T000200",
            status="failed",
            started_at=(now - timedelta(minutes=2)).isoformat(timespec="seconds"),
        )
        _seed_run(
            isolated_jobs, "flaky", "20260101T000100",
            status="failed",
            started_at=(now - timedelta(minutes=3)).isoformat(timespec="seconds"),
        )
        monkeypatch.setattr(jobs_reap_mod, "is_pid_alive", lambda *a, **k: False)
        job = Job(id="flaky", name="Flaky", script_path="C:/nowhere/x.py")

        # Before reap: the stranded "running" head breaks the streak at 0.
        assert jobs_mod.consecutive_failed_runs("flaky") == 0

        jobs_reap_mod.finalize_dead_runs(job)

        # After reap: the (now-failed) head plus the two behind it.
        assert jobs_mod.consecutive_failed_runs("flaky") == 3

    def test_is_running_and_run_button_reenable_after_reap(
        self, isolated_jobs, monkeypatch
    ):
        _seed_run(
            isolated_jobs, "solo", "20260101T000000",
            status="running", pid=4242,
            started_at=datetime.now().isoformat(timespec="seconds"),
        )
        monkeypatch.setattr(jobs_reap_mod, "is_pid_alive", lambda *a, **k: False)
        job = Job(id="solo", name="Solo", script_path="C:/nowhere/x.py")

        assert jobs_mod.is_running("solo") is True
        jobs_reap_mod.finalize_dead_runs(job)
        assert jobs_mod.is_running("solo") is False


# ---------------------------------------------- mutex_collision self-heals (#591)


class TestMutexCollisionReconciles:
    def test_stranded_holder_no_longer_blocks_admission(self, isolated_jobs, monkeypatch):
        holder = Job(
            id="holder", name="Holder", script_path="C:/nowhere/a.py",
            mutex_group="chrome",
        )
        fresh = Job(
            id="fresh", name="Fresh", script_path="C:/nowhere/b.py",
            mutex_group="chrome",
        )
        _seed_run(
            isolated_jobs, "holder", "20260101T000000",
            status="running", pid=4242,
            started_at=datetime.now().isoformat(timespec="seconds"),
        )
        monkeypatch.setattr(jobs_reap_mod, "is_pid_alive", lambda *a, **k: False)

        blocker = jobs_mod.mutex_collision([holder, fresh], fresh)

        assert blocker is None
        record = jobs_mod.read_run(isolated_jobs / "holder" / "20260101T000000")
        assert record["status"] == "failed"

    def test_still_blocks_when_pid_is_alive(self, isolated_jobs, monkeypatch):
        holder = Job(
            id="holder", name="Holder", script_path="C:/nowhere/a.py",
            mutex_group="chrome",
        )
        fresh = Job(
            id="fresh", name="Fresh", script_path="C:/nowhere/b.py",
            mutex_group="chrome",
        )
        _seed_run(
            isolated_jobs, "holder", "20260101T000000",
            status="running", pid=4242,
            started_at=datetime.now().isoformat(timespec="seconds"),
        )
        monkeypatch.setattr(jobs_reap_mod, "is_pid_alive", lambda *a, **k: True)

        blocker = jobs_mod.mutex_collision([holder, fresh], fresh)
        assert blocker is holder

    def test_no_pid_recorded_still_blocks(self, isolated_jobs, monkeypatch):
        # A record with no pid yet can't be verified — mutex_collision must
        # keep treating it as a live holder, never guess.
        holder = Job(
            id="holder", name="Holder", script_path="C:/nowhere/a.py",
            mutex_group="chrome",
        )
        fresh = Job(
            id="fresh", name="Fresh", script_path="C:/nowhere/b.py",
            mutex_group="chrome",
        )
        _seed_run(
            isolated_jobs, "holder", "20260101T000000",
            status="running",
            started_at=datetime.now().isoformat(timespec="seconds"),
        )
        blocker = jobs_mod.mutex_collision([holder, fresh], fresh)
        assert blocker is holder
