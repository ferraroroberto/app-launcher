"""A trigger pinned to a UTC offset drifts an hour at daylight-saving changes.

``New-ScheduledTaskTrigger`` stamps ``StartBoundary`` as an absolute instant
(``…Z`` / ``…+01:00``), which Task Scheduler honours, so a session-less task
registered in summer fires an hour off the wall clock after the autumn
change. These tests pin both halves: the generated registration script
re-renders every trigger as an offset-free local timestamp, and the coverage
scan names a pinned entry for what it is instead of reporting missed fires.
"""

from __future__ import annotations

import subprocess
from datetime import datetime
from typing import List

import pytest

from src import jobs as jobs_mod
from src.jobs import coverage as jobs_coverage
from src.jobs import schtasks as jobs_schtasks
from src.jobs import schtasks_offsets
from src.jobs.config import Job, Schedule

_TASK = "\\AppLauncher\\backup"


def _job(schedule: Schedule, **kw) -> Job:
    return Job(
        id="backup",
        name="Backup",
        script_path="C:\\stub\\backup.py",
        schedule=schedule,
        **kw,
    )


@pytest.mark.parametrize(
    "schedule",
    [
        Schedule(type="daily", at="03:00"),
        Schedule(type="daily_times", at=["06:00", "18:00"]),
        Schedule(type="weekly", day="MON", at="09:00"),
        Schedule(type="once", at="2026-12-01T14:30"),
        Schedule(type="minutes", every=15),
        Schedule(type="hourly", every=2),
    ],
    ids=lambda s: s.type,
)
def test_every_registered_trigger_is_normalised_to_local_time(schedule):
    script = jobs_mod.registration_script(_job(schedule, session_less=True))
    assert script is not None
    lines = script.splitlines()
    registrations = [i for i, l in enumerate(lines) if l.startswith("Register-")]
    assert registrations
    for i in registrations:
        # The offset-free assignment sits directly between building the
        # trigger and registering it, and the registration reuses that object.
        assert lines[i - 1] == jobs_schtasks._PS_LOCAL_START_BOUNDARY
        assert lines[i - 2].startswith("$trigger = New-ScheduledTaskTrigger")
        assert "-Trigger $trigger " in lines[i]
    assert "'yyyy-MM-ddTHH:mm:ss'" in jobs_schtasks._PS_LOCAL_START_BOUNDARY


_XML = """\
<Tasks>

<!-- \\AppLauncher\\pinned -->

<Task version="1.2">
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>2026-08-21T02:00:00+01:00</StartBoundary>
    </CalendarTrigger>
  </Triggers>
</Task>

<!-- \\AppLauncher\\utc -->

<Task version="1.2">
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>2026-08-21T01:00:00Z</StartBoundary>
    </CalendarTrigger>
  </Triggers>
</Task>

<!-- \\AppLauncher\\local -->

<Task version="1.2">
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>2026-10-29T03:00:00</StartBoundary>
    </CalendarTrigger>
  </Triggers>
</Task>
</Tasks>
"""


def test_xml_dump_flags_only_triggers_carrying_an_offset():
    assert schtasks_offsets._parse_trigger_offsets(_XML) == {
        "\\AppLauncher\\pinned": True,
        "\\AppLauncher\\utc": True,
        "\\AppLauncher\\local": False,
    }


def _runner(calls: List[List[str]], *, rc: int = 0, stdout: str = _XML):
    def run(argv):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, rc, stdout=stdout, stderr="")

    return run


def test_offset_pins_query_is_cached_and_reports_failure_as_none():
    jobs_schtasks.invalidate_next_run_cache()
    calls: List[List[str]] = []
    pins = schtasks_offsets.registered_task_offset_pins(_runner(calls))
    assert pins is not None and pins["\\AppLauncher\\pinned"] is True
    schtasks_offsets.registered_task_offset_pins(_runner(calls))
    assert len(calls) == 1

    jobs_schtasks.invalidate_next_run_cache()
    assert (
        schtasks_offsets.registered_task_offset_pins(_runner([], rc=1, stdout="")) is None
    )
    jobs_schtasks.invalidate_next_run_cache()


class TestCoverageOffsetPin:
    @pytest.fixture(autouse=True)
    def _no_history(self, monkeypatch):
        self.missed = [datetime(2026, 10, 29, 3, 0)]
        monkeypatch.setattr(
            jobs_coverage,
            "behavioural_coverage",
            lambda job, now=None: (list(self.missed), None),
        )

    def _cover(self, job: Job, **kw):
        return jobs_coverage.coverage_for(job, {_TASK: True}, **kw)

    def test_pinned_entry_is_its_own_problem_not_missed_fires(self):
        result = self._cover(
            _job(Schedule(type="daily", at="03:00")), offset_pins={_TASK: True}
        )
        assert result["state"] == jobs_coverage.STATE_PROBLEM
        assert result["problems"] == [jobs_coverage.PROBLEM_OFFSET_PINNED_TRIGGER]
        assert result["offset_pinned_tasks"] == [_TASK]
        assert result["missed_count"] == 0
        assert "UTC offset" in result["detail"]

    def test_offset_free_entry_keeps_reporting_a_real_missed_fire(self):
        result = self._cover(
            _job(Schedule(type="daily", at="03:00")), offset_pins={_TASK: False}
        )
        assert result["problems"] == [jobs_coverage.PROBLEM_MISSED_FIRE]

    def test_failed_xml_query_is_unknown_for_a_session_less_job(self):
        self.missed = []
        result = self._cover(
            _job(Schedule(type="daily", at="03:00"), session_less=True),
            offset_pins=None,
        )
        assert result["state"] == jobs_coverage.STATE_UNKNOWN

    def test_failed_xml_query_does_not_unsettle_a_plain_job(self):
        self.missed = []
        result = self._cover(_job(Schedule(type="daily", at="03:00")), offset_pins=None)
        assert result["state"] == jobs_coverage.STATE_OK

    def test_announced_as_a_warning_in_its_own_words(self):
        note = jobs_coverage.announcement_for(
            [
                jobs_coverage.PROBLEM_MISSED_FIRE,
                jobs_coverage.PROBLEM_OFFSET_PINNED_TRIGGER,
            ]
        )
        assert "pinned to a UTC offset" in note.title
        assert note.severity == "warning"
