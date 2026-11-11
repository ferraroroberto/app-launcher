"""The declared job-run status machine (src/jobs/run_status.py)."""

from __future__ import annotations

import json

import pytest

from src.jobs import history as jobs_history_mod
from src.jobs import reap as jobs_reap_mod
from src.jobs.run_status import RunStatus, finished_status, is_active, run_status


@pytest.mark.parametrize(
    ("record", "state", "active"),
    [
        ({"status": "queued"}, RunStatus.QUEUED, False),
        ({"status": "pending"}, RunStatus.PENDING, True),
        ({"status": "running"}, RunStatus.RUNNING, True),
        ({"status": "success"}, RunStatus.SUCCESS, False),
        ({"status": "failed"}, RunStatus.FAILED, False),
        ({"status": "skipped"}, RunStatus.SKIPPED, False),
        ({"status": "dry_run_success"}, RunStatus.DRY_RUN_SUCCESS, False),
        ({"status": "dry_run_failed"}, RunStatus.DRY_RUN_FAILED, False),
        ({"status": "exploded"}, None, False),
        ({"status": None}, None, False),
        ({}, None, False),
        (None, None, False),
    ],
)
def test_run_status_table(record, state, active):
    assert run_status(record) is state
    assert is_active(record) is active


def test_finished_status_from_exit_code():
    assert finished_status(0) is RunStatus.SUCCESS
    assert finished_status(1) is RunStatus.FAILED
    assert finished_status(-1) is RunStatus.FAILED


@pytest.mark.parametrize("terminal", ["success", "failed"])
def test_reaper_refuses_to_move_a_finished_run(tmp_path, terminal):
    """A terminal run is never rewritten by the reaper's guarded write.

    state-machine-proof: job-run-status
    """
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    before = {"status": terminal, "exit_code": 0 if terminal == "success" else 1}
    (run_dir / "run.json").write_text(json.dumps(before), encoding="utf-8")

    moved = jobs_history_mod.write_run_json_if(
        run_dir, jobs_reap_mod._is_unfinalised, status=RunStatus.FAILED, reaped=True
    )

    assert moved is False
    assert json.loads((run_dir / "run.json").read_text(encoding="utf-8")) == before


def test_status_is_persisted_as_its_plain_string(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    jobs_history_mod.write_run_json(run_dir, status=RunStatus.RUNNING)
    assert json.loads((run_dir / "run.json").read_text(encoding="utf-8"))["status"] == "running"
