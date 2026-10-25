"""Fixtures for the session-host input tests: the fake clock and parked deferred-submit watchers."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src import session_host as session_host_module
from src.session_hosting import deferred_submit

from tests.unit.session_host._support import _FakeClock


@pytest.fixture
def clock(monkeypatch):
    fake = _FakeClock()
    monkeypatch.setattr(session_host_module.time, "time", fake.time)
    monkeypatch.setattr(session_host_module.time, "sleep", fake.sleep)
    return fake


@pytest.fixture
def parked_watchers(monkeypatch):
    """Park the deferred-submit watcher thread ``submit_input`` starts, so a
    test decides when the agent settles; ``run()`` then runs it to its verdict."""
    parked: list = []

    class _Parked:
        def __init__(self, target, args=(), kwargs=None, **_):
            self._call = lambda: target(*args, **(kwargs or {}))

        def start(self):
            parked.append(self)

        def run(self):
            self._call()

    monkeypatch.setattr(deferred_submit, "threading", SimpleNamespace(Thread=_Parked))
    return parked
