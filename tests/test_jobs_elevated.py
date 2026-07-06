"""`elevated` job flag (issue #350) and the Task Scheduler skip (issue #352).

An elevated job's real Task Scheduler entry needs ``/RL HIGHEST``, which can
only be created by an already-elevated caller — this webapp process never is.
So `sync_schtasks()` treats an elevated job's schedule entry as
externally-managed and never touches Task Scheduler for it at all; the entry
must be registered/updated by hand from an elevated shell. These tests cover
the schema round-trip and the skip behaviour.
"""

from __future__ import annotations

import logging
import subprocess
from typing import List

from src import jobs as jobs_mod
from src import jobs_config as jc
from src.jobs_config import (
    Job,
    JobsConfig,
    Schedule,
    job_from_dict,
    save_jobs,
    update_job,
)


def _mk_completed(stdout: str = "", rc: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr="")


class TestElevatedRoundTrip:
    def test_default_is_false_and_omitted(self):
        job = Job(id="j", name="J", script_path="C:\\x\\s.py")
        assert job.elevated is False
        assert "elevated" not in job.to_dict()

    def test_true_emitted_and_parsed(self):
        job = Job(id="j", name="J", script_path="C:\\x\\s.py", elevated=True)
        payload = job.to_dict()
        assert payload["elevated"] is True
        assert job_from_dict(payload).elevated is True

    def test_from_dict_defaults_false(self):
        job = job_from_dict(
            {"id": "j", "name": "J", "script_path": "C:\\x\\s.py"}
        )
        assert job.elevated is False

    def test_update_job_toggles(self, tmp_path, monkeypatch):
        monkeypatch.setattr(jc, "DEFAULT_JOBS_PATH", tmp_path / "jobs.json")
        cfg = JobsConfig(jobs=[Job(id="j", name="J", script_path="C:\\x\\s.py")])
        save_jobs(cfg)
        update_job(cfg, "j", elevated=True)
        assert jc.get_by_id(cfg, "j").elevated is True
        update_job(cfg, "j", elevated=False)
        assert jc.get_by_id(cfg, "j").elevated is False


class TestSyncSchtasksSkipsElevated:
    """Issue #352: sync_schtasks() must never touch Task Scheduler for an
    elevated job — the delete-then-recreate pattern silently strands the job
    (delete needs no elevation, recreate does) on every edit/pause/resume."""

    def test_elevated_job_never_calls_runner(self):
        job = Job(
            id="hwinfo",
            name="HWiNFO restart",
            script_path="C:\\stub\\hwinfo_restart.py",
            schedule=Schedule(type="hourly", every=8),
            elevated=True,
        )
        calls: List[List[str]] = []

        def runner(argv):
            calls.append(argv)
            return _mk_completed(rc=0)

        created = jobs_mod.sync_schtasks(job, runner=runner)
        assert created == []
        assert calls == []

    def test_elevated_job_skipped_even_when_paused(self):
        # pause() parks the schedule as "none" before calling sync_schtasks —
        # the elevated skip must win regardless of the schedule shape.
        job = Job(
            id="hwinfo",
            name="HWiNFO restart",
            script_path="C:\\stub\\hwinfo_restart.py",
            schedule=Schedule(type="none"),
            elevated=True,
        )
        calls: List[List[str]] = []

        def runner(argv):
            calls.append(argv)
            return _mk_completed(rc=0)

        created = jobs_mod.sync_schtasks(job, runner=runner)
        assert created == []
        assert calls == []

    def test_default_job_still_syncs_normally(self):
        job = Job(
            id="plain",
            name="Plain",
            script_path="C:\\stub\\demo.py",
            schedule=Schedule(type="daily", at="06:00"),
        )
        calls: List[List[str]] = []

        def runner(argv):
            calls.append(argv)
            if argv[:2] == ["schtasks", "/Query"]:
                return _mk_completed(stdout="", rc=0)
            return _mk_completed(rc=0)

        created = jobs_mod.sync_schtasks(job, runner=runner)
        assert created == ["\\AppLauncher\\plain"]
        assert any(c[:2] == ["schtasks", "/Create"] for c in calls)
