"""Lifespan gating for the Jobs missed-fire coverage tick (issue #697).

The tick is what makes coverage independent of the Jobs tab being open — the
two real incidents (`config-map` / `sota-watch` with no registered task at
all) survived weeks precisely because nothing looked when nobody was looking.

But it must never run on a *disposable* instance: the e2e / verify-before-ship
autoboot webapp, pointed at a scratch config, would otherwise push a real
Pushover/Telegram alert to the user's phone every time the gate runs — the
same class of bug as #278's mirror-window slaughter, and identified by the
same ``LAUNCHER_SESSION_HOST_PORT`` marker.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.webapp import server
from src.webapp_config import SESSION_HOST_PORT_ENV


def _app(**cfg_kw) -> SimpleNamespace:
    defaults = {"jobs_coverage_interval_minutes": 60}
    defaults.update(cfg_kw)
    return SimpleNamespace(state=SimpleNamespace(webapp_config=SimpleNamespace(**defaults)))


class TestIntervalCoercion:
    def test_missing_key_falls_back_to_the_schema_default(self):
        assert server._coverage_interval_minutes(SimpleNamespace()) == 60.0

    def test_zero_disables(self):
        assert server._coverage_interval_minutes(
            SimpleNamespace(jobs_coverage_interval_minutes=0)
        ) == 0.0

    def test_garbage_disables_rather_than_raising(self):
        assert server._coverage_interval_minutes(
            SimpleNamespace(jobs_coverage_interval_minutes="soon")
        ) == 0.0


@pytest.mark.asyncio
class TestLifespanGate:
    async def _run_lifespan(self, app, monkeypatch):
        created = []
        real_create_task = asyncio.create_task

        def _spy(coro, *a, **k):
            task = real_create_task(coro, *a, **k)
            created.append(task)
            return task

        monkeypatch.setattr(server.asyncio, "create_task", _spy)
        # Never let the real tick body run — it would sleep two minutes and
        # then shell out to schtasks.
        monkeypatch.setattr(
            server, "_coverage_tick", lambda a: asyncio.sleep(3600)
        )
        monkeypatch.setattr(
            server, "_reconcile_orphan_mirror_windows", _noop_async
        )
        async with server._lifespan(app):
            pass
        return created

    async def test_disposable_instance_never_starts_the_tick(
        self, monkeypatch
    ):
        monkeypatch.setenv(SESSION_HOST_PORT_ENV, "54321")
        assert await self._run_lifespan(_app(), monkeypatch) == []

    async def test_zero_interval_never_starts_the_tick(self, monkeypatch):
        monkeypatch.delenv(SESSION_HOST_PORT_ENV, raising=False)
        created = await self._run_lifespan(
            _app(jobs_coverage_interval_minutes=0), monkeypatch
        )
        assert created == []

    async def test_canonical_instance_starts_and_cancels_the_tick(
        self, monkeypatch
    ):
        monkeypatch.delenv(SESSION_HOST_PORT_ENV, raising=False)
        created = await self._run_lifespan(_app(), monkeypatch)
        assert len(created) == 1
        # Shutdown must not leave the tick running against a torn-down app.
        assert created[0].cancelled()


async def _noop_async(*a, **k):
    return None
