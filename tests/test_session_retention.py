"""Retention window for ``webapp/sessions`` (issue #902).

What gets deleted here is not in git and not backed up, so most of these
pin what the sweep must *not* do: remove a live session's files, remove a
file whose age it couldn't establish, act on a live list it couldn't fetch,
follow a link out of the directory, or empty the directory on a clock that
jumped forward.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.webapp import server
from src import instance_role, session_client, session_retention
from src.webapp_config import SESSION_HOST_PORT_ENV, WebappConfig, _validate

DAY = 86400
NOW = 1_800_000_000.0  # fixed clock: 2027-01-15


def _touch(path: Path, age_days: float, body: str = "x") -> Path:
    path.write_text(body, encoding="utf-8")
    t = NOW - age_days * DAY
    os.utime(path, (t, t))
    return path


@pytest.fixture
def sessions(tmp_path: Path) -> Path:
    d = tmp_path / "sessions"
    d.mkdir()
    # Something recent always exists in a real directory (the audit trail is
    # written continuously); without it the clock witness stands down.
    _touch(d / "fresh.log", 0.1)
    return d


def _sweep(d: Path, live=(), *, dry_run=False, days=365):
    return session_retention.sweep(d, days, live, dry_run=dry_run, now=NOW)


class TestSelection:
    def test_removes_both_kinds_past_the_window_and_keeps_younger(self, sessions):
        old_log = _touch(sessions / "old.log", 400)
        old_tx = _touch(sessions / "old.transcript", 400)
        young_tx = _touch(sessions / "young.transcript", 300)

        report = _sweep(sessions)

        assert report.stood_down is None
        assert not old_log.exists() and not old_tx.exists()
        assert young_tx.exists() and (sessions / "fresh.log").exists()
        assert report.removed == 2 and report.kept_young == 2

    def test_live_session_files_are_never_removed_however_old(self, sessions):
        live_log = _touch(sessions / "live-sid.log", 5000)
        live_tx = _touch(sessions / "live-sid.transcript", 5000)

        report = _sweep(sessions, live=["live-sid"])

        assert live_log.exists() and live_tx.exists()
        assert report.kept_live == 2 and report.removed == 0

    def test_dry_run_selects_but_deletes_nothing(self, sessions):
        old = _touch(sessions / "old.transcript", 400, body="abcd")

        report = _sweep(sessions, dry_run=True)

        assert old.exists()
        assert report.expired == [sessions / "old.transcript"]
        assert report.expired_bytes == 4 and report.removed == 0
        assert "would remove 1 file" in report.summary()

    def test_other_files_in_the_directory_are_ignored(self, sessions):
        other = _touch(sessions / "notes.txt", 4000)
        sub = sessions / "old.log.d"
        sub.mkdir()

        report = _sweep(sessions)

        assert other.exists() and sub.exists()
        assert report.ignored == 2

    def test_a_zero_window_is_a_caller_error_not_delete_everything(self, sessions):
        with pytest.raises(ValueError):
            _sweep(sessions, days=0)


class TestUnknownKeepsTheFile:
    """An age the sweep cannot establish is ``unknown``, and unknown keeps
    the file — it is never read as "old enough"."""

    def test_stat_failure_is_logged_and_kept(self, sessions, monkeypatch, caplog):
        target = _touch(sessions / "bad.log", 400)
        real_scandir = os.scandir

        class _StatFails:
            def __init__(self, entry):
                self.name = entry.name

            def is_file(self, follow_symlinks=True):
                return True

            def stat(self, follow_symlinks=True):
                raise PermissionError("access denied")

        monkeypatch.setattr(
            session_retention.os,
            "scandir",
            lambda d: [
                _StatFails(e) if e.name == "bad.log" else e for e in real_scandir(d)
            ],
        )
        with caplog.at_level("WARNING", logger=session_retention.logger.name):
            report = _sweep(sessions)
            session_retention.log_report(report)

        assert target.exists()
        assert report.removed == 0
        assert [n for n, _ in report.unknown] == ["bad.log"]
        assert any("bad.log" in r.getMessage() for r in caplog.records), caplog.text

    def test_future_mtime_is_unknown_not_young_or_old(self, sessions):
        future = sessions / "future.transcript"
        future.write_text("x", encoding="utf-8")
        t = NOW + 30 * DAY
        os.utime(future, (t, t))

        report = _sweep(sessions)

        assert future.exists()
        assert [n for n, _ in report.unknown] == ["future.transcript"]

    def test_unreachable_session_host_stands_the_whole_sweep_down(self, sessions):
        old = _touch(sessions / "old.log", 400)

        report = _sweep(sessions, live=None)

        assert old.exists()
        assert report.stood_down == session_retention.STOOD_DOWN_NO_LIVE_LIST
        assert report.expired == []

    def test_live_list_failure_reads_as_none_not_empty(self, monkeypatch):
        def _boom(port):
            raise session_client.SessionHostError("connection refused")

        monkeypatch.setattr(session_client, "list_sessions", _boom)
        assert session_retention.live_session_ids(8446) is None

    def test_empty_live_list_is_a_real_answer(self, monkeypatch):
        monkeypatch.setattr(session_client, "list_sessions", lambda port: [])
        assert session_retention.live_session_ids(8446) == set()

    def test_clock_that_makes_everything_old_stands_down(self, tmp_path):
        d = tmp_path / "sessions"
        d.mkdir()
        a = _touch(d / "a.log", 400)
        b = _touch(d / "b.transcript", 500)

        report = _sweep(d)

        assert a.exists() and b.exists()
        assert report.stood_down == session_retention.STOOD_DOWN_CLOCK

    def test_missing_directory_is_its_own_state(self, tmp_path):
        report = _sweep(tmp_path / "nope")
        assert report.stood_down == session_retention.STOOD_DOWN_DIR_ABSENT

    def test_unlink_failure_is_reported_not_counted_as_removed(
        self, sessions, monkeypatch
    ):
        old = _touch(sessions / "old.log", 400)

        def _locked(path):
            raise PermissionError("in use by another process")

        monkeypatch.setattr(session_retention.os, "unlink", _locked)
        report = _sweep(sessions)

        assert old.exists()
        assert report.removed == 0
        assert [n for n, _ in report.failed] == ["old.log"]


def _make_dir_link(link: Path, target: Path) -> None:
    """A directory junction on Windows (no privilege needed), else a symlink."""
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


class TestNeverLeavesTheDirectory:
    def test_sessions_dir_that_is_a_link_is_refused(self, tmp_path):
        real = tmp_path / "elsewhere"
        real.mkdir()
        _touch(real / "fresh.log", 0.1)
        precious = _touch(real / "old.log", 400)
        link = tmp_path / "sessions"
        _make_dir_link(link, real)

        report = _sweep(link)

        assert precious.exists()
        assert report.stood_down == session_retention.STOOD_DOWN_DIR_IS_LINK

    def test_link_entry_inside_is_kept_and_its_target_untouched(self, sessions, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        precious = _touch(outside / "keep.log", 400)
        link = sessions / "evil.transcript"
        _make_dir_link(link, outside)
        # The link's own mtime is real "now", months before the fixed NOW, so
        # a 30-day window puts it past the cutoff: only the link check keeps it.
        assert os.lstat(link).st_mtime < NOW - 30 * DAY

        report = _sweep(sessions, days=30)

        assert precious.exists() and link.exists()
        assert "evil.transcript" in [n for n, _ in report.unknown]
        assert report.removed == 0


class TestConfig:
    def test_default_window_is_365_days(self):
        assert WebappConfig().session_retention_days == 365

    def test_negative_window_is_rejected(self):
        with pytest.raises(ValueError):
            _validate(WebappConfig(session_retention_days=-1))

    def test_retention_is_not_patchable_from_the_settings_api(self, webapp_client):
        client, app, _ = webapp_client
        before = app.state.webapp_config.session_retention_days
        client.post("/api/config", json={"session_retention_days": 1})
        assert app.state.webapp_config.session_retention_days == before


def _app(**cfg_kw) -> SimpleNamespace:
    defaults = {"jobs_coverage_interval_minutes": 0, "session_host_port": 8446}
    defaults.update(cfg_kw)
    return SimpleNamespace(
        state=SimpleNamespace(webapp_config=SimpleNamespace(**defaults))
    )


class TestRetentionDaysCoercion:
    def test_missing_key_reads_as_off_not_as_the_default(self):
        # This tick deletes: an absent setting must never arm it.
        assert server._session_retention_days(SimpleNamespace()) == 0

    def test_garbage_reads_as_off(self):
        assert server._session_retention_days(
            SimpleNamespace(session_retention_days="forever")
        ) == 0


@pytest.mark.asyncio
class TestRetentionTick:
    async def _lifespan_tasks(self, app, monkeypatch):
        created = []
        real_create_task = asyncio.create_task

        def _spy(coro, *a, **k):
            task = real_create_task(coro, *a, **k)
            created.append(task)
            return task

        monkeypatch.setattr(server.asyncio, "create_task", _spy)
        monkeypatch.setattr(
            server, "_session_retention_tick", lambda a: asyncio.sleep(3600)
        )
        monkeypatch.setattr(server, "_coverage_tick", lambda a: asyncio.sleep(3600))
        monkeypatch.setattr(
            server, "_reconcile_orphan_mirror_windows", _noop_async
        )
        async with server._lifespan(app):
            pass
        return created

    async def test_canonical_instance_starts_and_cancels_it(self, monkeypatch):
        monkeypatch.delenv(SESSION_HOST_PORT_ENV, raising=False)
        monkeypatch.setattr(
            instance_role,
            "canonical_instance",
            lambda project_root=None: (True, instance_role.REASON_CANONICAL),
        )
        created = await self._lifespan_tasks(
            _app(session_retention_days=365), monkeypatch
        )
        assert len(created) == 1 and created[0].cancelled()

    async def test_zero_window_never_starts_it(self, monkeypatch):
        monkeypatch.setattr(
            instance_role,
            "canonical_instance",
            lambda project_root=None: (True, instance_role.REASON_CANONICAL),
        )
        assert await self._lifespan_tasks(
            _app(session_retention_days=0), monkeypatch
        ) == []

    async def test_disposable_instance_never_starts_it(self, monkeypatch):
        # The gate's webapp shares this checkout's webapp/sessions but talks
        # to an empty disposable session-host — its live list protects nothing.
        monkeypatch.setenv(SESSION_HOST_PORT_ENV, "54321")
        assert await self._lifespan_tasks(
            _app(session_retention_days=365), monkeypatch
        ) == []

    async def test_the_sweep_runs_off_the_event_loop(self, monkeypatch):
        offloaded = []

        async def _to_thread(fn, *a, **k):
            offloaded.append((fn, a))
            raise asyncio.CancelledError  # stop after the first cycle

        async def _no_sleep(_s):
            return None

        monkeypatch.setattr(server.asyncio, "to_thread", _to_thread)
        monkeypatch.setattr(server.asyncio, "sleep", _no_sleep)

        with pytest.raises(asyncio.CancelledError):
            await server._session_retention_tick(_app(session_retention_days=365))

        assert offloaded == [
            (session_retention.run_scheduled_sweep, (8446, 365))
        ]


async def _noop_async(*a, **k):
    return None


def test_scheduled_sweep_uses_the_audit_sessions_dir(tmp_path, monkeypatch):
    from src import audit

    d = tmp_path / "sessions"
    d.mkdir()
    old = d / "old.log"
    old.write_text("x", encoding="utf-8")
    t = time.time() - 400 * DAY
    os.utime(old, (t, t))
    (d / "fresh.log").write_text("x", encoding="utf-8")
    monkeypatch.setattr(audit, "_SESSIONS_DIR", d)
    monkeypatch.setattr(session_client, "list_sessions", lambda port: [])

    report = session_retention.run_scheduled_sweep(8446, 365)

    assert report.removed == 1 and not old.exists()
