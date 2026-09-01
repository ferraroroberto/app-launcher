"""Unit tests for the executor's last-resort running-forever watchdog
(issue #695).

Three layers, matching the three places the feature lives:

* :class:`~app.cli.commands.run_job_supervision.RunWatchdog` itself — pointed at
  a real child process, with the poll interval monkeypatched down so a
  test costs a fraction of a second instead of the production 5 s tick.
* :func:`~app.cli.commands.run_job_supervision.resolve_watchdog_limits` — the
  tri-state (unset / explicit / ``0`` = off) resolution and the
  history-derived default.
* The whole ``RunJobCommand`` path — a real sleeping child killed by the
  watchdog must finalise ``failed`` with the distinct watchdog fields,
  and an ordinary fast run must gain none of them.
* :func:`~src.notifications.notify_failure` (issue #819) — the watchdog's
  ``note`` must reach the Telegram alert body, not just the run record.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.cli.commands import run_job_cmd as rjc
from app.cli.commands import run_job_supervision as sup
from src import jobs as jobs_mod
from src import jobs_history as jobs_history_mod
from src import jobs_stats as jobs_stats_mod
from src import notifications as notif
from src.jobs_config import Job, JobsConfig, job_from_dict
from src.subprocess_flags import NO_WINDOW


@pytest.fixture
def fast_watchdog(monkeypatch):
    """Tick the watchdog ~100x faster so tests don't wait out a real poll."""
    monkeypatch.setattr(sup, "_WATCHDOG_POLL_INTERVAL_SECONDS", 0.05)


@pytest.fixture
def isolated_runs_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_history_mod, "JOBS_RUNS_DIR", tmp_path)
    jobs_stats_mod.invalidate_stats_cache()
    return tmp_path


def _sleeper(seconds: float = 30.0) -> "subprocess.Popen[bytes]":
    """A child that lives long enough for the watchdog to notice it."""
    return subprocess.Popen(
        [sys.executable, "-c", f"import time; time.sleep({seconds})"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        creationflags=NO_WINDOW,
    )


def _wait_for(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


# ================================================== _RunWatchdog itself


@pytest.mark.usefixtures("fast_watchdog")
class TestRunWatchdog:
    def test_max_runtime_breach_kills_and_records_reason(self, tmp_path):
        log = tmp_path / "output.log"
        log.write_bytes(b"")
        proc = _sleeper()
        wd = sup.RunWatchdog(
            proc, log, max_runtime_seconds=0.2, no_output_seconds=None
        )
        wd.start()
        try:
            assert _wait_for(lambda: wd.fired), "watchdog never fired"
            assert wd.reason == "max_runtime"
            assert wd.note.startswith("watchdog: max runtime")
            assert _wait_for(lambda: proc.poll() is not None), "child survived"
        finally:
            wd.stop()
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=10)

    def test_no_output_breach_kills_and_records_reason(self, tmp_path):
        log = tmp_path / "output.log"
        log.write_bytes(b"started\n")
        proc = _sleeper()
        wd = sup.RunWatchdog(
            proc, log, max_runtime_seconds=None, no_output_seconds=0.2
        )
        wd.start()
        try:
            assert _wait_for(lambda: wd.fired), "watchdog never fired"
            assert wd.reason == "no_output"
            assert wd.note.startswith("watchdog: no output for")
            assert _wait_for(lambda: proc.poll() is not None), "child survived"
        finally:
            wd.stop()
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=10)

    def test_growing_output_never_trips_the_no_output_signal(self, tmp_path):
        log = tmp_path / "output.log"
        log.write_bytes(b"")
        proc = _sleeper()
        wd = sup.RunWatchdog(
            proc, log, max_runtime_seconds=None, no_output_seconds=0.4
        )
        wd.start()
        try:
            # Keep the file growing for comfortably longer than the
            # threshold — a healthy chatty run must never be killed.
            deadline = time.monotonic() + 1.5
            while time.monotonic() < deadline:
                with log.open("ab") as fh:
                    fh.write(b"tick\n")
                time.sleep(0.1)
            assert not wd.fired
            assert proc.poll() is None
        finally:
            wd.stop()
            proc.kill()
            proc.wait(timeout=10)

    def test_does_not_fire_for_a_child_that_already_exited(self, tmp_path):
        """The child finishing microseconds before the breach is not a kill."""
        log = tmp_path / "output.log"
        log.write_bytes(b"")
        proc = _sleeper(0.05)
        proc.wait(timeout=10)
        wd = sup.RunWatchdog(
            proc, log, max_runtime_seconds=0.1, no_output_seconds=0.1
        )
        wd.start()
        time.sleep(0.6)
        wd.stop()
        assert not wd.fired
        assert wd.reason is None

    def test_unarmed_watchdog_starts_no_thread(self, tmp_path):
        log = tmp_path / "output.log"
        log.write_bytes(b"")
        proc = _sleeper(0.05)
        try:
            wd = sup.RunWatchdog(
                proc, log, max_runtime_seconds=None, no_output_seconds=None
            )
            assert wd.armed is False
            wd.start()
            assert wd._thread.is_alive() is False
            wd.stop()
            assert not wd.fired
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=10)

    def test_missing_log_file_is_unknown_not_stalled(self, tmp_path):
        """An unreadable stat must not masquerade as "no growth"."""
        log = tmp_path / "never-created.log"
        proc = _sleeper()
        wd = sup.RunWatchdog(
            proc, log, max_runtime_seconds=None, no_output_seconds=0.2
        )
        wd.start()
        try:
            time.sleep(1.0)
            assert not wd.fired
            assert proc.poll() is None
        finally:
            wd.stop()
            proc.kill()
            proc.wait(timeout=10)


# ============================================ _resolve_watchdog_limits


class TestResolveWatchdogLimits:
    def _job(self, **kw) -> Job:
        return Job(id="demo", name="Demo", script_path="C:\\ok.py", **kw)

    def test_explicit_values_win(self, isolated_runs_dir):
        max_runtime, no_output = sup.resolve_watchdog_limits(
            self._job(max_runtime_seconds=120, no_output_seconds=90)
        )
        assert max_runtime == 120.0
        assert no_output == 90.0

    def test_zero_disables_each_signal_independently(self, isolated_runs_dir):
        max_runtime, no_output = sup.resolve_watchdog_limits(
            self._job(max_runtime_seconds=0, no_output_seconds=0)
        )
        assert max_runtime is None
        assert no_output is None

    def test_unset_no_output_falls_back_to_the_module_default(
        self, isolated_runs_dir
    ):
        _, no_output = sup.resolve_watchdog_limits(self._job())
        assert no_output == sup._WATCHDOG_DEFAULT_NO_OUTPUT_SECONDS

    def test_thin_history_yields_no_runtime_ceiling(self, isolated_runs_dir):
        """Two completed runs is not evidence — better no ceiling than a
        fabricated one that kills a healthy first-of-its-kind run."""
        for i in range(2):
            rd = jobs_mod.new_run_dir("demo", f"2026010{i + 1}T060000")
            jobs_mod.write_run_json(
                rd,
                status="success",
                started_at=f"2026-01-0{i + 1}T06:00:00",
                finished_at=f"2026-01-0{i + 1}T06:00:10",
            )
        jobs_stats_mod.invalidate_stats_cache()
        max_runtime, _ = sup.resolve_watchdog_limits(self._job())
        assert max_runtime is None

    def test_enough_history_derives_the_is_stuck_threshold(
        self, isolated_runs_dir
    ):
        for i in range(1, 7):
            rd = jobs_mod.new_run_dir("demo", f"2026010{i}T060000")
            jobs_mod.write_run_json(
                rd,
                status="success",
                started_at=f"2026-01-0{i}T06:00:00",
                finished_at=f"2026-01-0{i}T06:00:10",
            )
        jobs_stats_mod.invalidate_stats_cache()
        max_runtime, _ = sup.resolve_watchdog_limits(self._job())
        # 10 s runs → p95 × 3 = 30 s, so the 300 s floor is what binds.
        assert max_runtime == 300.0
        assert max_runtime == jobs_stats_mod.stuck_threshold_seconds("demo")


# ============================================== jobs_stats derivation


class TestDerivedRuntimeCeiling:
    def test_returns_none_without_enough_completed_runs(self, isolated_runs_dir):
        assert jobs_stats_mod.derived_runtime_ceiling_seconds("demo") is None

    def test_long_history_scales_past_the_floor(self, isolated_runs_dir):
        # Six 10-minute runs → p95 × 3 = 1800 s, well past the 300 s floor.
        for i in range(1, 7):
            rd = jobs_mod.new_run_dir("demo", f"2026010{i}T060000")
            jobs_mod.write_run_json(
                rd,
                status="success",
                started_at=f"2026-01-0{i}T06:00:00",
                finished_at=f"2026-01-0{i}T06:10:00",
            )
        jobs_stats_mod.invalidate_stats_cache()
        assert jobs_stats_mod.derived_runtime_ceiling_seconds("demo") == 1800.0


# ================================================= Job field validation


class TestJobWatchdogFields:
    def test_defaults_to_unset_and_omitted_from_to_dict(self):
        job = Job(id="d", name="D", script_path="C:\\ok.py")
        assert job.max_runtime_seconds is None
        assert job.no_output_seconds is None
        assert "max_runtime_seconds" not in job.to_dict()
        assert "no_output_seconds" not in job.to_dict()

    def test_explicit_zero_survives_a_round_trip(self):
        """``0`` means "off" and must not collapse to "unset" the way
        ``cooldown_seconds`` does."""
        job = job_from_dict(
            {
                "id": "d",
                "name": "D",
                "script_path": "C:\\ok.py",
                "max_runtime_seconds": 0,
                "no_output_seconds": 0,
            }
        )
        assert job.max_runtime_seconds == 0
        assert job.no_output_seconds == 0
        payload = job.to_dict()
        assert payload["max_runtime_seconds"] == 0
        assert payload["no_output_seconds"] == 0
        assert job_from_dict(payload).max_runtime_seconds == 0

    @pytest.mark.parametrize(
        "value", [-1, True, "600", 1.5, 7 * 86_400 + 1]
    )
    def test_rejects_bad_values(self, value):
        with pytest.raises(ValueError):
            job_from_dict(
                {
                    "id": "d",
                    "name": "D",
                    "script_path": "C:\\ok.py",
                    "max_runtime_seconds": value,
                }
            )

    def test_accepts_the_upper_bound(self):
        job = job_from_dict(
            {
                "id": "d",
                "name": "D",
                "script_path": "C:\\ok.py",
                "no_output_seconds": 7 * 86_400,
            }
        )
        assert job.no_output_seconds == 7 * 86_400


# ==================================================== executor end-to-end


class TestExecutorWatchdogIntegration:
    """Drive a real child through ``RunJobCommand`` and check ``run.json``."""

    def _wire(self, monkeypatch, job: Job) -> None:
        monkeypatch.setattr(rjc, "load_jobs", lambda: JobsConfig(jobs=[job]))
        monkeypatch.setattr(
            "src.jobs_config.load_jobs", lambda: JobsConfig(jobs=[job])
        )
        monkeypatch.setattr(
            rjc,
            "load_webapp_config",
            lambda: SimpleNamespace(
                pushover_api_token="",
                pushover_user_key="",
                notify_on_failure=False,
                notify_failure_streak=0,
                notify_failure_summary=False,
            ),
        )

    def _run(self, job: Job) -> dict:
        from src.app_config import AppConfig

        cmd = rjc.RunJobCommand(AppConfig())
        rc = cmd.execute(
            SimpleNamespace(job_id=job.id, trigger="manual", run_id=None)
        )
        runs = jobs_mod.list_runs(job.id)
        assert len(runs) == 1
        return {"rc": rc, "record": runs[0]}

    def test_max_runtime_breach_finalises_with_the_watchdog_reason(
        self, isolated_runs_dir, tmp_path, monkeypatch, fast_watchdog
    ):
        script = tmp_path / "sleeper.py"
        script.write_text("import time; time.sleep(120)\n", encoding="utf-8")
        job = Job(
            id="wedged",
            name="Wedged",
            script_path=str(script),
            max_runtime_seconds=1,
            no_output_seconds=0,
        )
        self._wire(monkeypatch, job)

        outcome = self._run(job)
        record = outcome["record"]
        assert outcome["rc"] == 1
        assert record["status"] == "failed"
        assert record["watchdog"] is True
        assert record["watchdog_reason"] == "max_runtime"
        assert record["note"].startswith("watchdog: max runtime")
        # Distinct from an operator kill and from a stranded-run reap.
        assert "killed" not in record
        assert "reaped" not in record

    def test_no_output_breach_finalises_with_the_watchdog_reason(
        self, isolated_runs_dir, tmp_path, monkeypatch, fast_watchdog
    ):
        script = tmp_path / "silent.py"
        script.write_text("import time; time.sleep(120)\n", encoding="utf-8")
        job = Job(
            id="silent",
            name="Silent",
            script_path=str(script),
            max_runtime_seconds=0,
            no_output_seconds=1,
        )
        self._wire(monkeypatch, job)

        record = self._run(job)["record"]
        assert record["status"] == "failed"
        assert record["watchdog_reason"] == "no_output"

    def test_normal_run_is_untouched(
        self, isolated_runs_dir, tmp_path, monkeypatch, fast_watchdog
    ):
        script = tmp_path / "ok.py"
        script.write_text(
            "print('done', flush=True)\n", encoding="utf-8"
        )
        job = Job(
            id="fine",
            name="Fine",
            script_path=str(script),
            max_runtime_seconds=60,
            no_output_seconds=60,
        )
        self._wire(monkeypatch, job)

        outcome = self._run(job)
        record = outcome["record"]
        assert outcome["rc"] == 0
        assert record["status"] == "success"
        assert record["exit_code"] == 0
        # No new keys on the happy path — the record shape is unchanged.
        assert "watchdog" not in record
        assert "watchdog_reason" not in record
        assert "note" not in record


# ============================================ notify_failure watchdog note


class TestNotifyFailureWatchdogNote:
    """issue #819 — the Telegram alert must name the watchdog and its
    reason instead of a bare, otherwise-unproducible exit code.
    """

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

    def _run_dir(self, tmp_path, monkeypatch, job_id: str) -> Path:
        monkeypatch.setattr(jobs_history_mod, "JOBS_RUNS_DIR", tmp_path)
        rd = jobs_mod.new_run_dir(job_id, "20260901T080000")
        jobs_mod.write_run_json(rd, status="failed")
        return rd

    @pytest.mark.usefixtures("fast_watchdog")
    def test_max_runtime_note_reaches_telegram_body(self, tmp_path, monkeypatch):
        log = tmp_path / "output.log"
        log.write_bytes(b"")
        proc = _sleeper()
        wd = sup.RunWatchdog(proc, log, max_runtime_seconds=0.2, no_output_seconds=None)
        wd.start()
        try:
            assert _wait_for(lambda: wd.fired), "watchdog never fired"
        finally:
            wd.stop()
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=10)

        rd = self._run_dir(tmp_path, monkeypatch, "wedged")
        job = Job(id="wedged", name="Wedged", script_path="C:\\ok.py", alert_on_failure=True)
        telegram_notifier = MagicMock()
        notif.notify_failure(
            self._cfg(), job, rd,
            status="failed", exit_code=15, watchdog_note=wd.note,
            telegram_notifier=telegram_notifier,
        )
        telegram_notifier.notify.assert_called_once()
        _, body = telegram_notifier.notify.call_args.args[:2]
        assert wd.note in body
        assert "exit=15" in body

    @pytest.mark.usefixtures("fast_watchdog")
    def test_no_output_note_reaches_telegram_body(self, tmp_path, monkeypatch):
        log = tmp_path / "output.log"
        log.write_bytes(b"started\n")
        proc = _sleeper()
        wd = sup.RunWatchdog(proc, log, max_runtime_seconds=None, no_output_seconds=0.2)
        wd.start()
        try:
            assert _wait_for(lambda: wd.fired), "watchdog never fired"
        finally:
            wd.stop()
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=10)

        rd = self._run_dir(tmp_path, monkeypatch, "silent")
        job = Job(id="silent", name="Silent", script_path="C:\\ok.py", alert_on_failure=True)
        telegram_notifier = MagicMock()
        notif.notify_failure(
            self._cfg(), job, rd,
            status="failed", exit_code=15, watchdog_note=wd.note,
            telegram_notifier=telegram_notifier,
        )
        _, body = telegram_notifier.notify.call_args.args[:2]
        assert wd.note in body

    def test_no_watchdog_note_leaves_body_unchanged(self, tmp_path, monkeypatch):
        rd = self._run_dir(tmp_path, monkeypatch, "fine")
        job = Job(id="fine", name="Fine", script_path="C:\\ok.py", alert_on_failure=True)
        telegram_notifier = MagicMock()
        notif.notify_failure(
            self._cfg(), job, rd,
            status="failed", exit_code=1, telegram_notifier=telegram_notifier,
        )
        _, body = telegram_notifier.notify.call_args.args[:2]
        assert "watchdog" not in body
        assert body.endswith("exit=1")

    def test_composes_with_reaped_note(self, tmp_path, monkeypatch):
        rd = self._run_dir(tmp_path, monkeypatch, "reaped-wedged")
        job = Job(
            id="reaped-wedged", name="Reaped Wedged", script_path="C:\\ok.py",
            alert_on_failure=True,
        )
        telegram_notifier = MagicMock()
        notif.notify_failure(
            self._cfg(), job, rd,
            status="failed", exit_code=None, reaped=True,
            watchdog_note="watchdog: max runtime 33min exceeded",
            telegram_notifier=telegram_notifier,
        )
        _, body = telegram_notifier.notify.call_args.args[:2]
        assert "reaped — end time not confirmed" in body
        assert "watchdog: max runtime 33min exceeded" in body
