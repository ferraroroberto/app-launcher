"""Board unit tests start each test with the module-global gh cache and failure latches reset."""

from __future__ import annotations

import pytest

from app.webapp.routers import board_spawn
from src import board
from src.clients import github as github_client


@pytest.fixture(autouse=True)
def _pristine_gh_cache():
    """The gh cache is module-global — every test starts and ends empty."""
    github_client.reset_cache()
    yield
    github_client.reset_cache()


@pytest.fixture(autouse=True)
def _pristine_failure_latches(monkeypatch):
    """The once-per-failure log latches (#915) are module-global too."""
    monkeypatch.setattr(board_spawn, "_session_host_down", False, raising=False)
    monkeypatch.setattr(board, "_UNREADABLE_JOBS", set(), raising=False)
