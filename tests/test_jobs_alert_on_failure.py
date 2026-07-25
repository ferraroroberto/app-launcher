"""`alert_on_failure` job flag (issue #597).

A job with this flag pushes a Telegram alert on a failed run, via the
vendored `src.notify` primitive, independent of the global Pushover
`notify_on_failure` switch. Opt-in per job (default off).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from app.cli.commands import run_job_cmd as rjc
from src import jobs as jobs_mod
from src import jobs_config as jc
from src import jobs_history as jobs_history_mod
from src.jobs_config import Job, JobsConfig, job_from_dict, save_jobs, update_job


class TestAlertOnFailureRoundTrip:
    def test_default_is_false_and_omitted(self):
        job = Job(id="j", name="J", script_path="C:\\x\\s.py")
        assert job.alert_on_failure is False
        assert "alert_on_failure" not in job.to_dict()

    def test_true_emitted_and_parsed(self):
        job = Job(id="j", name="J", script_path="C:\\x\\s.py", alert_on_failure=True)
        payload = job.to_dict()
        assert payload["alert_on_failure"] is True
        assert job_from_dict(payload).alert_on_failure is True

    def test_from_dict_defaults_false(self):
        job = job_from_dict(
            {"id": "j", "name": "J", "script_path": "C:\\x\\s.py"}
        )
        assert job.alert_on_failure is False

    def test_update_job_toggles(self, tmp_path, monkeypatch):
        monkeypatch.setattr(jc, "DEFAULT_JOBS_PATH", tmp_path / "jobs.json")
        cfg = JobsConfig(jobs=[Job(id="j", name="J", script_path="C:\\x\\s.py")])
        save_jobs(cfg)
        update_job(cfg, "j", alert_on_failure=True)
        assert jc.get_by_id(cfg, "j").alert_on_failure is True
        update_job(cfg, "j", alert_on_failure=False)
        assert jc.get_by_id(cfg, "j").alert_on_failure is False


class TestMaybeNotifyFailureTelegramChannel:
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

    def test_noop_when_job_flag_off(self, tmp_path, monkeypatch):
        monkeypatch.setattr(jobs_history_mod, "JOBS_RUNS_DIR", tmp_path)
        rd = jobs_mod.new_run_dir("demo", "20260524T080000")
        jobs_mod.write_run_json(rd, status="failed")
        job = Job(id="demo", name="Demo", script_path="C:\\ok.py")
        telegram_notifier = MagicMock()
        rjc._maybe_notify_failure(
            self._cfg(), job, rd,
            status="failed", exit_code=1, telegram_notifier=telegram_notifier,
        )
        telegram_notifier.notify.assert_not_called()

    def test_noop_when_creds_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(jobs_history_mod, "JOBS_RUNS_DIR", tmp_path)
        rd = jobs_mod.new_run_dir("demo", "20260524T080000")
        jobs_mod.write_run_json(rd, status="failed")
        job = Job(
            id="demo", name="Demo", script_path="C:\\ok.py", alert_on_failure=True,
        )
        # No telegram_notifier injected → falls through to
        # build_telegram_notifier_from_config, which resolves to
        # NoopNotifier when creds are missing.
        rjc._maybe_notify_failure(
            self._cfg(telegram_bot_token="", telegram_chat_id=""), job, rd,
            status="failed", exit_code=1,
        )

    def test_noop_on_success(self, tmp_path, monkeypatch):
        monkeypatch.setattr(jobs_history_mod, "JOBS_RUNS_DIR", tmp_path)
        rd = jobs_mod.new_run_dir("demo", "20260524T080000")
        jobs_mod.write_run_json(rd, status="success")
        job = Job(
            id="demo", name="Demo", script_path="C:\\ok.py", alert_on_failure=True,
        )
        telegram_notifier = MagicMock()
        rjc._maybe_notify_failure(
            self._cfg(), job, rd,
            status="success", exit_code=0, telegram_notifier=telegram_notifier,
        )
        telegram_notifier.notify.assert_not_called()

    def test_fires_on_failure(self, tmp_path, monkeypatch):
        monkeypatch.setattr(jobs_history_mod, "JOBS_RUNS_DIR", tmp_path)
        rd = jobs_mod.new_run_dir("demo", "20260524T080000")
        jobs_mod.write_run_json(rd, status="failed")
        job = Job(
            id="demo", name="Demo", script_path="C:\\ok.py", alert_on_failure=True,
        )
        telegram_notifier = MagicMock()
        rjc._maybe_notify_failure(
            self._cfg(), job, rd,
            status="failed", exit_code=1, telegram_notifier=telegram_notifier,
        )
        telegram_notifier.notify.assert_called_once()
        title, body = telegram_notifier.notify.call_args.args[:2]
        assert "Demo" in title
        assert "failed" in title
        assert rd.name in body

    def test_independent_of_global_pushover_switch(self, tmp_path, monkeypatch):
        """Telegram fires even when the global Pushover switch is off."""
        monkeypatch.setattr(jobs_history_mod, "JOBS_RUNS_DIR", tmp_path)
        rd = jobs_mod.new_run_dir("demo", "20260524T080000")
        jobs_mod.write_run_json(rd, status="failed")
        job = Job(
            id="demo", name="Demo", script_path="C:\\ok.py", alert_on_failure=True,
        )
        pushover_notifier = MagicMock()
        telegram_notifier = MagicMock()
        rjc._maybe_notify_failure(
            self._cfg(notify_on_failure=False), job, rd,
            status="failed", exit_code=1,
            notifier=pushover_notifier, telegram_notifier=telegram_notifier,
        )
        pushover_notifier.notify.assert_not_called()
        telegram_notifier.notify.assert_called_once()
