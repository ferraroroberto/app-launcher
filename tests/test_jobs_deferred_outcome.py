"""A job-declared ``deferred`` exit code (issue #1316).

life-os's nightly email sweep exits 3 when it has mail to sweep but runs in
session 0, where starting Outlook would strand a hidden ``OUTLOOK.EXE``. That
deferral is by design, yet the Jobs card drew it as a red failure and alerted
every night. A job now declares what its own codes mean
(``"exit_outcomes": {"3": "deferred"}``), derived at read time like
``unconfirmed``.

Acceptance, one class each:

* a run of a declaring job that exits 3 reads ``deferred`` and raises no alert;
* the same job exiting 1 or 2 still reads ``failed`` and alerts;
* a job with no declaration is unchanged.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src import board
from src import jobs_history
from src import jobs as jobs_mod
from src import jobs_config as jc
from src import notifications as notif
from src.jobs_config import Job, JobsConfig, job_from_dict, save_jobs
from src.jobs_outcome import (
    OUTCOME_DEFERRED,
    OUTCOME_FAILED,
    OUTCOME_UNCONFIRMED,
    run_outcome,
)
from src.jobs_stats import consecutive_failed_runs, run_stats

SWEEP_ID = "email-sweep"
DECLARED = {3: OUTCOME_DEFERRED}


def _sweep(**kw) -> Job:
    return Job(
        id=SWEEP_ID, name="Email Sweep", script_path="C:\\x\\sweep.bat",
        alert_on_failure=True, exit_outcomes=dict(DECLARED), **kw,
    )


def _plain(job_id: str = "plain") -> Job:
    return Job(id=job_id, name="Plain", script_path="C:\\x\\plain.bat",
               alert_on_failure=True)


@pytest.fixture
def registry(tmp_path, monkeypatch):
    """A registry holding the declaring job and an undeclared neighbour."""
    monkeypatch.setattr(jc, "DEFAULT_JOBS_PATH", tmp_path / "jobs.json")
    monkeypatch.setattr(jobs_history, "JOBS_RUNS_DIR", tmp_path / "runs")
    save_jobs(JobsConfig(jobs=[_sweep(), _plain()]))
    return tmp_path


def _write_run(job_id: str, run_id: str, *, exit_code: int, status: str = "failed",
               minutes_ago: int = 5):
    finished = datetime.now() - timedelta(minutes=minutes_ago)
    started = finished - timedelta(seconds=4)
    rd = jobs_mod.new_run_dir(job_id, run_id)
    jobs_mod.write_run_json(
        rd, run_id=rd.name, status=status, exit_code=exit_code,
        started_at=started.isoformat(timespec="seconds"),
        finished_at=finished.isoformat(timespec="seconds"),
        duration_seconds=4.0,
    )
    return rd


# ------------------------------------------------------------ classification


class TestRunOutcome:
    def test_declared_code_is_deferred(self):
        outcome, reason = run_outcome({"status": "failed", "exit_code": 3}, DECLARED)
        assert outcome == OUTCOME_DEFERRED
        assert reason and "declared deferred" in reason

    @pytest.mark.parametrize("code", [1, 2])
    def test_other_codes_of_the_same_job_stay_failed(self, code):
        assert run_outcome({"status": "failed", "exit_code": code}, DECLARED)[0] \
            == OUTCOME_FAILED

    def test_undeclared_job_is_unchanged(self):
        assert run_outcome({"status": "failed", "exit_code": 3})[0] == OUTCOME_FAILED
        assert run_outcome({"status": "failed", "exit_code": 3}, {})[0] \
            == OUTCOME_FAILED

    @pytest.mark.parametrize("field", ["killed", "watchdog", "reaped"])
    def test_launcher_terminated_run_is_never_deferred(self, field):
        record = {"status": "failed", "exit_code": 3, field: True}
        assert run_outcome(record, DECLARED)[0] == OUTCOME_FAILED

    def test_success_and_in_flight_are_never_reinterpreted(self):
        for status in ("success", "running", "pending", "queued"):
            assert run_outcome({"status": status, "exit_code": 3}, DECLARED)[0] \
                == status

    def test_declaration_does_not_disturb_adapter_codes(self):
        # 122 is the adapter's "not confirmed"; a job declaring only 3 keeps it.
        assert run_outcome({"status": "failed", "exit_code": 122}, DECLARED)[0] \
            == OUTCOME_UNCONFIRMED


# ------------------------------------------------------------------ config


class TestDeclarationConfig:
    def test_round_trips_as_string_keys(self):
        payload = _sweep().to_dict()
        assert payload["exit_outcomes"] == {"3": "deferred"}
        assert job_from_dict(payload).exit_outcomes == DECLARED

    def test_absent_declaration_is_omitted(self):
        assert "exit_outcomes" not in _plain().to_dict()
        assert job_from_dict(_plain().to_dict()).exit_outcomes == {}

    @pytest.mark.parametrize("bad", [
        {"3": "success"},      # a success is exit 0, not a declaration
        {"3": "failed"},       # failed is already the default
        {"0": "deferred"},     # 0 always means success
        {"three": "deferred"},
        ["3", "deferred"],
    ])
    def test_rejects_malformed_declarations(self, bad):
        with pytest.raises(ValueError):
            job_from_dict({"id": "j", "name": "J", "script_path": "C:\\x\\s.py",
                           "exit_outcomes": bad})


# ------------------------------------------------- read path (card + stats)


class TestReadPath:
    def test_card_reads_deferred_for_the_declaring_job(self, registry):
        _write_run(SWEEP_ID, "20260929T020000", exit_code=3)
        latest = jobs_mod.latest_run(SWEEP_ID)
        assert latest["status"] == "failed"  # on disk, unchanged
        assert latest["outcome"] == OUTCOME_DEFERRED

    @pytest.mark.parametrize("code", [1, 2])
    def test_card_still_reads_failed_for_real_failures(self, registry, code):
        _write_run(SWEEP_ID, "20260929T020000", exit_code=code)
        assert jobs_mod.latest_run(SWEEP_ID)["outcome"] == OUTCOME_FAILED

    def test_undeclared_job_exit_3_still_reads_failed(self, registry):
        _write_run("plain", "20260929T020000", exit_code=3)
        assert jobs_mod.latest_run("plain")["outcome"] == OUTCOME_FAILED

    def test_historical_runs_re_render_when_the_declaration_lands(
        self, registry, monkeypatch
    ):
        """Derived at read time: no migration of the runs already on disk."""
        save_jobs(JobsConfig(jobs=[_plain(SWEEP_ID)]))
        _write_run(SWEEP_ID, "20260929T020000", exit_code=3)
        assert jobs_mod.latest_run(SWEEP_ID)["outcome"] == OUTCOME_FAILED
        save_jobs(JobsConfig(jobs=[_sweep()]))
        assert jobs_mod.latest_run(SWEEP_ID)["outcome"] == OUTCOME_DEFERRED

    def test_deferred_is_out_of_the_success_rate_and_durations(self, registry):
        _write_run(SWEEP_ID, "20260926T020000", exit_code=0, status="success",
                   minutes_ago=60 * 72)
        _write_run(SWEEP_ID, "20260927T020000", exit_code=3, minutes_ago=60 * 48)
        _write_run(SWEEP_ID, "20260928T020000", exit_code=3, minutes_ago=60 * 24)
        stats = run_stats(SWEEP_ID, fresh=True)
        assert stats["success_rate_30d"] == 1.0
        assert stats["completed_count"] == 1
        # Still on the card: the sparkline carries every run.
        assert [e["outcome"] for e in stats["last7"]] == [
            "success", OUTCOME_DEFERRED, OUTCOME_DEFERRED,
        ]

    def test_deferred_breaks_a_failure_streak(self, registry):
        _write_run(SWEEP_ID, "20260927T020000", exit_code=1, minutes_ago=60 * 48)
        _write_run(SWEEP_ID, "20260928T020000", exit_code=3, minutes_ago=60 * 24)
        assert consecutive_failed_runs(SWEEP_ID) == 0

    def test_board_raises_no_attention_card_for_a_deferral(
        self, registry, monkeypatch
    ):
        monkeypatch.setattr(jobs_mod, "reap_stranded_runs", lambda job: None)
        _write_run(SWEEP_ID, "20260929T020000", exit_code=3, minutes_ago=0)
        _write_run("plain", "20260929T020000", exit_code=3, minutes_ago=0)
        states = {c["job_id"]: c["state"] for c in board.jobs_attention()}
        assert SWEEP_ID not in states
        assert states["plain"] == "failed"


# ------------------------------------------------------------------ alerts


class TestAlerts:
    def _cfg(self):
        return SimpleNamespace(
            pushover_api_token="", pushover_user_key="",
            notify_on_failure=True, notify_failure_streak=0,
            notify_failure_summary=False,
            telegram_bot_token="tok", telegram_chat_id="chat",
        )

    def _notify(self, registry, code):
        rd = _write_run(SWEEP_ID, "20260929T020000", exit_code=code)
        pushover, telegram = MagicMock(), MagicMock()
        notif.notify_failure(
            self._cfg(), _sweep(), rd, status="failed", exit_code=code,
            notifier=pushover, telegram_notifier=telegram,
        )
        return pushover, telegram

    def test_deferral_sends_no_alert_on_either_channel(self, registry):
        pushover, telegram = self._notify(registry, 3)
        pushover.notify.assert_not_called()
        telegram.notify.assert_not_called()

    @pytest.mark.parametrize("code", [1, 2])
    def test_real_failure_still_alerts(self, registry, code):
        pushover, telegram = self._notify(registry, code)
        telegram.notify.assert_called_once()
        assert "failed" in telegram.notify.call_args.args[0]
        pushover.notify.assert_called_once()
