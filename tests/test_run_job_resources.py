"""Unit tests for the run-job executor's resource sampler + notifier hook
(issue #66 follow-ups to issue #47).

The sampler is tested by pointing it at a live child process (a short
``time.sleep`` Python subprocess), letting it tick a couple of times,
then asserting the on-disk run.json gained the resource fields. The
notifier path is tested in isolation via :func:`_maybe_notify_failure`.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.cli.commands import run_job_cmd as rjc
from src import jobs as jobs_mod


# ============================================================== sampler


class TestResourceSampler:
    def test_captures_nonzero_peak_for_live_process(self):
        # Use the current Python interpreter as a guaranteed-running
        # process — we don't actually wait on it; we just point the
        # sampler at our own pid so memory_info() returns sensible values.
        sampler = rjc._ResourceSampler(os.getpid())
        sampler.start()
        # One tick is plenty — the sampler runs at 1 Hz with no warmup.
        time.sleep(1.2)
        sampler.stop()
        assert sampler.peak_rss_bytes > 0
        assert sampler.cpu_seconds >= 0.0

    def test_stop_idempotent_and_quick(self):
        sampler = rjc._ResourceSampler(os.getpid())
        sampler.start()
        t0 = time.monotonic()
        sampler.stop()
        sampler.stop()  # second call must not raise / hang
        assert time.monotonic() - t0 < 3.0  # uses the event, not sleep

    def test_handles_dead_pid_gracefully(self):
        # A PID that does not exist (or that already exited): the
        # sampler must not crash and must surface zero peak.
        sampler = rjc._ResourceSampler(2 ** 31 - 1)
        sampler.start()
        time.sleep(0.2)
        sampler.stop()
        assert sampler.peak_rss_bytes == 0
        assert sampler.cpu_seconds == 0.0


# ============================================== executor integration


class TestExecutorFinalises:
    """End-to-end: run a tiny python script through ``RunJobCommand``
    and assert pid + duration + resource fields land in run.json.
    """

    @pytest.fixture
    def isolated_runs_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(jobs_mod, "JOBS_RUNS_DIR", tmp_path)
        return tmp_path

    def test_records_pid_duration_and_resources(
        self, isolated_runs_dir, tmp_path, monkeypatch
    ):
        from src.jobs_config import Job, JobsConfig

        # A tiny script that succeeds quickly. We use the same Python
        # that's running the tests so the venv-walk falls back to
        # ``sys.executable`` (no .venv next to the temp dir).
        script = tmp_path / "ok.py"
        script.write_text("import time; time.sleep(0.2)\n", encoding="utf-8")

        job = Job(
            id="demo",
            name="Demo",
            script_path=str(script),
            args="",
        )
        monkeypatch.setattr(
            "src.jobs_config.load_jobs",
            lambda: JobsConfig(jobs=[job]),
        )
        # Also patch the executor's import binding (it does
        # ``from src.jobs_config import ... load_jobs``).
        monkeypatch.setattr(rjc, "load_jobs", lambda: JobsConfig(jobs=[job]))
        # Notifier path must not raise; stub the config loader to a
        # plain noop-shaped config so we don't read the real on-disk one.
        monkeypatch.setattr(
            rjc, "load_webapp_config",
            lambda: SimpleNamespace(
                pushover_api_token="", pushover_user_key="",
                notify_on_failure=False, notify_failure_streak=0,
                notify_failure_summary=False,
            ),
        )

        from src.app_config import AppConfig
        cmd = rjc.RunJobCommand(AppConfig())
        rc = cmd.execute(
            SimpleNamespace(job_id="demo", trigger="manual", run_id=None)
        )
        assert rc == 0

        runs = jobs_mod.list_runs("demo")
        assert len(runs) == 1
        record = runs[0]
        assert record["status"] == "success"
        assert record["exit_code"] == 0
        assert isinstance(record.get("pid"), int)
        assert record["pid"] > 0
        assert record.get("duration_seconds") is not None
        assert record["duration_seconds"] >= 0.0
        # Resources are best-effort; we only assert presence, not values
        # (a very fast child can sample zero peak/cpu before exit).
        assert "peak_rss_bytes" in record
        assert "cpu_seconds" in record


# ================================================ _maybe_notify_failure


class TestMaybeNotifyFailure:
    def _cfg(self, **kw):
        defaults = dict(
            pushover_api_token="tok",
            pushover_user_key="user",
            notify_on_failure=True,
            notify_failure_streak=0,
            notify_failure_summary=False,
        )
        defaults.update(kw)
        return SimpleNamespace(**defaults)

    def test_noop_on_success(self, tmp_path, monkeypatch):
        from src.jobs_config import Job

        monkeypatch.setattr(jobs_mod, "JOBS_RUNS_DIR", tmp_path)
        rd = jobs_mod.new_run_dir("demo", "20260524T080000")
        jobs_mod.write_run_json(rd, status="success")
        job = Job(id="demo", name="Demo", script_path="C:\\ok.py")
        notifier = MagicMock()
        rjc._maybe_notify_failure(
            self._cfg(), job, rd,
            status="success", exit_code=0, notifier=notifier,
        )
        notifier.notify.assert_not_called()

    def test_noop_when_master_switch_off(self, tmp_path, monkeypatch):
        from src.jobs_config import Job

        monkeypatch.setattr(jobs_mod, "JOBS_RUNS_DIR", tmp_path)
        rd = jobs_mod.new_run_dir("demo", "20260524T080000")
        jobs_mod.write_run_json(rd, status="failed")
        (rd / "output.log").write_bytes(b"boom\n")
        job = Job(id="demo", name="Demo", script_path="C:\\ok.py")
        notifier = MagicMock()
        rjc._maybe_notify_failure(
            self._cfg(notify_on_failure=False), job, rd,
            status="failed", exit_code=1, notifier=notifier,
        )
        notifier.notify.assert_not_called()

    def test_fires_on_failure(self, tmp_path, monkeypatch):
        from src.jobs_config import Job

        monkeypatch.setattr(jobs_mod, "JOBS_RUNS_DIR", tmp_path)
        rd = jobs_mod.new_run_dir("demo", "20260524T080000")
        (rd / "output.log").write_bytes(b"Traceback\nValueError: nope\n")
        jobs_mod.write_run_json(rd, status="failed")
        job = Job(id="demo", name="Demo", script_path="C:\\ok.py")
        notifier = MagicMock()
        rjc._maybe_notify_failure(
            self._cfg(), job, rd,
            status="failed", exit_code=1, notifier=notifier,
        )
        notifier.notify.assert_called_once()
        call = notifier.notify.call_args
        title, body = call.args[0], call.args[1]
        severity = call.kwargs.get("severity") or (
            call.args[2] if len(call.args) > 2 else None
        )
        assert title.endswith("Demo")
        assert "ValueError" in body
        assert severity == "error"

    def test_streak_fires_on_exact_count(self, tmp_path, monkeypatch):
        from src.jobs_config import Job

        monkeypatch.setattr(jobs_mod, "JOBS_RUNS_DIR", tmp_path)
        # Seed two prior failed runs so the count hits 3 with the live one.
        for i, rid in enumerate(["20260101T060000", "20260102T060000"]):
            rd = jobs_mod.new_run_dir("demo", rid)
            jobs_mod.write_run_json(
                rd, status="failed", started_at=f"2026-01-0{i+1}T06:00:00",
            )
        rd = jobs_mod.new_run_dir("demo", "20260103T060000")
        jobs_mod.write_run_json(
            rd, status="failed", started_at="2026-01-03T06:00:00",
        )
        (rd / "output.log").write_bytes(b"third failure\n")
        job = Job(id="demo", name="Demo", script_path="C:\\ok.py")
        notifier = MagicMock()
        rjc._maybe_notify_failure(
            self._cfg(notify_failure_streak=3), job, rd,
            status="failed", exit_code=1, notifier=notifier,
        )
        # Two notify() calls: the per-failure one + the streak one.
        assert notifier.notify.call_count == 2
        streak_call = notifier.notify.call_args_list[1]
        assert "consecutive failures" in streak_call.args[0]
