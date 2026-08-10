"""Lifespan gating for the Jobs missed-fire coverage tick (issue #697).

The tick is what makes coverage independent of the Jobs tab being open — the
two real incidents (`config-map` / `sota-watch` with no registered task at
all) survived weeks precisely because nothing looked when nobody was looking.

But it must never run on a *disposable* instance: the e2e / verify-before-ship
autoboot webapp, pointed at a scratch config, would otherwise push a real
Pushover/Telegram alert to the user's phone every time the gate runs — the
same class of bug as #278's mirror-window slaughter, and identified by the
same ``LAUNCHER_SESSION_HOST_PORT`` marker.

That marker alone was too narrow (#736): a webapp booted out of a git
worktree carries the real config and real credentials but resolves its run
history under its own empty ``webapp/jobs/``, so it read "every scheduled
fire was missed" and pushed three false alerts to the phone on 2026-08-10.
The gate is now the broader :func:`src.instance_role.canonical_instance`.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.webapp import server
from src import instance_role
from src.webapp_config import SESSION_HOST_PORT_ENV


def _app(**cfg_kw) -> SimpleNamespace:
    defaults = {"jobs_coverage_interval_minutes": 60}
    defaults.update(cfg_kw)
    return SimpleNamespace(state=SimpleNamespace(webapp_config=SimpleNamespace(**defaults)))


@pytest.fixture(autouse=True)
def _no_canonical_override(monkeypatch):
    """Never let the ambient environment decide the instance's role."""
    monkeypatch.delenv(instance_role.CANONICAL_OVERRIDE_ENV, raising=False)


def _primary_checkout(tmp_path):
    """A directory shaped like a primary checkout: ``.git`` is a directory."""
    (tmp_path / ".git").mkdir()
    return tmp_path


def _linked_worktree(tmp_path):
    """A directory shaped like a linked worktree: ``.git`` is a *file*."""
    (tmp_path / ".git").write_text(
        "gitdir: E:/automation/app-launcher/.git/worktrees/app-launcher-wt-727\n",
        encoding="utf-8",
    )
    return tmp_path


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


class TestCanonicalInstancePredicate:
    """The role predicate itself (#736), independent of the lifespan."""

    def test_primary_checkout_is_canonical(self, tmp_path, monkeypatch):
        monkeypatch.delenv(SESSION_HOST_PORT_ENV, raising=False)
        canonical, reason = instance_role.canonical_instance(
            _primary_checkout(tmp_path)
        )
        assert canonical is True
        assert reason == instance_role.REASON_CANONICAL

    def test_linked_worktree_is_not_canonical(self, tmp_path, monkeypatch):
        monkeypatch.delenv(SESSION_HOST_PORT_ENV, raising=False)
        canonical, reason = instance_role.canonical_instance(
            _linked_worktree(tmp_path)
        )
        assert canonical is False
        assert reason == instance_role.REASON_LINKED_WORKTREE

    def test_root_without_git_stands_down_rather_than_guessing(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.delenv(SESSION_HOST_PORT_ENV, raising=False)
        canonical, reason = instance_role.canonical_instance(tmp_path)
        assert canonical is False
        assert reason == instance_role.REASON_ROOT_UNVERIFIABLE

    def test_disposable_marker_wins_over_a_forcing_override(
        self, tmp_path, monkeypatch
    ):
        # The e2e/verify autoboot exemption must be unconditional — an
        # override must never re-arm the gate's throwaway webapp.
        monkeypatch.setenv(SESSION_HOST_PORT_ENV, "54321")
        monkeypatch.setenv(instance_role.CANONICAL_OVERRIDE_ENV, "1")
        canonical, reason = instance_role.canonical_instance(
            _primary_checkout(tmp_path)
        )
        assert canonical is False
        assert reason == instance_role.REASON_DISPOSABLE

    def test_override_can_silence_a_second_clone(self, tmp_path, monkeypatch):
        monkeypatch.delenv(SESSION_HOST_PORT_ENV, raising=False)
        monkeypatch.setenv(instance_role.CANONICAL_OVERRIDE_ENV, "0")
        canonical, reason = instance_role.canonical_instance(
            _primary_checkout(tmp_path)
        )
        assert canonical is False
        assert reason == instance_role.REASON_FORCED_OFF

    def test_override_can_arm_a_root_with_no_git(self, tmp_path, monkeypatch):
        monkeypatch.delenv(SESSION_HOST_PORT_ENV, raising=False)
        monkeypatch.setenv(instance_role.CANONICAL_OVERRIDE_ENV, "true")
        canonical, reason = instance_role.canonical_instance(tmp_path)
        assert canonical is True
        assert reason == instance_role.REASON_FORCED_ON

    def test_this_very_checkout_is_canonical(self, monkeypatch):
        # The regression guard's other half: whatever the predicate does to
        # strays, the real installed instance must keep alerting.
        monkeypatch.delenv(SESSION_HOST_PORT_ENV, raising=False)
        assert instance_role.canonical_instance() == (
            True,
            instance_role.REASON_CANONICAL,
        )


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

    async def test_worktree_instance_never_starts_the_tick(
        self, tmp_path, monkeypatch, caplog
    ):
        """#736 regression: the stray instance that alerted the phone.

        No ``LAUNCHER_SESSION_HOST_PORT`` — this webapp was started by hand
        out of ``app-launcher-wt-727``, not by the e2e autoboot — yet it read
        an empty ``webapp/jobs/`` and pushed three false "scheduled run never
        fired" alerts on 2026-08-10.
        """
        monkeypatch.delenv(SESSION_HOST_PORT_ENV, raising=False)
        root = _linked_worktree(tmp_path)
        monkeypatch.setattr(instance_role, "PROJECT_ROOT", root)
        with caplog.at_level("WARNING", logger=server._log.name):
            created = await self._run_lifespan(_app(), monkeypatch)
        assert created == []
        # Findable after the fact: a stray instance that silently declines is
        # only marginally better than one that alerts wrongly.
        assert any(
            instance_role.REASON_LINKED_WORKTREE in record.getMessage()
            and str(root) in record.getMessage()
            for record in caplog.records
            if record.levelname == "WARNING"
        ), caplog.text

    async def test_unverifiable_root_never_starts_the_tick(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.delenv(SESSION_HOST_PORT_ENV, raising=False)
        monkeypatch.setattr(instance_role, "PROJECT_ROOT", tmp_path)
        assert await self._run_lifespan(_app(), monkeypatch) == []


async def _noop_async(*a, **k):
    return None
