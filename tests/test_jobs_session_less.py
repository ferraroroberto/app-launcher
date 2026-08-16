"""`session_less` job flag and its Task Scheduler carve-out (issue #757).

Every ``\\AppLauncher\\`` task is created by ``schtasks /Create`` with no
principal flags, so Windows applies its default — ``InteractiveToken``, "run
only when the user is logged on". While the machine sits logged out Task
Scheduler skips those triggers **silently**: no run, no error, no
``LastTaskResult``, no catch-up. That cost a ``fleet-private-backup-daily``
run on 2026-08-13 and would have cost seven jobs had the update reboot landed
two hours earlier.

The fix cannot be "add a principal to the argv": #757 measured, from a
non-elevated shell (the webapp's own privilege level), that *every* route to
an S4U principal returns ``Access is denied`` — including the
create-then-``Set-ScheduledTask`` patch that works fine for *Settings*. So a
``session_less`` job's entry is externally managed, exactly like ``elevated``
(#352), with one deliberate difference: it is never deleted either, because a
delete would succeed and destroy an entry this process can never recreate.

These tests cover the schema round-trip, the mutual exclusion with
``visible``, the sync carve-out, the generated registration command, and the
coverage check that catches an entry left on the wrong principal.
"""

from __future__ import annotations

import subprocess
from typing import List

import pytest

from src import jobs as jobs_mod
from src import jobs_config as jc
from src import jobs_coverage
from src import jobs_schtasks
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


def _recording_runner(calls: List[List[str]]):
    def runner(argv):
        calls.append(argv)
        if argv[:2] == ["schtasks", "/Query"]:
            return _mk_completed(stdout="", rc=0)
        return _mk_completed(rc=0)

    return runner


class TestSessionLessRoundTrip:
    def test_default_is_false_and_omitted(self):
        job = Job(id="j", name="J", script_path="C:\\x\\s.py")
        assert job.session_less is False
        assert "session_less" not in job.to_dict()

    def test_true_emitted_and_parsed(self):
        job = Job(id="j", name="J", script_path="C:\\x\\s.py", session_less=True)
        payload = job.to_dict()
        assert payload["session_less"] is True
        assert job_from_dict(payload).session_less is True

    def test_from_dict_defaults_false(self):
        job = job_from_dict({"id": "j", "name": "J", "script_path": "C:\\x\\s.py"})
        assert job.session_less is False

    def test_update_job_toggles(self, tmp_path, monkeypatch):
        monkeypatch.setattr(jc, "DEFAULT_JOBS_PATH", tmp_path / "jobs.json")
        cfg = JobsConfig(jobs=[Job(id="j", name="J", script_path="C:\\x\\s.py")])
        save_jobs(cfg)
        update_job(cfg, "j", session_less=True)
        assert jc.get_by_id(cfg, "j").session_less is True
        update_job(cfg, "j", session_less=False)
        assert jc.get_by_id(cfg, "j").session_less is False


class TestMutuallyExclusiveWithVisible:
    """An S4U task runs in session 0 with no desktop, so `visible`'s console
    window has nowhere to render. Combining them is silently futile, so it is
    rejected at both write paths rather than resolved by precedence."""

    def test_job_from_dict_rejects_both(self):
        with pytest.raises(ValueError, match="mutually exclusive"):
            job_from_dict(
                {
                    "id": "j",
                    "name": "J",
                    "script_path": "C:\\x\\s.py",
                    "visible": True,
                    "session_less": True,
                }
            )

    def test_each_alone_is_fine(self):
        assert job_from_dict(
            {"id": "j", "name": "J", "script_path": "C:\\x\\s.py", "visible": True}
        ).visible is True
        assert job_from_dict(
            {"id": "j", "name": "J", "script_path": "C:\\x\\s.py", "session_less": True}
        ).session_less is True

    def test_update_job_rejects_the_effective_pair(self, tmp_path, monkeypatch):
        # The edit names only session_less; it must still be checked against
        # the *stored* visible, not just against the incoming patch.
        monkeypatch.setattr(jc, "DEFAULT_JOBS_PATH", tmp_path / "jobs.json")
        cfg = JobsConfig(
            jobs=[Job(id="j", name="J", script_path="C:\\x\\s.py", visible=True)]
        )
        save_jobs(cfg)
        with pytest.raises(ValueError, match="mutually exclusive"):
            update_job(cfg, "j", session_less=True)
        # Rejected edit leaves the job untouched.
        assert jc.get_by_id(cfg, "j").session_less is False
        assert jc.get_by_id(cfg, "j").visible is True

    def test_update_job_can_swap_both_at_once(self, tmp_path, monkeypatch):
        monkeypatch.setattr(jc, "DEFAULT_JOBS_PATH", tmp_path / "jobs.json")
        cfg = JobsConfig(
            jobs=[Job(id="j", name="J", script_path="C:\\x\\s.py", visible=True)]
        )
        save_jobs(cfg)
        update_job(cfg, "j", visible=False, session_less=True)
        assert jc.get_by_id(cfg, "j").session_less is True
        assert jc.get_by_id(cfg, "j").visible is False


class TestSyncSchtasksNeverTouchesSessionLess:
    """Unlike the elevated carve-out (#352/#409), a session-less job's entry is
    not deleted either: the delete would succeed and strand a job this process
    cannot re-register. The stale-entry risk is reported by coverage instead."""

    def test_no_create_and_no_delete(self):
        job = Job(
            id="backup",
            name="Backup",
            script_path="C:\\stub\\backup.py",
            schedule=Schedule(type="daily", at="03:00"),
            session_less=True,
        )
        calls: List[List[str]] = []
        assert jobs_mod.sync_schtasks(job, runner=_recording_runner(calls)) == []
        assert calls == []

    def test_session_less_wins_over_elevated(self):
        # A job flagged both must keep the safer rule (no delete), so the
        # elevated branch's #409 stale-entry delete must not run.
        job = Job(
            id="both",
            name="Both",
            script_path="C:\\stub\\both.py",
            schedule=Schedule(type="daily", at="03:00"),
            session_less=True,
            elevated=True,
        )
        calls: List[List[str]] = []
        assert jobs_mod.sync_schtasks(job, runner=_recording_runner(calls)) == []
        assert calls == []

    def test_plain_job_unaffected(self):
        job = Job(
            id="plain",
            name="Plain",
            script_path="C:\\stub\\demo.py",
            schedule=Schedule(type="daily", at="06:00"),
        )
        calls: List[List[str]] = []
        created = jobs_mod.sync_schtasks(job, runner=_recording_runner(calls))
        assert created == ["\\AppLauncher\\plain"]
        assert any(c[:2] == ["schtasks", "/Create"] for c in calls)


class TestRegistrationScript:
    def test_daily_emits_one_s4u_registration(self):
        job = Job(
            id="backup",
            name="Backup",
            script_path="C:\\stub\\backup.py",
            schedule=Schedule(type="daily", at="03:00"),
            session_less=True,
        )
        script = jobs_mod.registration_script(job)
        assert script is not None
        assert "-LogonType S4U" in script
        assert "-StartWhenAvailable" in script
        assert "New-ScheduledTaskTrigger -Daily -At '03:00'" in script
        assert "-TaskName 'backup'" in script
        assert script.count("Register-ScheduledTask") == 1
        # No machine identity baked in - the script resolves it at run time.
        assert "$env:USERDOMAIN\\$env:USERNAME" in script

    def test_daily_times_fans_out_to_the_same_task_names(self):
        job = Job(
            id="multi",
            name="Multi",
            script_path="C:\\stub\\m.py",
            schedule=Schedule(type="daily_times", at=["06:00", "18:00"]),
            session_less=True,
        )
        script = jobs_mod.registration_script(job)
        assert script is not None
        assert script.count("Register-ScheduledTask") == 2
        # Must match task_names_for exactly, or coverage reports the extra
        # slots as missing entries.
        for name in jobs_mod.task_names_for(job):
            assert f"-TaskName '{name.split(chr(92))[-1]}'" in script

    def test_weekly_maps_the_day_name(self):
        job = Job(
            id="w",
            name="W",
            script_path="C:\\stub\\w.py",
            schedule=Schedule(type="weekly", day="MON", at="09:00"),
            session_less=True,
        )
        script = jobs_mod.registration_script(job)
        assert script is not None
        assert "-Weekly -DaysOfWeek Monday -At '09:00'" in script

    def test_minutes_uses_a_repetition_interval(self):
        job = Job(
            id="m",
            name="M",
            script_path="C:\\stub\\m.py",
            schedule=Schedule(type="minutes", every=15),
            session_less=True,
        )
        script = jobs_mod.registration_script(job)
        assert script is not None
        assert "New-TimeSpan -Minutes 15" in script

    def test_once_parses_the_iso_stamp(self):
        job = Job(
            id="o",
            name="O",
            script_path="C:\\stub\\o.py",
            schedule=Schedule(type="once", at="2026-09-01T14:30"),
            session_less=True,
        )
        script = jobs_mod.registration_script(job)
        assert script is not None
        assert "Get-Date '2026-09-01T14:30'" in script

    def test_elevated_adds_run_level_highest(self):
        job = Job(
            id="e",
            name="E",
            script_path="C:\\stub\\e.py",
            schedule=Schedule(type="daily", at="03:00"),
            session_less=True,
            elevated=True,
        )
        script = jobs_mod.registration_script(job)
        assert script is not None
        assert "-RunLevel Highest" in script

    def test_no_schedule_yields_none(self):
        job = Job(
            id="n",
            name="N",
            script_path="C:\\stub\\n.py",
            schedule=Schedule(type="none"),
            session_less=True,
        )
        assert jobs_mod.registration_script(job) is None


_BULK_INTERACTIVE = """\
TaskName:                             \\AppLauncher\\backup
Next Run Time:                        17/08/2026 3:00:00
Status:                               Ready
Logon Mode:                           Interactive only
Scheduled Task State:                 Enabled

"""

_BULK_BACKGROUND = _BULK_INTERACTIVE.replace(
    "Interactive only", "Interactive/Background"
)

_BULK_NO_MODE = "\n".join(
    line for line in _BULK_INTERACTIVE.splitlines() if "Logon Mode" not in line
)


class TestLogonModeParsing:
    def test_interactive_only_is_not_background_capable(self):
        rec = jobs_schtasks._parse_bulk_records(_BULK_INTERACTIVE)
        assert rec["\\AppLauncher\\backup"]["background_capable"] is False

    def test_background_is_capable(self):
        rec = jobs_schtasks._parse_bulk_records(_BULK_BACKGROUND)
        assert rec["\\AppLauncher\\backup"]["background_capable"] is True

    def test_absent_field_is_unknown_not_false(self):
        rec = jobs_schtasks._parse_bulk_records(_BULK_NO_MODE)
        assert rec["\\AppLauncher\\backup"]["background_capable"] is None


class TestCoveragePrincipalCheck:
    """A session-less job whose entry exists and is enabled but still carries
    the InteractiveToken principal passes both pre-existing coverage halves
    and does nothing whenever the box is logged out. That is #757's whole
    failure mode, so it gets its own problem code."""

    def _job(self, **kw) -> Job:
        return Job(
            id="backup",
            name="Backup",
            script_path="C:\\stub\\backup.py",
            schedule=Schedule(type="daily", at="03:00"),
            **kw,
        )

    def test_interactive_entry_is_a_problem(self, monkeypatch):
        monkeypatch.setattr(
            jobs_coverage, "behavioural_coverage", lambda job, now=None: ([], None)
        )
        result = jobs_coverage.coverage_for(
            self._job(session_less=True),
            {"\\AppLauncher\\backup": True},
            principals={"\\AppLauncher\\backup": False},
        )
        assert result["state"] == jobs_coverage.STATE_PROBLEM
        assert jobs_coverage.PROBLEM_PRINCIPAL_INTERACTIVE in result["problems"]
        assert result["interactive_tasks"] == ["\\AppLauncher\\backup"]

    def test_background_entry_is_ok(self, monkeypatch):
        monkeypatch.setattr(
            jobs_coverage, "behavioural_coverage", lambda job, now=None: ([], None)
        )
        result = jobs_coverage.coverage_for(
            self._job(session_less=True),
            {"\\AppLauncher\\backup": True},
            principals={"\\AppLauncher\\backup": True},
        )
        assert result["state"] == jobs_coverage.STATE_OK

    def test_unreadable_principal_is_unknown_not_ok(self, monkeypatch):
        monkeypatch.setattr(
            jobs_coverage, "behavioural_coverage", lambda job, now=None: ([], None)
        )
        result = jobs_coverage.coverage_for(
            self._job(session_less=True),
            {"\\AppLauncher\\backup": True},
            principals={"\\AppLauncher\\backup": None},
        )
        assert result["state"] == jobs_coverage.STATE_UNKNOWN

    def test_failed_query_is_unknown_not_ok(self, monkeypatch):
        monkeypatch.setattr(
            jobs_coverage, "behavioural_coverage", lambda job, now=None: ([], None)
        )
        result = jobs_coverage.coverage_for(
            self._job(session_less=True),
            {"\\AppLauncher\\backup": True},
            principals=None,
        )
        assert result["state"] == jobs_coverage.STATE_UNKNOWN

    def test_missing_entry_reports_only_the_structural_problem(self, monkeypatch):
        # "missing" and "wrong principal" for one entry is one problem told
        # twice - the structural half owns it.
        monkeypatch.setattr(
            jobs_coverage, "behavioural_coverage", lambda job, now=None: ([], None)
        )
        result = jobs_coverage.coverage_for(
            self._job(session_less=True), {}, principals={}
        )
        assert result["problems"] == [jobs_coverage.PROBLEM_TASK_MISSING]

    def test_plain_job_ignores_the_principal(self, monkeypatch):
        # A normal job is *expected* to be Interactive only - flagging it
        # would fire on all 20 jobs.
        monkeypatch.setattr(
            jobs_coverage, "behavioural_coverage", lambda job, now=None: ([], None)
        )
        result = jobs_coverage.coverage_for(
            self._job(),
            {"\\AppLauncher\\backup": True},
            principals={"\\AppLauncher\\backup": False},
        )
        assert result["state"] == jobs_coverage.STATE_OK
