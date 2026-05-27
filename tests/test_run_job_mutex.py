"""Executor-side mutex queue drain (issue #68 PR #2).

When a run finalises in a job carrying ``mutex_group``, the executor
pops the next queued sibling entry and spawns it detached. We mock the
spawn so no real subprocess runs and assert the spawn argv.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.cli.commands import run_job_cmd as rjc
from src import jobs as jobs_mod
from src.app_config import AppConfig
from src.jobs_config import Job, JobsConfig


@pytest.fixture
def isolated_jobs(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_mod, "JOBS_RUNS_DIR", tmp_path)
    monkeypatch.setattr(jobs_mod, "JOBS_QUEUE_PATH", tmp_path / "_queue.json")
    return tmp_path


def _seed_run(runs_root, job_id, run_id, *, started_at, status):
    rd = runs_root / job_id / run_id
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "run.json").write_text(
        json.dumps({
            "run_id": run_id,
            "job_id": job_id,
            "status": status,
            "started_at": started_at,
        }),
        encoding="utf-8",
    )
    return rd


def _silence_notifier(monkeypatch):
    monkeypatch.setattr(
        rjc, "load_webapp_config",
        lambda: SimpleNamespace(
            notify_on_failure=False,
            notify_failure_summary=False,
            notify_failure_streak=0,
        ),
    )


class TestExecutorMutexDrain:
    def test_finalising_executor_spawns_next_queued(
        self, isolated_jobs, tmp_path, monkeypatch
    ):
        """A successful run finalisation in mutex_group X pops the head
        queued entry for X and spawns the run via the detached spawn."""
        # A short python script that exits 0.
        script = tmp_path / "ok.py"
        script.write_text("import sys; sys.exit(0)\n", encoding="utf-8")
        job_a = Job(
            id="alpha", name="Alpha", script_path=str(script),
            mutex_group="chrome",
        )
        # Pre-seed the queue with a sibling entry (job beta, run rN).
        # The drainer expects the run dir to exist with status=queued.
        _seed_run(
            isolated_jobs, "beta", "20260101T000010",
            started_at=datetime.now().isoformat(timespec="seconds"),
            status="queued",
        )
        jobs_mod.enqueue_mutex("chrome", {
            "job_id": "beta",
            "run_id": "20260101T000010",
            "trigger": "manual",
            "params": None,
        })
        # Job registry — alpha is the one we're running, beta exists so
        # the route logic / drainer can find it; the drainer ultimately
        # spawns by id without consulting the registry, so this is just
        # for completeness.
        monkeypatch.setattr(
            "src.jobs_config.load_jobs",
            lambda: JobsConfig(jobs=[job_a]),
        )
        monkeypatch.setattr(rjc, "load_jobs", lambda: JobsConfig(jobs=[job_a]))
        _silence_notifier(monkeypatch)
        spawn = MagicMock(return_value=99999)
        monkeypatch.setattr(jobs_mod, "spawn_run_job_detached", spawn)

        cmd = rjc.RunJobCommand(AppConfig())
        rc = cmd.execute(SimpleNamespace(
            job_id="alpha", trigger="manual", run_id=None, params=None,
        ))
        assert rc == 0
        # Spawn was called for beta's queued run, not alpha's.
        assert spawn.called
        args = spawn.call_args.args
        assert args[0] == "beta"
        assert args[1] == "20260101T000010"
        # The queue is now empty.
        assert jobs_mod.peek_mutex_queue("chrome") == []

    def test_drain_skips_non_queued_entry(
        self, isolated_jobs, tmp_path, monkeypatch
    ):
        """If the head's run dir was already promoted (status=running or
        success) the drainer must NOT spawn — that's the double-spawn
        guard. The head is still popped (the queue moves forward)."""
        # The head's record is already 'running' (someone else picked it up).
        _seed_run(
            isolated_jobs, "beta", "20260101T000010",
            started_at=datetime.now().isoformat(timespec="seconds"),
            status="running",
        )
        jobs_mod.enqueue_mutex("chrome", {
            "job_id": "beta", "run_id": "20260101T000010",
            "trigger": "manual",
        })
        spawn = MagicMock()
        result = jobs_mod.drain_mutex_queue("chrome", spawn=spawn)
        assert result is None
        assert not spawn.called
        # Queue advanced past the malformed head (idempotency over
        # forever-stuck queues).
        assert jobs_mod.peek_mutex_queue("chrome") == []
