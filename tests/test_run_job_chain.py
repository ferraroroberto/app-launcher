"""Executor-side DAG chain (issue #68 PR #3).

A finished run with ``on_success`` / ``on_failure`` set spawns the
configured downstream jobs detached, with ``trigger="chain:<upstream>"``
and a ``chained_from`` marker on the downstream run record.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.cli.commands import run_job_cmd as rjc
from src import jobs as jobs_mod
from src.app_config import AppConfig
from src.jobs_config import Job, JobsConfig


@pytest.fixture
def isolated_jobs(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_mod, "JOBS_RUNS_DIR", tmp_path)
    monkeypatch.setattr(jobs_mod, "JOBS_QUEUE_PATH", tmp_path / "_queue.json")
    return tmp_path


def _silence_notifier(monkeypatch):
    monkeypatch.setattr(
        rjc, "load_webapp_config",
        lambda: SimpleNamespace(
            notify_on_failure=False,
            notify_failure_summary=False,
            notify_failure_streak=0,
        ),
    )


def _patch_registry(monkeypatch, jobs):
    cfg = JobsConfig(jobs=jobs)
    monkeypatch.setattr("src.jobs_config.load_jobs", lambda: cfg)
    monkeypatch.setattr(rjc, "load_jobs", lambda: cfg)


class TestExecutorChain:
    def test_on_success_fires_downstream(
        self, isolated_jobs, tmp_path, monkeypatch
    ):
        script = tmp_path / "ok.py"
        script.write_text("import sys; sys.exit(0)\n", encoding="utf-8")
        downstream_script = tmp_path / "down.py"
        downstream_script.write_text(
            "import sys; sys.exit(0)\n", encoding="utf-8"
        )
        upstream = Job(
            id="up", name="Up", script_path=str(script),
            on_success=["down"],
        )
        downstream = Job(
            id="down", name="Down", script_path=str(downstream_script),
        )
        _patch_registry(monkeypatch, [upstream, downstream])
        _silence_notifier(monkeypatch)

        spawn = MagicMock(return_value=4242)
        monkeypatch.setattr(jobs_mod, "spawn_run_job_detached", spawn)

        cmd = rjc.RunJobCommand(AppConfig())
        rc = cmd.execute(SimpleNamespace(
            job_id="up", trigger="manual", run_id=None, params=None,
        ))
        assert rc == 0
        assert spawn.called
        # spawn args: (job_id, run_id, trigger, params)
        args = spawn.call_args.args
        assert args[0] == "down"
        assert args[2].startswith("chain:up")
        # The downstream run dir was pre-created with chained_from=up.
        down_root = isolated_jobs / "down"
        runs = list(down_root.iterdir())
        assert len(runs) == 1
        record = json.loads((runs[0] / "run.json").read_text(encoding="utf-8"))
        assert record["chained_from"] == "up"
        assert record["trigger"].startswith("chain:up")

    def test_on_failure_fires_downstream(
        self, isolated_jobs, tmp_path, monkeypatch
    ):
        script = tmp_path / "fail.py"
        script.write_text("import sys; sys.exit(7)\n", encoding="utf-8")
        downstream_script = tmp_path / "down.py"
        downstream_script.write_text(
            "import sys; sys.exit(0)\n", encoding="utf-8"
        )
        upstream = Job(
            id="up", name="Up", script_path=str(script),
            on_failure=["down"],
        )
        downstream = Job(
            id="down", name="Down", script_path=str(downstream_script),
        )
        _patch_registry(monkeypatch, [upstream, downstream])
        _silence_notifier(monkeypatch)

        spawn = MagicMock(return_value=4242)
        monkeypatch.setattr(jobs_mod, "spawn_run_job_detached", spawn)

        cmd = rjc.RunJobCommand(AppConfig())
        rc = cmd.execute(SimpleNamespace(
            job_id="up", trigger="manual", run_id=None, params=None,
        ))
        # The upstream failed (exit 7) but the executor returns 1.
        assert rc == 1
        assert spawn.called
        assert spawn.call_args.args[0] == "down"

    def test_on_failure_does_not_fire_on_success(
        self, isolated_jobs, tmp_path, monkeypatch
    ):
        script = tmp_path / "ok.py"
        script.write_text("import sys; sys.exit(0)\n", encoding="utf-8")
        upstream = Job(
            id="up", name="Up", script_path=str(script),
            on_failure=["down"],
        )
        downstream = Job(id="down", name="Down", script_path="C:\\x.py")
        _patch_registry(monkeypatch, [upstream, downstream])
        _silence_notifier(monkeypatch)

        spawn = MagicMock(return_value=4242)
        monkeypatch.setattr(jobs_mod, "spawn_run_job_detached", spawn)

        cmd = rjc.RunJobCommand(AppConfig())
        rc = cmd.execute(SimpleNamespace(
            job_id="up", trigger="manual", run_id=None, params=None,
        ))
        assert rc == 0
        assert not spawn.called

    def test_chain_into_mutex_collision_queues(
        self, isolated_jobs, tmp_path, monkeypatch
    ):
        """A chained downstream that hits a mutex collision must be
        queued, not spawned — so chain + mutex interact correctly."""
        script = tmp_path / "ok.py"
        script.write_text("import sys; sys.exit(0)\n", encoding="utf-8")
        upstream = Job(
            id="up", name="Up", script_path=str(script),
            on_success=["down"],
        )
        downstream = Job(
            id="down", name="Down", script_path="C:\\x.py",
            mutex_group="shared",
        )
        # A third party already holds the mutex group.
        other = Job(
            id="other", name="Other", script_path="C:\\y.py",
            mutex_group="shared",
        )
        _patch_registry(monkeypatch, [upstream, downstream, other])
        _silence_notifier(monkeypatch)
        # Seed `other` as running.
        from datetime import datetime
        rd = isolated_jobs / "other" / "20260101T000000"
        rd.mkdir(parents=True)
        (rd / "run.json").write_text(json.dumps({
            "run_id": "20260101T000000", "job_id": "other",
            "status": "running",
            "started_at": datetime.now().isoformat(timespec="seconds"),
        }), encoding="utf-8")

        spawn = MagicMock(return_value=4242)
        monkeypatch.setattr(jobs_mod, "spawn_run_job_detached", spawn)

        cmd = rjc.RunJobCommand(AppConfig())
        rc = cmd.execute(SimpleNamespace(
            job_id="up", trigger="manual", run_id=None, params=None,
        ))
        assert rc == 0
        # Downstream landed as queued, not spawned. (No spawn for `down`.)
        spawned_ids = [c.args[0] for c in spawn.call_args_list]
        assert "down" not in spawned_ids
        # And the on-disk record is status=queued.
        down_runs = list((isolated_jobs / "down").iterdir())
        assert len(down_runs) == 1
        record = json.loads(
            (down_runs[0] / "run.json").read_text(encoding="utf-8")
        )
        assert record["status"] == "queued"
        assert record["mutex_blocked_by"] == "other"
