"""`elevated` job flag (issue #350).

An elevated job materialises its scheduled Task Scheduler entry with
`/RL HIGHEST` so it fires with silent elevation (no interactive UAC prompt),
for a script whose target needs admin rights to do its work. These tests
cover the schema round-trip and the schtasks `/Create` argv.
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


class TestSyncSchtasksHonoursElevated:
    def test_elevated_job_create_includes_rl_highest(self):
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
            if argv[:2] == ["schtasks", "/Query"]:
                return _mk_completed(stdout="", rc=0)
            return _mk_completed(rc=0)

        created = jobs_mod.sync_schtasks(job, runner=runner)
        assert created == ["\\AppLauncher\\hwinfo"]
        create = next(c for c in calls if c[:2] == ["schtasks", "/Create"])
        assert "/RL" in create
        assert create[create.index("/RL") + 1] == "HIGHEST"

    def test_default_job_create_omits_rl(self):
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

        jobs_mod.sync_schtasks(job, runner=runner)
        create = next(c for c in calls if c[:2] == ["schtasks", "/Create"])
        assert "/RL" not in create

    def test_elevated_create_failure_logs_actionable_hint(self, caplog):
        job = Job(
            id="hwinfo",
            name="HWiNFO restart",
            script_path="C:\\stub\\hwinfo_restart.py",
            schedule=Schedule(type="hourly", every=8),
            elevated=True,
        )

        def runner(argv):
            if argv[:2] == ["schtasks", "/Query"]:
                return _mk_completed(stdout="", rc=0)
            if argv[:2] == ["schtasks", "/Create"]:
                return _mk_completed(rc=1, stdout="")
            return _mk_completed(rc=0)

        with caplog.at_level(logging.WARNING):
            created = jobs_mod.sync_schtasks(job, runner=runner)
        assert created == []
        assert any("already be elevated" in r.message for r in caplog.records)
