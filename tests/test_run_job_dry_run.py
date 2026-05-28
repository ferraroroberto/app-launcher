"""Executor-side dry-run 'execute' mode (issue #69 PR #2).

``--dry-run`` makes the executor spawn the child with ``JOB_DRY_RUN=1``
in its environment and stamp ``dry_run: true`` on the run record. The
child still runs — opted-in scripts read the env var and suppress their
own side effects.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.cli.commands import run_job_cmd as rjc
from src import jobs as jobs_mod
from src.app_config import AppConfig
from src.jobs_config import Job, JobsConfig, save_jobs


@pytest.fixture
def isolated_jobs(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_mod, "JOBS_RUNS_DIR", tmp_path)
    monkeypatch.setattr(jobs_mod, "JOBS_QUEUE_PATH", tmp_path / "_queue.json")
    from src import jobs_config as jc
    monkeypatch.setattr(jc, "DEFAULT_JOBS_PATH", tmp_path / "jobs.json")
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


def _seed(tmp_path, body: str) -> Job:
    script = tmp_path / "probe.py"
    script.write_text(body, encoding="utf-8")
    job = Job(id="probe", name="Probe", script_path=str(script))
    save_jobs(JobsConfig(jobs=[job]))
    return job


def test_dry_run_sets_env_and_stamps_record(
    isolated_jobs, tmp_path, monkeypatch
):
    marker = tmp_path / "marker.txt"
    # The child writes whatever JOB_DRY_RUN it sees to a sidecar file.
    body = (
        "import os\n"
        f"open(r'{marker}', 'w').write(os.environ.get('JOB_DRY_RUN', ''))\n"
    )
    _seed(tmp_path, body)
    _silence_notifier(monkeypatch)

    cmd = rjc.RunJobCommand(AppConfig())
    rc = cmd.execute(SimpleNamespace(
        job_id="probe", trigger="manual", run_id=None, params=None,
        dry_run=True,
    ))
    assert rc == 0
    # The child observed JOB_DRY_RUN=1 in its environment.
    assert marker.read_text(encoding="utf-8") == "1"
    # And the run record is stamped dry_run:true.
    latest = jobs_mod.latest_run("probe")
    assert latest is not None
    assert latest.get("dry_run") is True
    assert latest.get("status") == "success"


def test_non_dry_run_leaves_env_unset(
    isolated_jobs, tmp_path, monkeypatch
):
    marker = tmp_path / "marker.txt"
    body = (
        "import os\n"
        f"open(r'{marker}', 'w').write(os.environ.get('JOB_DRY_RUN', 'UNSET'))\n"
    )
    _seed(tmp_path, body)
    _silence_notifier(monkeypatch)

    cmd = rjc.RunJobCommand(AppConfig())
    rc = cmd.execute(SimpleNamespace(
        job_id="probe", trigger="manual", run_id=None, params=None,
        dry_run=False,
    ))
    assert rc == 0
    assert marker.read_text(encoding="utf-8") == "UNSET"
    latest = jobs_mod.latest_run("probe")
    assert latest is not None
    assert "dry_run" not in latest
