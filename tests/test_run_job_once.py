"""Executor-side `once` schedule cleanup (issue #68 PR #4).

A `once` job that fires via Task Scheduler must have its schtasks entry
removed and its in-memory schedule flipped to ``none`` after the run
finalises — otherwise the row keeps showing "once <past>" forever.
Manual runs of a `once` job leave the schedule alone so a future
deferred fire still works.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.cli.commands import run_job_cmd as rjc
from src import jobs as jobs_mod
from src.app_config import AppConfig
from src.jobs_config import (
    Job,
    JobsConfig,
    Schedule,
    save_jobs,
)


@pytest.fixture
def isolated_jobs(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_mod, "JOBS_RUNS_DIR", tmp_path)
    monkeypatch.setattr(jobs_mod, "JOBS_QUEUE_PATH", tmp_path / "_queue.json")
    from src import jobs_config as jc
    monkeypatch.setattr(jc, "DEFAULT_JOBS_PATH", tmp_path / "jobs.json")
    return tmp_path


def _silence_notifier(monkeypatch):
    monkeypatch.setattr(
        rjc, "load_webapp_config",
        lambda: SimpleNamespace(
            notify_on_failure=False,
            notify_failure_summary=False,
            notify_failure_streak=0,
        ),
    )


class TestOnceCleanup:
    def test_scheduled_once_fire_clears_schedule(
        self, isolated_jobs, tmp_path, monkeypatch
    ):
        script = tmp_path / "ok.py"
        script.write_text("import sys; sys.exit(0)\n", encoding="utf-8")
        job = Job(
            id="oneoff", name="OneOff", script_path=str(script),
            schedule=Schedule(type="once", at="2026-06-01T14:30"),
        )
        cfg = JobsConfig(jobs=[job])
        save_jobs(cfg)
        _silence_notifier(monkeypatch)

        delete = MagicMock(return_value=["\\AppLauncher\\oneoff"])
        # The executor imports delete_schtasks at module top — patch
        # the rjc binding, not jobs_mod, so the executor sees the mock.
        monkeypatch.setattr(rjc, "delete_schtasks", delete)

        cmd = rjc.RunJobCommand(AppConfig())
        rc = cmd.execute(SimpleNamespace(
            job_id="oneoff", trigger="scheduled", run_id=None, params=None,
        ))
        assert rc == 0
        # Schtasks entry was removed.
        assert delete.called
        assert delete.call_args.args[0] == "oneoff"
        # And the on-disk job has schedule=none now.
        from src.jobs_config import load_jobs
        reloaded = load_jobs()
        survivor = next(j for j in reloaded.jobs if j.id == "oneoff")
        assert survivor.schedule.type == "none"

    def test_manual_once_fire_does_not_clear(
        self, isolated_jobs, tmp_path, monkeypatch
    ):
        """Manual fires of a `once` job leave the schedule intact so a
        deferred future fire still works."""
        script = tmp_path / "ok.py"
        script.write_text("import sys; sys.exit(0)\n", encoding="utf-8")
        job = Job(
            id="oneoff", name="OneOff", script_path=str(script),
            schedule=Schedule(type="once", at="2026-06-01T14:30"),
        )
        cfg = JobsConfig(jobs=[job])
        save_jobs(cfg)
        _silence_notifier(monkeypatch)

        delete = MagicMock(return_value=[])
        monkeypatch.setattr(rjc, "delete_schtasks", delete)

        cmd = rjc.RunJobCommand(AppConfig())
        rc = cmd.execute(SimpleNamespace(
            job_id="oneoff", trigger="manual", run_id=None, params=None,
        ))
        assert rc == 0
        assert not delete.called
        from src.jobs_config import load_jobs
        reloaded = load_jobs()
        survivor = next(j for j in reloaded.jobs if j.id == "oneoff")
        assert survivor.schedule.type == "once"
