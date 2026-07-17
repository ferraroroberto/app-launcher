"""Executor-side ``Job.env`` overlay + provenance stamps (issue #72).

The env overlay merges into the child's environment at fire time, with
``$secret:<key>`` references resolved against the webapp config's
``secrets`` block; an unresolvable reference finalises the run as failed
with a clear note. Scheduled fires stamp ``trigger_source: "schtasks"``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.cli.commands import run_job_cmd as rjc
from src import jobs as jobs_mod
from src import jobs_history as jobs_history_mod
from src import jobs_queue as jobs_queue_mod
from src.app_config import AppConfig
from src.jobs_config import Job, JobsConfig, save_jobs


@pytest.fixture
def isolated_jobs(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_history_mod, "JOBS_RUNS_DIR", tmp_path)
    monkeypatch.setattr(jobs_queue_mod, "JOBS_QUEUE_PATH", tmp_path / "_queue.json")
    from src import jobs_config as jc
    monkeypatch.setattr(jc, "DEFAULT_JOBS_PATH", tmp_path / "jobs.json")
    return tmp_path


def _stub_webapp_config(monkeypatch, secrets=None):
    """One patch point covers both the env resolver and the notifier."""
    monkeypatch.setattr(
        rjc, "load_webapp_config",
        lambda: SimpleNamespace(
            secrets=secrets or {},
            notify_on_failure=False,
            notify_failure_summary=False,
            notify_failure_streak=0,
        ),
    )


def _seed(tmp_path, body: str, env=None) -> Job:
    script = tmp_path / "probe.py"
    script.write_text(body, encoding="utf-8")
    job = Job(id="probe", name="Probe", script_path=str(script), env=env or {})
    save_jobs(JobsConfig(jobs=[job]))
    return job


def _execute(trigger="manual"):
    cmd = rjc.RunJobCommand(AppConfig())
    return cmd.execute(SimpleNamespace(
        job_id="probe", trigger=trigger, run_id=None, params=None,
        dry_run=False,
    ))


def test_env_overlay_reaches_child_with_resolved_secret(
    isolated_jobs, tmp_path, monkeypatch
):
    marker = tmp_path / "marker.txt"
    body = (
        "import os\n"
        f"open(r'{marker}', 'w').write("
        "os.environ.get('MY_TOKEN', '') + '|' + os.environ.get('PLAIN', ''))\n"
    )
    _seed(tmp_path, body, env={"MY_TOKEN": "$secret:api", "PLAIN": "literal"})
    _stub_webapp_config(monkeypatch, secrets={"api": "resolved-xyz"})

    assert _execute() == 0
    assert marker.read_text(encoding="utf-8") == "resolved-xyz|literal"
    latest = jobs_mod.latest_run("probe")
    assert latest is not None and latest.get("status") == "success"


def test_unknown_secret_finalises_failed_run_with_note(
    isolated_jobs, tmp_path, monkeypatch
):
    marker = tmp_path / "marker.txt"
    body = f"open(r'{marker}', 'w').write('ran')\n"
    _seed(tmp_path, body, env={"X": "$secret:missing"})
    _stub_webapp_config(monkeypatch, secrets={})

    assert _execute() == 2
    # The child never spawned.
    assert not marker.exists()
    latest = jobs_mod.latest_run("probe")
    assert latest is not None
    assert latest.get("status") == "failed"
    assert latest.get("exit_code") == -1
    assert "secret 'missing' not found" in latest.get("note", "")


def test_scheduled_fire_stamps_schtasks_source(
    isolated_jobs, tmp_path, monkeypatch
):
    _seed(tmp_path, "print('ok')\n")
    _stub_webapp_config(monkeypatch)

    assert _execute(trigger="scheduled") == 0
    latest = jobs_mod.latest_run("probe")
    assert latest is not None
    assert latest.get("trigger") == "scheduled"
    assert latest.get("trigger_source") == "schtasks"


def test_manual_cli_fire_has_no_trigger_source(
    isolated_jobs, tmp_path, monkeypatch
):
    _seed(tmp_path, "print('ok')\n")
    _stub_webapp_config(monkeypatch)

    assert _execute(trigger="manual") == 0
    latest = jobs_mod.latest_run("probe")
    assert latest is not None
    # A direct CLI fire has no HTTP provenance; only the API route (which
    # pre-writes trigger_source="api") and schtasks stamp a source.
    assert "trigger_source" not in latest
