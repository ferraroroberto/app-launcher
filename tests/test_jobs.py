"""Unit tests for the Jobs tab plumbing (issue #47):

* :mod:`src.jobs_config` — schedule validation, job CRUD round-trips.
* :mod:`src.jobs`         — schtasks argv mapping, run-history I/O.
* :mod:`app.cli.commands.run_job_cmd` — venv walk-up + dispatch.

All schtasks calls go through a single ``runner`` callable, which these
tests mock — no real ``schtasks.exe`` is invoked.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import List
from unittest.mock import MagicMock

import pytest

from app.cli.commands.run_job_cmd import build_invocation, resolve_venv_python
from src import jobs as jobs_mod
from src.jobs_config import (
    Job,
    JobsConfig,
    Schedule,
    add_job,
    get_by_id,
    job_from_dict,
    load_jobs,
    make_job_id,
    remove_by_id,
    save_jobs,
    schedule_from_dict,
    update_job,
)


def _mk_completed(stdout: str = "", rc: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr="")


# =================================================================== schedule


class TestScheduleValidation:
    def test_none_default(self):
        assert schedule_from_dict(None).type == "none"
        assert schedule_from_dict({"type": "none"}).type == "none"

    def test_unknown_type_rejected(self):
        with pytest.raises(ValueError, match="unknown schedule"):
            schedule_from_dict({"type": "yearly"})

    def test_daily_requires_hhmm(self):
        with pytest.raises(ValueError, match="HH:MM"):
            schedule_from_dict({"type": "daily", "at": "6am"})
        ok = schedule_from_dict({"type": "daily", "at": "06:00"})
        assert ok.at == "06:00"

    def test_daily_times_non_empty_list(self):
        with pytest.raises(ValueError, match="non-empty"):
            schedule_from_dict({"type": "daily_times", "at": []})
        ok = schedule_from_dict({"type": "daily_times", "at": ["06:00", "12:00"]})
        assert ok.at == ["06:00", "12:00"]

    def test_daily_times_each_must_be_hhmm(self):
        with pytest.raises(ValueError, match="HH:MM"):
            schedule_from_dict({"type": "daily_times", "at": ["06:00", "noon"]})

    def test_minutes_every_must_be_positive_int(self):
        with pytest.raises(ValueError, match="every"):
            schedule_from_dict({"type": "minutes", "every": 0})
        with pytest.raises(ValueError, match="every"):
            schedule_from_dict({"type": "minutes", "every": "5"})

    def test_hourly_every_capped_at_23(self):
        with pytest.raises(ValueError, match="1..23"):
            schedule_from_dict({"type": "hourly", "every": 24})

    def test_weekly_day_must_be_known(self):
        with pytest.raises(ValueError, match="day must"):
            schedule_from_dict({"type": "weekly", "day": "FRIDAY", "at": "06:00"})
        ok = schedule_from_dict({"type": "weekly", "day": "fri", "at": "06:00"})
        assert ok.day == "FRI"


class TestScheduleChip:
    def test_chip_strings(self):
        assert Schedule(type="none").chip() == ""
        assert Schedule(type="daily", at="06:00").chip() == "daily 06:00"
        assert (
            Schedule(type="daily_times", at=["06:00", "12:00"]).chip()
            == "daily 06:00 12:00"
        )
        assert Schedule(type="minutes", every=5).chip() == "every 5 min"
        assert Schedule(type="weekly", day="MON", at="06:00").chip() == "MON 06:00"


# ====================================================================== job


class TestJobFromDict:
    def test_minimum_valid(self):
        job = job_from_dict(
            {
                "id": "demo",
                "name": "Demo",
                "script_path": "C:\\stub\\demo.py",
            }
        )
        assert job.id == "demo"
        assert job.schedule.type == "none"
        assert job.target_kind == "py"

    def test_bat_target_kind(self):
        job = job_from_dict(
            {
                "id": "rep",
                "name": "Reporting",
                "script_path": "C:\\stub\\launch_reporting.bat",
            }
        )
        assert job.target_kind == "bat"

    def test_bad_suffix_rejected(self):
        with pytest.raises(ValueError, match=".py or .bat"):
            job_from_dict({"id": "x", "name": "X", "script_path": "C:\\x.txt"})

    def test_missing_required_fields(self):
        with pytest.raises(ValueError, match="script_path"):
            job_from_dict({"id": "x", "name": "X"})
        with pytest.raises(ValueError, match="name"):
            job_from_dict({"id": "x", "script_path": "C:\\x.py"})


class TestMakeJobId:
    def test_slug_collision_suffix(self):
        existing = ["demo", "demo-2"]
        assert make_job_id("Demo", existing) == "demo-3"
        assert make_job_id("Fresh", existing) == "fresh"


class TestJobsConfigPersistence:
    def test_round_trip(self, tmp_path):
        path = tmp_path / "jobs.json"
        cfg = JobsConfig(
            jobs=[
                Job(
                    id="demo",
                    name="Demo",
                    script_path="C:\\stub\\demo.py",
                    schedule=Schedule(type="daily", at="06:00"),
                )
            ]
        )
        save_jobs(cfg, path=path)
        reloaded = load_jobs(path=path)
        assert len(reloaded.jobs) == 1
        assert reloaded.jobs[0].id == "demo"
        assert reloaded.jobs[0].schedule.at == "06:00"

    def test_missing_file_is_empty(self, tmp_path):
        cfg = load_jobs(path=tmp_path / "nope.json")
        assert cfg.jobs == []

    def test_malformed_rows_skipped(self, tmp_path, caplog):
        path = tmp_path / "jobs.json"
        path.write_text(
            json.dumps(
                {
                    "jobs": [
                        {"id": "ok", "name": "OK", "script_path": "C:\\ok.py"},
                        {"id": "bad", "name": "Bad", "script_path": "C:\\bad.txt"},
                    ]
                }
            ),
            encoding="utf-8",
        )
        cfg = load_jobs(path=path)
        assert [j.id for j in cfg.jobs] == ["ok"]


class TestMutations:
    def test_add_rejects_duplicate(self, tmp_path, monkeypatch):
        path = tmp_path / "jobs.json"
        monkeypatch.setattr("src.jobs_config.DEFAULT_JOBS_PATH", path)
        cfg = JobsConfig()
        job = Job(
            id="demo", name="Demo", script_path="C:\\stub\\demo.py"
        )
        add_job(cfg, job)
        with pytest.raises(ValueError, match="already exists"):
            add_job(cfg, Job(id="demo", name="Demo2", script_path="C:\\stub\\demo.py"))

    def test_update_changes_fields(self, tmp_path, monkeypatch):
        path = tmp_path / "jobs.json"
        monkeypatch.setattr("src.jobs_config.DEFAULT_JOBS_PATH", path)
        cfg = JobsConfig()
        add_job(cfg, Job(id="demo", name="Demo", script_path="C:\\stub\\demo.py"))
        updated = update_job(
            cfg, "demo",
            name="Demo 2",
            schedule={"type": "daily", "at": "07:00"},
        )
        assert updated.name == "Demo 2"
        assert updated.schedule.at == "07:00"

    def test_remove_returns_entry(self, tmp_path, monkeypatch):
        path = tmp_path / "jobs.json"
        monkeypatch.setattr("src.jobs_config.DEFAULT_JOBS_PATH", path)
        cfg = JobsConfig()
        add_job(cfg, Job(id="demo", name="Demo", script_path="C:\\stub\\demo.py"))
        removed = remove_by_id(cfg, "demo")
        assert removed is not None
        assert get_by_id(cfg, "demo") is None


# ================================================================= schtasks


class TestScheduleArgvParts:
    def test_none_empty(self):
        assert jobs_mod.schedule_argv_parts(Schedule(type="none")) == []

    def test_daily(self):
        parts = jobs_mod.schedule_argv_parts(Schedule(type="daily", at="06:00"))
        assert parts == [["/SC", "DAILY", "/ST", "06:00"]]

    def test_daily_times_fans_out(self):
        parts = jobs_mod.schedule_argv_parts(
            Schedule(type="daily_times", at=["06:00", "12:00", "18:00"])
        )
        assert parts == [
            ["/SC", "DAILY", "/ST", "06:00"],
            ["/SC", "DAILY", "/ST", "12:00"],
            ["/SC", "DAILY", "/ST", "18:00"],
        ]

    def test_minutes(self):
        parts = jobs_mod.schedule_argv_parts(Schedule(type="minutes", every=5))
        assert parts == [["/SC", "MINUTE", "/MO", "5"]]

    def test_hourly(self):
        parts = jobs_mod.schedule_argv_parts(Schedule(type="hourly", every=6))
        assert parts == [["/SC", "HOURLY", "/MO", "6"]]

    def test_weekly(self):
        parts = jobs_mod.schedule_argv_parts(
            Schedule(type="weekly", day="FRI", at="06:00")
        )
        assert parts == [["/SC", "WEEKLY", "/D", "FRI", "/ST", "06:00"]]


class TestTaskNamesFor:
    def test_bare_for_daily(self):
        job = Job(
            id="reporting",
            name="Reporting",
            script_path="C:\\x.bat",
            schedule=Schedule(type="daily", at="06:00"),
        )
        assert jobs_mod.task_names_for(job) == ["\\AppLauncher\\reporting"]

    def test_suffixed_for_daily_times(self):
        job = Job(
            id="linkedin-scrape",
            name="LinkedIn",
            script_path="C:\\x.py",
            schedule=Schedule(type="daily_times", at=["06:00", "12:00", "18:00"]),
        )
        assert jobs_mod.task_names_for(job) == [
            "\\AppLauncher\\linkedin-scrape-1",
            "\\AppLauncher\\linkedin-scrape-2",
            "\\AppLauncher\\linkedin-scrape-3",
        ]


class TestSyncSchtasks:
    def test_daily_creates_one_task(self):
        job = Job(
            id="demo",
            name="Demo",
            script_path="C:\\stub\\demo.py",
            schedule=Schedule(type="daily", at="06:00"),
        )
        # Runner: list_known_tasks (Query) returns empty; deletes blind;
        # creates succeed.
        calls: List[List[str]] = []

        def runner(argv):
            calls.append(argv)
            # First call from list_known_tasks: empty stdout → no known tasks.
            if argv[:2] == ["schtasks", "/Query"]:
                return _mk_completed(stdout="", rc=0)
            return _mk_completed(rc=0)

        created = jobs_mod.sync_schtasks(job, runner=runner)
        assert created == ["\\AppLauncher\\demo"]
        # Last call is the /Create for the daily task.
        last = calls[-1]
        assert last[:5] == ["schtasks", "/Create", "/F", "/TN", "\\AppLauncher\\demo"]
        assert "/SC" in last and "DAILY" in last and "06:00" in last

    def test_daily_times_creates_three_tasks(self):
        job = Job(
            id="ls",
            name="LinkedIn",
            script_path="C:\\stub\\scrape.py",
            schedule=Schedule(type="daily_times", at=["06:00", "12:00", "18:00"]),
        )
        runner = MagicMock(return_value=_mk_completed(rc=0))
        created = jobs_mod.sync_schtasks(job, runner=runner)
        assert created == [
            "\\AppLauncher\\ls-1",
            "\\AppLauncher\\ls-2",
            "\\AppLauncher\\ls-3",
        ]

    def test_none_schedule_only_deletes(self):
        job = Job(
            id="demo",
            name="Demo",
            script_path="C:\\stub\\demo.py",
            schedule=Schedule(type="none"),
        )
        runner = MagicMock(return_value=_mk_completed(rc=0))
        created = jobs_mod.sync_schtasks(job, runner=runner)
        assert created == []
        # No /Create call should have been issued.
        create_calls = [
            c for c in runner.call_args_list
            if c.args[0][:2] == ["schtasks", "/Create"]
        ]
        assert create_calls == []


class TestDeleteSchtasks:
    def test_uses_query_results_when_available(self):
        # Query lists three tasks under \AppLauncher\ — two for our job, one foreign.
        query_stdout = (
            '"\\AppLauncher\\ls-1","ready","..."\n'
            '"\\AppLauncher\\ls-2","ready","..."\n'
            '"\\AppLauncher\\other","ready","..."\n'
        )
        deletes: List[str] = []

        def runner(argv):
            if argv[:2] == ["schtasks", "/Query"]:
                return _mk_completed(stdout=query_stdout, rc=0)
            if argv[:2] == ["schtasks", "/Delete"]:
                deletes.append(argv[4])  # /TN value
                return _mk_completed(rc=0)
            return _mk_completed(rc=0)

        result = jobs_mod.delete_schtasks("ls", runner=runner)
        assert sorted(result) == ["\\AppLauncher\\ls-1", "\\AppLauncher\\ls-2"]
        # The foreign task is left alone.
        assert "\\AppLauncher\\other" not in result


# ================================================================ executor


class TestResolveVenvPython:
    def test_walks_up_to_sibling_venv(self, tmp_path):
        # Layout: <root>/proj/sub/script.py with <root>/proj/.venv/Scripts/python.exe
        root = tmp_path
        proj = root / "proj"
        sub = proj / "sub"
        sub.mkdir(parents=True)
        venv_python = proj / ".venv" / "Scripts" / "python.exe"
        venv_python.parent.mkdir(parents=True)
        venv_python.write_text("stub")
        script = sub / "script.py"
        script.write_text("# stub")
        resolved = resolve_venv_python(script)
        assert resolved == venv_python

    def test_returns_none_when_no_venv(self, tmp_path):
        script = tmp_path / "lonely.py"
        script.write_text("# stub")
        assert resolve_venv_python(script) is None


class TestBuildInvocation:
    def test_bat_dispatch(self, tmp_path):
        bat = tmp_path / "demo.bat"
        bat.write_text("@echo off")
        job = Job(id="demo", name="Demo", script_path=str(bat), args="auto")
        argv, cwd, env = build_invocation(job)
        assert argv == ["cmd.exe", "/c", str(bat), "auto"]
        assert cwd == bat.parent
        assert env == {}

    def test_py_dispatch_with_venv(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        venv_py = proj / ".venv" / "Scripts" / "python.exe"
        venv_py.parent.mkdir(parents=True)
        venv_py.write_text("stub")
        script = proj / "sub" / "scrape.py"
        script.parent.mkdir()
        script.write_text("# stub")
        job = Job(id="ls", name="LS", script_path=str(script), args="")
        argv, cwd, env = build_invocation(job)
        assert argv == [str(venv_py), str(script)]
        # cwd is the project root (where the .venv lives).
        assert cwd == proj
        # PYTHONPATH points at the project root so package imports resolve.
        assert env["PYTHONPATH"] == str(proj)

    def test_bad_suffix_rejected(self, tmp_path):
        job = Job(id="x", name="X", script_path=str(tmp_path / "x.txt"))
        with pytest.raises(ValueError, match="unsupported"):
            build_invocation(job)


# ============================================================= run history


class TestRunHistory:
    def test_new_run_dir_creates(self, tmp_path, monkeypatch):
        monkeypatch.setattr(jobs_mod, "JOBS_RUNS_DIR", tmp_path)
        rd = jobs_mod.new_run_dir("demo", "20260523T060000")
        assert rd.exists()
        assert rd.parent.name == "demo"

    def test_new_run_dir_handles_collisions(self, tmp_path, monkeypatch):
        monkeypatch.setattr(jobs_mod, "JOBS_RUNS_DIR", tmp_path)
        rd1 = jobs_mod.new_run_dir("demo", "20260523T060000")
        rd2 = jobs_mod.new_run_dir("demo", "20260523T060000")
        assert rd1 != rd2
        assert rd2.name.endswith("-2")

    def test_write_and_read_run_json(self, tmp_path, monkeypatch):
        monkeypatch.setattr(jobs_mod, "JOBS_RUNS_DIR", tmp_path)
        rd = jobs_mod.new_run_dir("demo", "20260523T060000")
        jobs_mod.write_run_json(rd, status="running", trigger="manual")
        jobs_mod.write_run_json(rd, status="success", exit_code=0)
        record = jobs_mod.read_run(rd)
        # Updates merge — trigger from the first write survives.
        assert record["status"] == "success"
        assert record["trigger"] == "manual"
        assert record["exit_code"] == 0

    def test_list_runs_newest_first(self, tmp_path, monkeypatch):
        monkeypatch.setattr(jobs_mod, "JOBS_RUNS_DIR", tmp_path)
        for stamp in ("20260101T060000", "20260102T060000", "20260103T060000"):
            rd = jobs_mod.new_run_dir("demo", stamp)
            jobs_mod.write_run_json(rd, status="success")
        runs = jobs_mod.list_runs("demo")
        assert [r["run_id"] for r in runs] == [
            "20260103T060000",
            "20260102T060000",
            "20260101T060000",
        ]

    def test_prune_keeps_latest_n(self, tmp_path, monkeypatch):
        monkeypatch.setattr(jobs_mod, "JOBS_RUNS_DIR", tmp_path)
        for i in range(5):
            stamp = f"2026010{i + 1}T060000"
            rd = jobs_mod.new_run_dir("demo", stamp)
            jobs_mod.write_run_json(rd, status="success")
        removed = jobs_mod.prune_runs("demo", keep=2)
        assert removed == 3
        survivors = sorted((tmp_path / "demo").iterdir())
        assert [p.name for p in survivors] == [
            "20260104T060000",
            "20260105T060000",
        ]

    def test_read_output_tail(self, tmp_path, monkeypatch):
        monkeypatch.setattr(jobs_mod, "JOBS_RUNS_DIR", tmp_path)
        rd = jobs_mod.new_run_dir("demo", "20260523T060000")
        (rd / "output.log").write_text("hello\nworld\n", encoding="utf-8")
        assert "world" in jobs_mod.read_output_tail(rd)
