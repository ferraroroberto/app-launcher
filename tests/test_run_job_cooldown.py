"""Executor-side cooldown skip path (issue #68 PR #1).

Scheduled fires landing inside the cooldown window must finalise as a
``status=skipped`` record without spawning the underlying script. Manual
fires are 429'd at the route — by the time the executor runs for a
``trigger=manual`` call either no overlap exists or the caller
deliberately bypassed the gate, so this skip is scheduled-only.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.cli.commands import run_job_cmd as rjc
from src import jobs as jobs_mod
from src.app_config import AppConfig
from src.jobs_config import Job, JobsConfig


@pytest.fixture
def isolated_runs_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_mod, "JOBS_RUNS_DIR", tmp_path)
    return tmp_path


def _seed_run(runs_root, job_id, run_id, *, started_at, status):
    rd = runs_root / job_id / run_id
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "run.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "job_id": job_id,
                "status": status,
                "started_at": started_at,
            }
        ),
        encoding="utf-8",
    )
    return rd


def _stub_load_jobs(monkeypatch, job):
    monkeypatch.setattr(
        "src.jobs_config.load_jobs", lambda: JobsConfig(jobs=[job])
    )
    monkeypatch.setattr(rjc, "load_jobs", lambda: JobsConfig(jobs=[job]))


def _silence_notifier(monkeypatch):
    """Stop the executor from touching the real webapp_config on finalise."""
    monkeypatch.setattr(
        rjc,
        "load_webapp_config",
        lambda: SimpleNamespace(
            notify_on_failure=False,
            notify_failure_summary=False,
            notify_failure_streak=0,
        ),
    )


class TestScheduledSkip:
    def test_scheduled_fire_inside_cooldown_writes_skipped(
        self, isolated_runs_dir, tmp_path, monkeypatch
    ):
        # Anchor: real run 5 s ago. Cooldown = 60 s → still inside window.
        now = datetime.now().replace(microsecond=0)
        _seed_run(
            isolated_runs_dir,
            "demo",
            "20260101T000000",
            started_at=(now - timedelta(seconds=5)).isoformat(timespec="seconds"),
            status="success",
        )
        script = tmp_path / "boom.py"
        # If the executor spawns this it'd raise — but the skip path must
        # finalise before build_invocation. So we leave the file missing
        # on purpose: any failure to short-circuit will surface as
        # `cannot run job` (exit 2), not a `skipped` record.
        job = Job(
            id="demo",
            name="Demo",
            script_path=str(script),
            args="",
            cooldown_seconds=60,
        )
        _stub_load_jobs(monkeypatch, job)
        _silence_notifier(monkeypatch)

        # Track whether the spawn path was attempted.
        popen_mock = MagicMock(
            side_effect=AssertionError("Popen must not run inside cooldown")
        )
        monkeypatch.setattr(rjc.subprocess, "Popen", popen_mock)

        cmd = rjc.RunJobCommand(AppConfig())
        args = SimpleNamespace(
            job_id="demo",
            trigger="scheduled",
            run_id=None,
            params=None,
        )
        rc = cmd.execute(args)
        assert rc == 0
        # Exactly one new run dir was created (the skip record) on top
        # of the seeded anchor.
        runs = sorted((isolated_runs_dir / "demo").iterdir())
        assert len(runs) == 2
        skip_dirs = [r for r in runs if r.name != "20260101T000000"]
        assert len(skip_dirs) == 1
        record = json.loads(
            (skip_dirs[0] / "run.json").read_text(encoding="utf-8")
        )
        assert record["status"] == "skipped"
        assert record["note"] == "cooldown"
        assert record["cooldown_seconds"] == 60
        assert 1 <= record["cooldown_remaining_seconds"] <= 60
        assert record["cooldown_anchor_run_id"] == "20260101T000000"
        # No output.log for a skipped run.
        assert not (skip_dirs[0] / "output.log").exists()
        assert not popen_mock.called

    def test_scheduled_fire_outside_cooldown_runs(
        self, isolated_runs_dir, tmp_path, monkeypatch
    ):
        # Anchor old enough to be out of the window — the skip path must
        # not fire. We confirm by reaching the spawn point.
        now = datetime.now().replace(microsecond=0)
        _seed_run(
            isolated_runs_dir,
            "demo",
            "20260101T000000",
            started_at=(now - timedelta(seconds=120)).isoformat(
                timespec="seconds"
            ),
            status="success",
        )
        script = tmp_path / "ok.py"
        script.write_text(
            "import sys; sys.exit(0)\n", encoding="utf-8"
        )
        job = Job(
            id="demo",
            name="Demo",
            script_path=str(script),
            args="",
            cooldown_seconds=60,
        )
        _stub_load_jobs(monkeypatch, job)
        _silence_notifier(monkeypatch)
        cmd = rjc.RunJobCommand(AppConfig())
        args = SimpleNamespace(
            job_id="demo",
            trigger="scheduled",
            run_id=None,
            params=None,
        )
        rc = cmd.execute(args)
        assert rc == 0
        # The new run dir is a real success record, not a skip.
        runs = sorted((isolated_runs_dir / "demo").iterdir())
        latest = max(runs, key=lambda p: p.name)
        record = json.loads(
            (latest / "run.json").read_text(encoding="utf-8")
        )
        assert record["status"] == "success"

    def test_dry_run_records_are_not_the_cooldown_anchor(
        self, isolated_runs_dir, monkeypatch
    ):
        """A dry-run record sitting on top of an older real run must NOT
        become the cooldown anchor — a verification fire shouldn't reset
        the window (issue #69, mirrors the `skipped` rule)."""
        now = datetime.now().replace(microsecond=0)
        # Real run 90 s ago — outside a 60 s window.
        _seed_run(
            isolated_runs_dir, "demo", "20260101T000000",
            started_at=(now - timedelta(seconds=90)).isoformat(timespec="seconds"),
            status="success",
        )
        # Dry-run check 5 s ago — must be ignored by the anchor.
        _seed_run(
            isolated_runs_dir, "demo", "20260101T000090",
            started_at=(now - timedelta(seconds=5)).isoformat(timespec="seconds"),
            status="dry_run_success",
        )
        job = Job(id="demo", name="Demo", script_path="x.py",
                  cooldown_seconds=60)
        # Outside the window relative to the real run → allowed (None).
        assert jobs_mod.cooldown_check(job, now=now) is None

    def test_manual_fire_does_not_skip_at_executor(
        self, isolated_runs_dir, tmp_path, monkeypatch
    ):
        """Manual fires reach the executor only when the route has
        cleared them — the executor must NOT add a second cooldown gate.
        """
        now = datetime.now().replace(microsecond=0)
        _seed_run(
            isolated_runs_dir,
            "demo",
            "20260101T000000",
            started_at=(now - timedelta(seconds=5)).isoformat(timespec="seconds"),
            status="success",
        )
        script = tmp_path / "ok.py"
        script.write_text("import sys; sys.exit(0)\n", encoding="utf-8")
        job = Job(
            id="demo",
            name="Demo",
            script_path=str(script),
            args="",
            cooldown_seconds=60,
        )
        _stub_load_jobs(monkeypatch, job)
        _silence_notifier(monkeypatch)
        cmd = rjc.RunJobCommand(AppConfig())
        args = SimpleNamespace(
            job_id="demo",
            trigger="manual",
            run_id=None,
            params=None,
        )
        rc = cmd.execute(args)
        assert rc == 0
        # The new run is a real success — no skip injected on the manual path.
        runs = sorted((isolated_runs_dir / "demo").iterdir())
        latest = max(runs, key=lambda p: p.name)
        record = json.loads(
            (latest / "run.json").read_text(encoding="utf-8")
        )
        assert record["status"] == "success"
