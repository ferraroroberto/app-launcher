"""src.jobs schtasks plumbing: argv mapping, sync, list and delete, all through a mocked runner."""

from __future__ import annotations

import json
import subprocess
from typing import List
from unittest.mock import MagicMock


from src import jobs as jobs_mod
from src import jobs_schtasks as jobs_schtasks_mod
from src.jobs_config import (
    Job,
    Schedule,
)


def _mk_completed(stdout: str = "", rc: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr="")


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


class TestSpawnRunJobDetached:
    """issue #416: DETACHED_PROCESS alone does not escape ``taskkill /T`` —
    the spawn must re-parent via ``cmd /c start`` like the session-host does.
    """

    def test_wraps_argv_in_cmd_start_and_drops_detached_process_flag(
        self, monkeypatch
    ):
        captured = {}

        class _FakeProc:
            pid = 4242

        def fake_popen(argv, **kwargs):
            captured["argv"] = argv
            captured["kwargs"] = kwargs
            return _FakeProc()

        monkeypatch.setattr(jobs_schtasks_mod.subprocess, "Popen", fake_popen)

        pid = jobs_schtasks_mod.spawn_run_job_detached("demo", "run-1", "manual")

        assert pid == 4242
        argv = captured["argv"]
        assert argv[:5] == ["cmd", "/c", "start", "", "/b"]
        # The real launcher invocation follows the cmd/start wrapper untouched.
        assert "run-job" in argv
        assert "demo" in argv
        assert "run-1" in argv
        # DETACHED_PROCESS must NOT be set — it doesn't escape taskkill /T
        # (verified empirically, see app/tray/tray.py::_start_session_host).
        detached_flag = getattr(subprocess, "DETACHED_PROCESS", 0)
        creationflags = captured["kwargs"].get("creationflags", 0)
        if detached_flag:
            assert not (creationflags & detached_flag)

    def test_params_and_dry_run_still_appended_before_the_wrapper(
        self, monkeypatch
    ):
        captured = {}

        class _FakeProc:
            pid = 1

        def fake_popen(argv, **kwargs):
            captured["argv"] = argv
            return _FakeProc()

        monkeypatch.setattr(jobs_schtasks_mod.subprocess, "Popen", fake_popen)

        jobs_schtasks_mod.spawn_run_job_detached(
            "demo", "run-2", "manual", params={"x": 1}, dry_run=True
        )

        argv = captured["argv"]
        assert argv[:5] == ["cmd", "/c", "start", "", "/b"]
        assert "--params" in argv
        assert json.dumps({"x": 1}) in argv
        assert "--dry-run" in argv


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
        # /Create for the daily task, followed by one power-policy call.
        create_calls = [c for c in calls if c[:2] == ["schtasks", "/Create"]]
        assert len(create_calls) == 1
        last_create = create_calls[0]
        assert last_create[:5] == [
            "schtasks", "/Create", "/F", "/TN", "\\AppLauncher\\demo",
        ]
        assert "/SC" in last_create and "DAILY" in last_create and "06:00" in last_create
        # Exactly one batched PowerShell power-policy call, naming the task.
        ps_calls = [c for c in calls if c[0] == jobs_schtasks_mod._POWERSHELL_EXE]
        assert len(ps_calls) == 1
        ps_script = ps_calls[0][-1]
        assert "-TaskName 'demo'" in ps_script
        assert "StartWhenAvailable = $true" in ps_script
        assert "DisallowStartIfOnBatteries = $false" in ps_script
        assert "StopIfGoingOnBatteries = $false" in ps_script
        assert "WakeToRun" not in ps_script

    def test_daily_times_creates_three_tasks(self):
        job = Job(
            id="ls",
            name="LinkedIn",
            script_path="C:\\stub\\scrape.py",
            schedule=Schedule(type="daily_times", at=["06:00", "12:00", "18:00"]),
        )
        calls: List[List[str]] = []

        def runner(argv):
            calls.append(argv)
            return _mk_completed(rc=0)

        created = jobs_mod.sync_schtasks(job, runner=runner)
        assert created == [
            "\\AppLauncher\\ls-1",
            "\\AppLauncher\\ls-2",
            "\\AppLauncher\\ls-3",
        ]
        # All three slots are batched into a single PowerShell spawn, not
        # one per task.
        ps_calls = [c for c in calls if c[0] == jobs_schtasks_mod._POWERSHELL_EXE]
        assert len(ps_calls) == 1
        ps_script = ps_calls[0][-1]
        for suffix in ("ls-1", "ls-2", "ls-3"):
            assert f"-TaskName '{suffix}'" in ps_script

    def test_power_policy_failure_is_logged_not_raised(self, caplog):
        job = Job(
            id="demo",
            name="Demo",
            script_path="C:\\stub\\demo.py",
            schedule=Schedule(type="daily", at="06:00"),
        )

        def runner(argv):
            if argv[0] == jobs_schtasks_mod._POWERSHELL_EXE:
                return _mk_completed(stdout="", rc=1)
            return _mk_completed(rc=0)

        import logging

        with caplog.at_level(logging.WARNING, logger=jobs_schtasks_mod.logger.name):
            created = jobs_mod.sync_schtasks(job, runner=runner)
        # The task itself was still created — the power-policy write is
        # best-effort and never blocks the create result.
        assert created == ["\\AppLauncher\\demo"]
        assert "power-policy" in caplog.text

    def test_elevated_job_skips_power_policy(self):
        job = Job(
            id="hwinfo-restart",
            name="hwinfo restart",
            script_path="C:\\stub\\hwinfo.py",
            schedule=Schedule(type="daily", at="03:00"),
            elevated=True,
        )
        calls: List[List[str]] = []

        def runner(argv):
            calls.append(argv)
            if argv[:2] == ["schtasks", "/Query"]:
                return _mk_completed(stdout="", rc=0)
            return _mk_completed(rc=0)

        created = jobs_mod.sync_schtasks(job, runner=runner)
        assert created == []
        assert not any(c[0] == jobs_schtasks_mod._POWERSHELL_EXE for c in calls)

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


class TestListKnownTasks:
    def test_query_failure_returns_none_and_logs(self, caplog):
        # rc != 0 (Task Scheduler service down / no permission) must be
        # reported, not silently folded into "no known tasks" (issue #830).
        runner = MagicMock(
            return_value=_mk_completed(stdout="", rc=1)
        )
        runner.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="RPC server unavailable."
        )
        with caplog.at_level("WARNING", logger=jobs_schtasks_mod.logger.name):
            result = jobs_schtasks_mod.list_known_tasks(runner=runner)
        assert result is None
        assert any(
            "schtasks query failed" in record.message
            for record in caplog.records
        )

    def test_successful_empty_query_returns_empty_list_not_none(self):
        # rc == 0 with real (non-empty) CSV output but no AppLauncher rows
        # is a genuine "no tasks" answer, distinct from a failed query.
        runner = MagicMock(
            return_value=_mk_completed(
                stdout='"\\Foreign\\other","ready","..."\n', rc=0
            )
        )
        result = jobs_schtasks_mod.list_known_tasks(runner=runner)
        assert result == []


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

    def test_successful_empty_query_does_not_trigger_blind_fallback(self):
        # A genuinely empty-but-successful query (no AppLauncher tasks at
        # all) must not be treated the same as a failed query: there is
        # nothing to delete, so no blind /Delete attempts should fire.
        deletes: List[str] = []

        def runner(argv):
            if argv[:2] == ["schtasks", "/Query"]:
                return _mk_completed(
                    stdout='"\\Foreign\\other","ready","..."\n', rc=0
                )
            if argv[:2] == ["schtasks", "/Delete"]:
                deletes.append(argv[4])  # /TN value
                return _mk_completed(rc=0)
            return _mk_completed(rc=0)

        result = jobs_mod.delete_schtasks("ls", runner=runner)
        assert result == []
        assert deletes == []

    def test_query_failure_falls_back_to_blind_delete(self):
        def runner(argv):
            if argv[:2] == ["schtasks", "/Query"]:
                return subprocess.CompletedProcess(
                    args=[], returncode=1, stdout="", stderr="RPC server unavailable."
                )
            return _mk_completed(rc=0)

        result = jobs_mod.delete_schtasks("ls", runner=runner)
        # Blind fallback: bare task + every daily_times slot variant.
        assert "\\AppLauncher\\ls" in result


class TestBulkParser:
    """``src.jobs_schtasks._parse_bulk_records`` — the bulk Next Run Time parse."""

    def test_filters_by_prefix(self):
        stdout = (
            "TaskName: \\AppLauncher\\demo\nNext Run Time: 2026-06-01 06:00:00\n\n"
            "TaskName: \\Foreign\\task\nNext Run Time: 2026-06-01 12:00:00\n\n"
        )
        out = jobs_schtasks_mod._project(
            jobs_schtasks_mod._parse_bulk_records(stdout), "next_run"
        )
        assert "\\AppLauncher\\demo" in out
        assert out["\\AppLauncher\\demo"] == "2026-06-01 06:00:00"
        # Foreign task is filtered out.
        assert "\\Foreign\\task" not in out

    def test_n_a_collapses_to_none(self):
        stdout = (
            "TaskName: \\AppLauncher\\demo-1\nNext Run Time: N/A\n\n"
            "TaskName: \\AppLauncher\\demo-2\nNext Run Time: Disabled\n\n"
        )
        out = jobs_schtasks_mod._project(
            jobs_schtasks_mod._parse_bulk_records(stdout), "next_run"
        )
        assert out["\\AppLauncher\\demo-1"] is None
        assert out["\\AppLauncher\\demo-2"] is None


class TestBulkQueryScope:
    """#1516: ``_bulk_records`` asks for the ``\\AppLauncher\\`` folder, and
    falls back to the whole-machine query only when that fails."""

    _FOLDER = "TaskName: \\AppLauncher\\demo\nNext Run Time: 2026-06-01 06:00:00\n\n"

    @staticmethod
    def _runner(answers):
        calls: List[List[str]] = []

        def run(argv):
            calls.append(argv)
            rc, stdout, stderr = answers[len(calls) - 1]
            return subprocess.CompletedProcess(argv, rc, stdout=stdout, stderr=stderr)

        return run, calls

    def test_the_folder_query_answers_alone(self):
        run, calls = self._runner([(0, self._FOLDER, "")])
        out = jobs_schtasks_mod._bulk_records(runner=run)
        assert list(out) == ["\\AppLauncher\\demo"]
        assert calls == [["schtasks", "/Query", "/TN", "\\AppLauncher\\", "/FO", "LIST", "/V"]]

    def test_a_missing_folder_falls_back_and_still_means_no_tasks(self):
        # schtasks refuses a folder that does not exist yet; the full query
        # then answers, and "no AppLauncher tasks" stays {} rather than None.
        missing = (1, "", "ERROR: The system cannot find the file specified.\n")
        foreign = (0, "TaskName: \\Foreign\\task\nNext Run Time: N/A\n\n", "")
        run, calls = self._runner([missing, foreign])
        assert jobs_schtasks_mod._bulk_records(runner=run) == {}
        assert calls[1] == ["schtasks", "/Query", "/FO", "LIST", "/V"]

    def test_both_queries_failing_is_still_unknown(self):
        run, calls = self._runner([(1, "", "boom"), (1, "", "boom")])
        assert jobs_schtasks_mod._bulk_records(runner=run) is None
        assert len(calls) == 2
