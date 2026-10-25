"""Fixtures for the /api/jobs tests: schtasks and the detached executor mocked at the router."""

from __future__ import annotations

import shutil

import pytest

from tests.api.jobs import _support


@pytest.fixture(scope="session", autouse=True)
def _cleanup_stub_dir():
    yield
    # Read through the module: _stub_path() rebinds the global after import.
    stub_dir = _support._STUB_DIR
    if stub_dir is not None and stub_dir.is_dir():
        shutil.rmtree(stub_dir, ignore_errors=True)


@pytest.fixture
def mocked_jobs_side_effects(monkeypatch):
    """Stub schtasks I/O so the router can run without Windows Task Scheduler.

    Returns a dict of MagicMocks tests can inspect for call args.
    """
    from unittest.mock import MagicMock
    from app.webapp.routers import jobs as jobs_router

    mocks = {
        "sync_schtasks": MagicMock(return_value=[]),
        "delete_schtasks": MagicMock(return_value=[]),
        "query_next_run": MagicMock(return_value=None),
        "spawn_run_job_detached": MagicMock(return_value=1234),
        # Issue #66 — decorate_job now calls run_stats + is_stuck for
        # every row; default to "no data" so existing assertions remain
        # untouched and new behaviour is opt-in per test.
        "run_stats": MagicMock(
            return_value={
                "p50": None,
                "p95": None,
                "success_rate_30d": None,
                "completed_count": 0,
                "last7": [],
            }
        ),
        "is_stuck": MagicMock(return_value=False),
        # Issue #697 — decorate_job also asks for missed-fire coverage, whose
        # structural half reads Task Scheduler. Stub it here for the same
        # reason query_next_run above is stubbed: without it every decorated
        # row shells out to the real schtasks on the developer's box.
        "coverage_for_job": MagicMock(
            return_value={
                "state": "ok",
                "detail": "",
                "problems": [],
                "missing_tasks": [],
                "disabled_tasks": [],
                "missed_count": 0,
                "missed_fires": [],
            }
        ),
    }
    # The shared run tail moved into src.jobs_queue (issue #794's
    # admit_and_spawn), which binds spawn_run_job_detached in its own
    # namespace at import time — so stubbing only the src.jobs facade would
    # let the route spawn a real detached executor. Mirror every stub onto
    # jobs_queue for the names it actually holds.
    from src import jobs_queue as jobs_queue_mod

    for name, m in mocks.items():
        monkeypatch.setattr(jobs_router.jobs_mod, name, m)
        if hasattr(jobs_queue_mod, name):
            monkeypatch.setattr(jobs_queue_mod, name, m)
    return mocks
