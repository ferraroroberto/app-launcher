"""Missed-fire coverage for scheduled jobs (issue #697).

``alert_on_failure`` catches a run that failed and ``is_stuck`` catches one
that never ended; nothing caught one that never *started*. These tests pin
both halves of ``src.jobs_coverage`` — the structural check (a job whose Task
Scheduler entry is missing/disabled) and the behavioural one (an elapsed slot
with no run record) — plus, crucially, the never-flag rules that keep it from
crying wolf across a normal week.
"""

from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src import jobs as jobs_mod
from src import jobs_coverage as cov
from src import jobs_history as jobs_history_mod
from src import jobs_schtasks as jobs_schtasks_mod
from src.jobs_config import Job, Schedule


NOW = datetime(2026, 8, 1, 12, 0, 0)


@pytest.fixture
def runs_root(tmp_path, monkeypatch):
    # JOBS_RUNS_DIR is owned by src.jobs_history (issue #315 split) — patch
    # there, not on the src.jobs facade.
    monkeypatch.setattr(jobs_history_mod, "JOBS_RUNS_DIR", tmp_path)
    cov.invalidate_coverage_cache()
    yield tmp_path
    cov.invalidate_coverage_cache()


def _seed_run(runs_root, job_id, run_id, **fields):
    rd = runs_root / job_id / run_id
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "run.json").write_text(
        json.dumps({"run_id": run_id, "job_id": job_id, **fields}),
        encoding="utf-8",
    )
    return rd


def _daily_job(**kw):
    defaults = dict(
        id="demo",
        name="Demo",
        script_path="C:\\x\\s.py",
        schedule=Schedule(type="daily", at="09:00"),
        # Old enough that the 3-day window is never clamped by added_at.
        added_at="2026-07-01T00:00:00",
    )
    defaults.update(kw)
    return Job(**defaults)


def _states(*names, enabled=True):
    return {name: enabled for name in names}


# ------------------------------------------------------------- structural


class TestStructuralCheck:
    def test_registered_and_enabled_is_ok(self, runs_root):
        job = _daily_job()
        # Three days of 09:00 slots, all covered.
        for day in (29, 30, 31):
            _seed_run(
                runs_root, "demo", f"2026073{day}",
                started_at=f"2026-07-{day}T09:00:05", status="success",
            )
        _seed_run(
            runs_root, "demo", "20260801",
            started_at="2026-08-01T09:00:04", status="success",
        )
        result = cov.coverage_for(job, _states("\\AppLauncher\\demo"), now=NOW)
        assert result["state"] == cov.STATE_OK

    def test_missing_task_flagged_without_waiting_for_the_slot(self, runs_root):
        """The real incident: a launcher + 'runs weekly' docs, no task at all.

        Flagged from the *structural* half alone — the seeded runs cover every
        elapsed slot, so nothing behavioural fires here.
        """
        job = _daily_job()
        for day in (29, 30, 31):
            _seed_run(
                runs_root, "demo", f"2026073{day}",
                started_at=f"2026-07-{day}T09:00:05", status="success",
            )
        _seed_run(
            runs_root, "demo", "20260801",
            started_at="2026-08-01T09:00:04", status="success",
        )
        result = cov.coverage_for(job, {}, now=NOW)
        assert result["state"] == cov.STATE_PROBLEM
        assert cov.PROBLEM_TASK_MISSING in result["problems"]
        assert cov.PROBLEM_MISSED_FIRE not in result["problems"]
        assert result["missing_tasks"] == ["\\AppLauncher\\demo"]

    def test_disabled_task_flagged(self, runs_root):
        job = _daily_job()
        for day in (29, 30, 31):
            _seed_run(
                runs_root, "demo", f"2026073{day}",
                started_at=f"2026-07-{day}T09:00:05", status="success",
            )
        _seed_run(
            runs_root, "demo", "20260801",
            started_at="2026-08-01T09:00:04", status="success",
        )
        result = cov.coverage_for(
            job, _states("\\AppLauncher\\demo", enabled=False), now=NOW
        )
        assert result["state"] == cov.STATE_PROBLEM
        assert result["problems"] == [cov.PROBLEM_TASK_DISABLED]

    def test_unreadable_enabled_state_is_not_a_problem(self, runs_root):
        """Registered but state unparseable → the task demonstrably exists."""
        job = _daily_job()
        for day in (29, 30, 31):
            _seed_run(
                runs_root, "demo", f"2026073{day}",
                started_at=f"2026-07-{day}T09:00:05", status="success",
            )
        _seed_run(
            runs_root, "demo", "20260801",
            started_at="2026-08-01T09:00:04", status="success",
        )
        result = cov.coverage_for(
            job, {"\\AppLauncher\\demo": None}, now=NOW
        )
        assert result["state"] == cov.STATE_OK

    def test_failed_query_is_unknown_not_missing(self, runs_root):
        """A failed schtasks query must never flag every job as missing."""
        job = _daily_job()
        for day in (29, 30, 31):
            _seed_run(
                runs_root, "demo", f"2026073{day}",
                started_at=f"2026-07-{day}T09:00:05", status="success",
            )
        _seed_run(
            runs_root, "demo", "20260801",
            started_at="2026-08-01T09:00:04", status="success",
        )
        result = cov.coverage_for(job, None, now=NOW)
        assert result["state"] == cov.STATE_UNKNOWN
        assert result["missing_tasks"] == []

    def test_daily_times_expects_every_fan_out_task(self, runs_root):
        job = _daily_job(schedule=Schedule(type="daily_times", at=["09:00", "18:00"]))
        result = cov.coverage_for(job, _states("\\AppLauncher\\demo-1"), now=NOW)
        assert cov.PROBLEM_TASK_MISSING in result["problems"]
        assert result["missing_tasks"] == ["\\AppLauncher\\demo-2"]


# ------------------------------------------------------------ behavioural


class TestMissedFires:
    def test_elapsed_slot_with_no_run_is_missed(self, runs_root):
        job = _daily_job()
        missed = cov.missed_fires(job, now=NOW)
        # 3-day window ending 15 min before NOW → 2026-07-30/31 + 08-01 09:00.
        assert [m.isoformat(timespec="minutes") for m in missed] == [
            "2026-07-30T09:00",
            "2026-07-31T09:00",
            "2026-08-01T09:00",
        ]

    def test_run_within_grace_counts_as_fired(self, runs_root):
        job = _daily_job()
        for day, month in ((30, 7), (31, 7), (1, 8)):
            _seed_run(
                runs_root, "demo", f"r{month}{day}",
                started_at=f"2026-0{month}-{day:02d}T09:12:00", status="success",
            )
        assert cov.missed_fires(job, now=NOW) == []

    def test_skipped_run_counts_as_fired(self, runs_root):
        """A cooldown-skipped run *did* fire; it just declined to do work."""
        job = _daily_job()
        for day, month in ((30, 7), (31, 7), (1, 8)):
            _seed_run(
                runs_root, "demo", f"r{month}{day}",
                started_at=f"2026-0{month}-{day:02d}T09:00:02", status="skipped",
            )
        assert cov.missed_fires(job, now=NOW) == []

    def test_slot_inside_the_grace_window_is_not_yet_missed(self, runs_root):
        job = _daily_job(schedule=Schedule(type="daily", at="11:55"))
        # 11:55 is 5 min before NOW — inside the 15 min grace.
        assert all(
            m.isoformat(timespec="minutes") != "2026-08-01T11:55"
            for m in cov.missed_fires(job, now=NOW)
        )

    def test_frequent_schedules_are_not_enumerated(self, runs_root):
        job = _daily_job(schedule=Schedule(type="minutes", every=5))
        assert cov.missed_fires(job, now=NOW) == []

    def test_window_clamped_by_added_at(self, runs_root):
        """A job added this morning has no missed slots from last week."""
        job = _daily_job(added_at="2026-08-01T11:00:00")
        assert cov.missed_fires(job, now=NOW) == []

    def test_window_clamped_by_pruned_history(self, runs_root):
        """A full 20-run history proves nothing about older slots.

        Every retained run sits today, so slots before the oldest retained
        run must not be reported as missed — they may simply have been pruned.
        """
        job = _daily_job(schedule=Schedule(type="daily_times", at=["09:00", "18:00"]))
        for i in range(jobs_history_mod.MAX_RUNS_PER_JOB):
            _seed_run(
                runs_root, "demo", f"keep{i:02d}",
                started_at=f"2026-08-01T09:{i:02d}:00", status="success",
            )
        assert cov.missed_fires(job, now=NOW) == []

    def test_paused_job_is_exempt(self, runs_root):
        job = _daily_job(
            schedule=Schedule(type="none"),
            paused_schedule=Schedule(type="daily", at="09:00"),
        )
        result = cov.coverage_for(job, {}, now=NOW)
        assert result["state"] == cov.STATE_EXEMPT

    def test_schedule_none_job_is_exempt(self, runs_root):
        job = _daily_job(schedule=Schedule(type="none"))
        result = cov.coverage_for(job, {}, now=NOW)
        assert result["state"] == cov.STATE_EXEMPT

    def test_missed_fire_reported_even_when_task_is_registered(self, runs_root):
        job = _daily_job()
        result = cov.coverage_for(job, _states("\\AppLauncher\\demo"), now=NOW)
        assert result["state"] == cov.STATE_PROBLEM
        assert result["problems"] == [cov.PROBLEM_MISSED_FIRE]
        assert result["missed_count"] == 3
        assert result["missed_fires"][-1] == "2026-08-01T09:00"


# ------------------------------------------------------------------ scan


class TestScanCoverage:
    def test_one_batched_schtasks_query_for_the_whole_scan(
        self, runs_root, monkeypatch
    ):
        """Acceptance: no shell-out storm — batch once per cycle."""
        jobs_schtasks_mod.invalidate_next_run_cache()
        calls = []

        def runner(argv):
            calls.append(argv)
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    "TaskName: \\AppLauncher\\a\nScheduled Task State: Enabled\n\n"
                    "TaskName: \\AppLauncher\\b\nScheduled Task State: Enabled\n\n"
                ),
                stderr="",
            )

        monkeypatch.setattr(jobs_schtasks_mod, "_run_schtasks", runner)
        jobs = [
            _daily_job(id="a", name="A"),
            _daily_job(id="b", name="B"),
            _daily_job(id="c", name="C"),
        ]
        results = cov.scan_coverage(jobs, now=NOW)
        assert len(calls) == 1
        assert set(results) == {"a", "b", "c"}
        assert cov.PROBLEM_TASK_MISSING in results["c"]["problems"]
        assert cov.PROBLEM_TASK_MISSING not in results["a"]["problems"]
        jobs_schtasks_mod.invalidate_next_run_cache()


class TestSchtasksStateParsing:
    def test_status_key_fallback(self):
        records = jobs_schtasks_mod._parse_bulk_records(
            "TaskName: \\AppLauncher\\a\nStatus: Ready\n\n"
            "TaskName: \\AppLauncher\\b\nStatus: Disabled\n\n"
            "TaskName: \\AppLauncher\\c\nNext Run Time: N/A\n\n"
        )
        assert records["\\AppLauncher\\a"]["enabled"] is True
        assert records["\\AppLauncher\\b"]["enabled"] is False
        # Neither state key present → unknown, never a confident False.
        assert records["\\AppLauncher\\c"]["enabled"] is None

    def test_failed_query_returns_none_not_empty(self, monkeypatch):
        jobs_schtasks_mod.invalidate_next_run_cache()
        monkeypatch.setattr(
            jobs_schtasks_mod,
            "_run_schtasks",
            lambda argv: SimpleNamespace(returncode=1, stdout="", stderr="boom"),
        )
        assert jobs_schtasks_mod.registered_task_states() is None
        jobs_schtasks_mod.invalidate_next_run_cache()

    def test_next_run_view_still_matches_legacy_shape(self):
        out = jobs_schtasks_mod._parse_bulk_query(
            "TaskName: \\AppLauncher\\a\nNext Run Time: 2026-08-02 09:00:00\n\n"
        )
        assert out == {"\\AppLauncher\\a": "2026-08-02 09:00:00"}


# -------------------------------------------------------------- alerting


class TestCheckAndAlert:
    def _cfg(self, **kw):
        defaults = dict(
            pushover_api_token="",
            pushover_user_key="",
            notify_on_failure=False,
            notify_failure_streak=0,
            notify_failure_summary=False,
            telegram_bot_token="tok",
            telegram_chat_id="chat",
        )
        defaults.update(kw)
        return SimpleNamespace(**defaults)

    def test_alerts_opted_in_job_once_then_dedupes(self, runs_root, monkeypatch):
        monkeypatch.setattr(
            cov, "registered_task_states", lambda *a, **k: {}
        )
        job = _daily_job(alert_on_failure=True)
        telegram = MagicMock()
        first = cov.check_and_alert(
            self._cfg(), jobs=[job], now=NOW, telegram_notifier=telegram
        )
        assert first == ["demo"]
        telegram.notify.assert_called_once()
        title, body = telegram.notify.call_args.args[:2]
        assert "Demo" in title
        assert "demo" in body

        # Same problem on the next cycle → no second ping.
        second = cov.check_and_alert(
            self._cfg(), jobs=[job], now=NOW, telegram_notifier=telegram
        )
        assert second == []
        assert telegram.notify.call_count == 1

    def test_no_alert_when_job_flag_off(self, runs_root, monkeypatch):
        monkeypatch.setattr(
            cov, "registered_task_states", lambda *a, **k: {}
        )
        job = _daily_job()
        telegram = MagicMock()
        cov.check_and_alert(
            self._cfg(), jobs=[job], now=NOW, telegram_notifier=telegram
        )
        telegram.notify.assert_not_called()

    def test_recovery_clears_state_so_the_next_break_re_alerts(
        self, runs_root, monkeypatch
    ):
        job = _daily_job(alert_on_failure=True)
        telegram = MagicMock()
        # One mutable box so each phase swaps behaviour without stacking
        # (and later undoing) monkeypatches.
        registered = {"value": {}}
        missed = {"value": None}
        monkeypatch.setattr(
            cov, "registered_task_states", lambda *a, **k: registered["value"]
        )
        real_missed_fires = cov.missed_fires
        monkeypatch.setattr(
            cov,
            "missed_fires",
            lambda j, **k: (
                missed["value"]
                if missed["value"] is not None
                else real_missed_fires(j, **k)
            ),
        )

        cov.check_and_alert(
            self._cfg(), jobs=[job], now=NOW, telegram_notifier=telegram
        )
        assert telegram.notify.call_count == 1

        # Coverage recovers: task back, every slot covered.
        registered["value"] = {"\\AppLauncher\\demo": True}
        missed["value"] = []
        cov.check_and_alert(
            self._cfg(), jobs=[job], now=NOW, telegram_notifier=telegram
        )
        assert telegram.notify.call_count == 1
        assert json.loads(
            cov.coverage_alerts_path().read_text(encoding="utf-8")
        ) == {}

        # Breaks again → alerts immediately, not after the repeat window.
        registered["value"] = {}
        missed["value"] = None
        cov.check_and_alert(
            self._cfg(), jobs=[job], now=NOW, telegram_notifier=telegram
        )
        assert telegram.notify.call_count == 2

    def test_never_raises_on_a_broken_notifier(self, runs_root, monkeypatch):
        monkeypatch.setattr(cov, "registered_task_states", lambda *a, **k: {})
        job = _daily_job(alert_on_failure=True)
        boom = MagicMock()
        boom.notify.side_effect = RuntimeError("telegram down")
        assert cov.check_and_alert(
            self._cfg(), jobs=[job], now=NOW, telegram_notifier=boom
        ) == []

    def test_pushover_channel_gated_by_global_switch(self, runs_root, monkeypatch):
        monkeypatch.setattr(cov, "registered_task_states", lambda *a, **k: {})
        push = MagicMock()
        # Distinct job ids: the de-dup state is keyed by job, so reusing one
        # would suppress the second cycle for the wrong reason.
        cov.check_and_alert(
            self._cfg(notify_on_failure=False),
            jobs=[_daily_job(id="off", name="Off")],
            now=NOW,
            notifier=push,
        )
        push.notify.assert_not_called()
        cov.check_and_alert(
            self._cfg(notify_on_failure=True),
            jobs=[_daily_job(id="on", name="On")],
            now=NOW,
            notifier=push,
        )
        push.notify.assert_called_once()


class TestFacadeReExport:
    def test_coverage_helpers_reachable_from_src_jobs(self):
        assert jobs_mod.scan_coverage is cov.scan_coverage
        assert jobs_mod.coverage_for_job is cov.coverage_for_job
        assert jobs_mod.invalidate_coverage_cache is cov.invalidate_coverage_cache
