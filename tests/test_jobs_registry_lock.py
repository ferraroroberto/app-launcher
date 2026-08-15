"""Interprocess-lock regression for `config/jobs.json` (issue #755).

Every mutator in ``src.jobs_config`` used to take a caller-supplied
``JobsConfig`` snapshot at face value: mutate the in-memory copy, then
``save_jobs()`` the *whole* registry over the file. Two writers each
holding a snapshot taken before the other's change would race — the
second ``save_jobs()`` clobbers the first writer's already-persisted
change with a stale in-memory copy that never saw it (``os.replace``
always "wins" for whoever calls it last, but that writer's payload is
missing the other's edit).

This reproduces the loss without genuine multi-process concurrency: two
callers each hold a distinct ``JobsConfig`` loaded at different points,
writer A persists first, then writer B — whose snapshot predates A's
change — persists a second, unrelated field. The fix (mutators re-read
fresh state from disk under ``jobs_file_lock`` instead of trusting the
snapshot) must land both edits; the pre-fix code drops A's.
"""

from __future__ import annotations

import pytest

from src import jobs_config as jc
from src.jobs_config import (
    Job,
    JobsConfig,
    add_job,
    job_from_dict,
    load_jobs,
    update_job,
)


def test_concurrent_stale_snapshot_writers_do_not_clobber(tmp_path, monkeypatch):
    monkeypatch.setattr(jc, "DEFAULT_JOBS_PATH", tmp_path / "jobs.json")

    seed_cfg = JobsConfig(jobs=[])
    add_job(
        seed_cfg,
        job_from_dict(
            {"id": "x", "name": "X", "script_path": "C:\\x.py",
             "schedule": {"type": "daily", "at": "06:00"}}
        ),
    )

    # Two writers each independently load their own snapshot before either
    # writes — the exact shape of two concurrent webapp requests, or a
    # webapp edit racing the executor's own load-mutate-save.
    writer_a_cfg = load_jobs()
    writer_b_cfg = load_jobs()

    # Writer A persists first.
    update_job(writer_a_cfg, "x", cooldown_seconds=30)

    # Writer B's snapshot predates A's change, but touches an unrelated
    # field. A correct implementation re-reads fresh state under the lock
    # before writing, so B's save must not revert A's already-persisted
    # cooldown_seconds back to its pre-edit value.
    update_job(writer_b_cfg, "x", args="--verbose")

    reloaded = next(j for j in load_jobs().jobs if j.id == "x")
    assert reloaded.cooldown_seconds == 30, (
        "writer B's stale-snapshot save clobbered writer A's already-"
        "persisted change instead of preserving it"
    )
    assert reloaded.args == "--verbose"


def test_add_job_duplicate_check_sees_concurrent_writer(tmp_path, monkeypatch):
    """A duplicate-id add against a stale empty snapshot must still be
    rejected once a concurrent writer has already taken that id.
    """
    monkeypatch.setattr(jc, "DEFAULT_JOBS_PATH", tmp_path / "jobs.json")

    stale_cfg = JobsConfig(jobs=[])  # snapshot taken before anything exists
    add_job(
        JobsConfig(jobs=[]),
        job_from_dict({"id": "dup", "name": "Dup", "script_path": "C:\\a.py"}),
    )

    with pytest.raises(ValueError, match="already exists"):
        add_job(
            stale_cfg,
            Job(id="dup", name="Dup2", script_path="C:\\b.py"),
        )
