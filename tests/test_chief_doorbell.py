"""The chief doorbell (#1645): count, decide, ring, and stay off by default.

Fakes throughout: no test talks to a session-host or types into a session.
``observe()`` runs against the real Board overlay (:func:`board.merge_sessions`)
with a faked session list and a real hook-state file in ``tmp_path``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.webapp import chief_doorbell as bell_mod
from app.webapp import server
from app.webapp.chief_doorbell import (
    BACKOFF_SECONDS,
    CLAIM_STALE_MS,
    DEBOUNCE_SECONDS,
    DOORBELL_LINE,
    Doorbell,
    Observation,
    _SkipLog,
    count_pending,
    observe,
    tick_once,
)
from src import instance_role, session_client
from src.webapp_config import SESSION_HOST_PORT_ENV

NOW_MS = 1_800_000_000_000
CHIEF_SID = "chief-launcher-sid"


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")


# --- count_pending: the mailbox contract's counting rule ------------------


class TestCountPending:
    def test_missing_inbox_is_zero(self, tmp_path):
        assert count_pending(tmp_path / "chief-inbox", NOW_MS) == 0

    def test_counts_root_json_and_ignores_the_rest(self, tmp_path):
        inbox = tmp_path / "chief-inbox"
        _touch(inbox / f"{NOW_MS}-{'a' * 32}.json")
        _touch(inbox / f"{NOW_MS}-deadbeef.json")  # a v1 name still counts
        _touch(inbox / ".event.x1y2.tmp")
        _touch(inbox / ".hidden.json")
        _touch(inbox / "notes.txt")
        _touch(inbox / ".claim-123-456" / f"{NOW_MS}-b.json")  # wait-event's folder
        (inbox / "dir.json").mkdir()
        assert count_pending(inbox, NOW_MS) == 2

    def test_fresh_claims_are_held_stale_and_unstamped_count(self, tmp_path):
        claimed = tmp_path / "chief-inbox" / ".claimed"
        _touch(claimed / f"{NOW_MS - 1000}-fresh.json")
        _touch(claimed / f"{NOW_MS - CLAIM_STALE_MS + 1}-just-fresh.json")
        _touch(claimed / f"{NOW_MS - CLAIM_STALE_MS}-boundary.json")  # >= is stale
        _touch(claimed / f"{NOW_MS - CLAIM_STALE_MS * 2}-old.json")
        _touch(claimed / "no-stamp.json")
        _touch(claimed / ".tmp.json")
        (claimed / "sub").mkdir()
        _touch(claimed / "sub" / "1-x.json")
        assert count_pending(tmp_path / "chief-inbox", NOW_MS) == 3

    def test_root_and_stale_claims_add_up(self, tmp_path):
        inbox = tmp_path / "chief-inbox"
        _touch(inbox / f"{NOW_MS}-a.json")
        _touch(inbox / ".claimed" / "1-b.json")
        assert count_pending(inbox, NOW_MS) == 2

    def test_unlistable_inbox_is_unknown_never_zero(self, tmp_path):
        inbox = tmp_path / "chief-inbox"
        inbox.write_text("a file where the directory should be", encoding="utf-8")
        assert count_pending(inbox, NOW_MS) is None

    def test_unlistable_claimed_dir_is_unknown(self, tmp_path):
        inbox = tmp_path / "chief-inbox"
        _touch(inbox / f"{NOW_MS}-a.json")
        (inbox / ".claimed").write_text("not a directory", encoding="utf-8")
        assert count_pending(inbox, NOW_MS) is None


# --- Doorbell.decide: the debounce + back-off state machine ---------------


def _obs(**kw) -> Observation:
    base = dict(pending=2, chief_sid=CHIEF_SID, status="idle-finished", dirty=False)
    base.update(kw)
    return Observation(**base)


class TestDecide:
    def test_rings_once_after_the_debounce(self):
        bell = Doorbell()
        assert bell.decide(_obs(), 0.0) == "debouncing"
        assert bell.decide(_obs(), DEBOUNCE_SECONDS - 0.1) == "debouncing"
        assert bell.decide(_obs(), DEBOUNCE_SECONDS) == "ring"
        assert bell.decide(_obs(), DEBOUNCE_SECONDS + 3) == "backing off after ring"

    def test_awaiting_input_is_idle_too(self):
        bell = Doorbell()
        bell.decide(_obs(status="awaiting-input"), 0.0)
        assert bell.decide(_obs(status="awaiting-input"), DEBOUNCE_SECONDS) == "ring"

    @pytest.mark.parametrize(
        "status",
        ["working", "tool-pending", "waiting-on-task", "awaiting-decision", "stalled", "unknown"],
    )
    def test_never_rings_a_chief_that_is_not_idle(self, status):
        """``tool-pending`` is a permission prompt and ``awaiting-decision`` a
        picker: a typed line would land in the dialog, not the composer."""
        bell = Doorbell()
        for t in range(0, 300, 3):
            assert bell.decide(_obs(status=status), float(t)) != "ring"

    def test_never_rings_when_the_status_is_unknown(self):
        bell = Doorbell()
        for t in range(0, 30, 3):
            assert bell.decide(_obs(status=None), float(t)) == "chief status unknown"

    @pytest.mark.parametrize("dirty, reason", [(True, "composer dirty"), (None, "composer unknown")])
    def test_never_rings_over_a_dirty_or_unknown_composer(self, dirty, reason):
        """``None`` is a session-host too old to send ``composer_dirty`` (the
        live host until its next restart): it must never ring."""
        bell = Doorbell()
        for t in range(0, 300, 3):
            assert bell.decide(_obs(dirty=dirty), float(t)) == reason

    @pytest.mark.parametrize("pending, reason", [(0, "mailbox empty"), (None, "pending unknown")])
    def test_never_rings_on_an_empty_or_unknown_mailbox(self, pending, reason):
        bell = Doorbell()
        for t in range(0, 30, 3):
            assert bell.decide(_obs(pending=pending), float(t)) == reason

    def test_a_blocked_read_never_rings(self):
        bell = Doorbell()
        obs = Observation(pending=1, blocked="no live chief")
        assert bell.decide(obs, 0.0) == "no live chief"
        assert bell.decide(obs, 10.0) == "no live chief"

    def test_a_break_restarts_the_debounce(self):
        bell = Doorbell()
        bell.decide(_obs(), 0.0)
        bell.decide(_obs(dirty=True), 2.0)
        assert bell.decide(_obs(), 3.0) == "debouncing"
        assert bell.decide(_obs(), 3.0 + DEBOUNCE_SECONDS) == "ring"

    def test_re_rings_after_the_back_off_when_the_condition_persists(self):
        """A chief still idle with pending events after the back-off never got
        the ring: it rings again, and again after the next back-off."""
        bell = Doorbell()
        bell.decide(_obs(), 0.0)
        rang_at = DEBOUNCE_SECONDS
        assert bell.decide(_obs(), rang_at) == "ring"
        t = rang_at
        while t < rang_at + BACKOFF_SECONDS - 3:
            t += 3
            assert bell.decide(_obs(), t) == "backing off after ring"
        assert bell.decide(_obs(), rang_at + BACKOFF_SECONDS) == "debouncing"
        assert bell.decide(_obs(), rang_at + BACKOFF_SECONDS + DEBOUNCE_SECONDS) == "ring"

    def test_a_busy_chief_ends_the_back_off(self):
        """The chief went busy after the ring, so it landed: new pending
        events after its next turn ring after one debounce, not a back-off."""
        bell = Doorbell()
        bell.decide(_obs(), 0.0)
        assert bell.decide(_obs(), DEBOUNCE_SECONDS) == "ring"
        bell.decide(_obs(status="working"), 6.0)
        bell.decide(_obs(), 9.0)
        assert bell.decide(_obs(), 9.0 + DEBOUNCE_SECONDS) == "ring"

    def test_a_drained_mailbox_ends_the_back_off(self):
        bell = Doorbell()
        bell.decide(_obs(), 0.0)
        assert bell.decide(_obs(), DEBOUNCE_SECONDS) == "ring"
        bell.decide(_obs(pending=0), 6.0)
        bell.decide(_obs(), 9.0)
        assert bell.decide(_obs(), 9.0 + DEBOUNCE_SECONDS) == "ring"

    def test_a_dirty_composer_does_not_end_the_back_off(self):
        """Typing alone proves nothing about the ring, so it keeps the back-off."""
        bell = Doorbell()
        bell.decide(_obs(), 0.0)
        assert bell.decide(_obs(), DEBOUNCE_SECONDS) == "ring"
        bell.decide(_obs(dirty=True), 6.0)
        assert bell.decide(_obs(), 9.0) == "backing off after ring"


# --- tick_once: the toggle, the ring, and failures ------------------------


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def _cfg(**kw):
    base = dict(chief_doorbell=True, session_host_port=54321)
    base.update(kw)
    return SimpleNamespace(**base)


async def _run_ticks(cfg, obs, *, ticks=3, send=None):
    """Tick ``ticks`` times, 3 s apart; return (decisions, sends, observes)."""
    sends, observes = [], []
    clock = _Clock()

    async def fake_observe(c):
        observes.append(c)
        return obs

    def fake_send(port, sid, data, submit=True):
        sends.append((port, sid, data, submit))
        if send is not None:
            return send()
        return {"reason": "ok"}

    bell, skips = Doorbell(), _SkipLog()
    decisions = []
    for _ in range(ticks):
        decisions.append(await tick_once(
            cfg, bell, skips, now=clock, observe_fn=fake_observe, send_fn=fake_send,
        ))
        clock.t += 3.0
    return decisions, sends, observes


class TestTickOnce:
    async def test_toggle_off_does_nothing(self):
        """Acceptance: with the toggle off the tick never reads and never
        makes an ``/input`` call, however ringable the chief looks."""
        decisions, sends, observes = await _run_ticks(_cfg(chief_doorbell=False), _obs(), ticks=30)
        assert decisions == ["off"] * 30
        assert sends == [] and observes == []

    @pytest.mark.parametrize("value", ["true", 1, None])
    async def test_only_a_real_true_arms_it(self, value):
        _decisions, sends, observes = await _run_ticks(_cfg(chief_doorbell=value), _obs(), ticks=5)
        assert sends == [] and observes == []

    async def test_a_config_without_the_key_is_off(self):
        _decisions, sends, _ = await _run_ticks(SimpleNamespace(session_host_port=1), _obs())
        assert sends == []

    async def test_no_config_is_off(self):
        _decisions, sends, _ = await _run_ticks(None, _obs())
        assert sends == []

    async def test_rings_once_with_the_fixed_line_and_submit(self):
        decisions, sends, _ = await _run_ticks(_cfg(), _obs(), ticks=5)
        assert decisions == ["debouncing", "ring"] + ["backing off after ring"] * 3
        assert sends == [(54321, CHIEF_SID, DOORBELL_LINE, True)]

    async def test_never_rings_an_old_host_that_sends_no_composer_state(self):
        _decisions, sends, _ = await _run_ticks(_cfg(), _obs(dirty=None), ticks=40)
        assert sends == []

    async def test_a_refused_write_is_a_logged_skip_and_waits_the_back_off(self, caplog):
        def refuse():
            raise session_client.SessionHostError(502, "never echoed")

        with caplog.at_level(logging.WARNING, logger=bell_mod.logger.name):
            decisions, sends, _ = await _run_ticks(_cfg(), _obs(), ticks=4, send=refuse)
        assert decisions[1] == "ring failed"
        assert decisions[2:] == ["backing off after ring"] * 2
        assert len(sends) == 1
        assert "chief doorbell ring failed" in caplog.text

    async def test_a_failed_observe_is_a_skip_not_a_crash(self):
        async def boom(c):
            raise RuntimeError("state file locked")

        decision = await tick_once(_cfg(), Doorbell(), _SkipLog(), observe_fn=boom)
        assert decision.startswith("observe failed")


def test_the_line_is_the_contracts_fixed_data_free_line():
    """fleet-config docs/chief-mailbox.md "Doorbell line", verbatim. It must
    never carry a count or worker text (fleet-config#622)."""
    assert DOORBELL_LINE == "⟳ chief-inbox: pending — run chief_ops.py inbox"
    assert not any(ch.isdigit() for ch in DOORBELL_LINE)


class TestLogging:
    async def test_a_ring_and_each_skip_reason_change_are_logged_once(self, caplog):
        with caplog.at_level(logging.INFO, logger=bell_mod.logger.name):
            await _run_ticks(_cfg(), _obs(), ticks=6)
        lines = [r.getMessage() for r in caplog.records]
        assert sum("chief doorbell rang" in m for m in lines) == 1
        assert sum("skip: debouncing" in m for m in lines) == 1
        assert sum("skip: backing off after ring" in m for m in lines) == 1
        rang = next(m for m in lines if "rang" in m)
        assert "idle=True" in rang and "dirty=False" in rang and "pending=2" in rang

    async def test_off_is_logged_once(self, caplog):
        with caplog.at_level(logging.INFO, logger=bell_mod.logger.name):
            await _run_ticks(_cfg(chief_doorbell=False), _obs(), ticks=10)
        assert sum("skip: off" in r.getMessage() for r in caplog.records) == 1


# --- observe(): the real Board overlay over faked reads -------------------


#: Marks a session field the fake session-host omits entirely.
_MISSING = object()


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


def _setup(tmp_path, monkeypatch, *, status="needs-you", chief_extra=None,
           live=None, host_error=None, events=1):
    inbox = tmp_path / "chief-inbox"
    for n in range(events):
        _touch(inbox / f"{NOW_MS + n}-{n:032x}.json")
    state_file = tmp_path / "sessions-state.json"
    now = datetime.now(timezone.utc)
    state_file.write_text(json.dumps({
        "conv-chief": {
            "project": "fleet-config",
            "status": status,
            "transcript_path": None,
            "cwd": "E:/automation/fleet-config",
            "agent": "claude",
            "launcher_session_id": CHIEF_SID,
            "updated_at": _iso(now - timedelta(seconds=30)),
        }
    }), encoding="utf-8")
    chief = {
        "session_id": CHIEF_SID,
        "kind": "pty",
        "agent": "claude",
        "label": "chief",
        "project_dir": "E:/automation/fleet-config",
        "name": "fleet-config",
        "alive": True,
        "started_at": _iso(now - timedelta(hours=1)),
        "live_title": "",
        "prompt_title": "/chief",
        "composer_dirty": False,
    }
    chief.update(chief_extra or {})
    for key, value in list(chief.items()):
        if value is _MISSING:
            del chief[key]
    sessions = [chief] if live is None else live
    reads = []

    def fake_read_live(port):
        reads.append(port)
        return (sessions if host_error is None else []), host_error

    monkeypatch.setattr(bell_mod, "_read_live_sessions", fake_read_live)
    cfg = SimpleNamespace(
        chief_doorbell=True,
        chief_inbox_dir=str(inbox),
        sessions_state_file=str(state_file),
        session_host_port=54321,
    )
    return cfg, reads


class TestObserve:
    async def test_an_idle_chief_with_a_clean_composer_is_ringable(self, tmp_path, monkeypatch):
        cfg, _ = _setup(tmp_path, monkeypatch, events=2)
        obs = await observe(cfg)
        assert obs.chief_sid == CHIEF_SID
        assert obs.status in bell_mod.IDLE_STATUSES and obs.idle is True
        assert obs.dirty is False and obs.pending == 2

    async def test_a_working_chief_is_not_idle(self, tmp_path, monkeypatch):
        cfg, _ = _setup(tmp_path, monkeypatch, status="working")
        obs = await observe(cfg)
        assert obs.status == "working" and obs.idle is False

    async def test_a_busy_title_overrides_the_needs_you_stamp(self, tmp_path, monkeypatch):
        """The same overlay as the Board: a fresh spinner title is working."""
        import time as _time
        cfg, _ = _setup(tmp_path, monkeypatch, chief_extra={
            "live_title": "⠂ chief", "last_output_at": _time.time(),
        })
        obs = await observe(cfg)
        assert obs.idle is False

    async def test_an_old_host_without_composer_state_reads_unknown(self, tmp_path, monkeypatch):
        """The live session-host predates #1643 until its next restart: the
        field is absent, which is unknown, and the bell never rings."""
        cfg, _ = _setup(tmp_path, monkeypatch, chief_extra={"composer_dirty": _MISSING})
        obs = await observe(cfg)
        assert obs.dirty is None
        bell = Doorbell()
        assert [bell.decide(obs, float(t)) for t in (0, 3, 6)] == ["composer unknown"] * 3

    async def test_a_dirty_composer_reads_dirty(self, tmp_path, monkeypatch):
        cfg, _ = _setup(tmp_path, monkeypatch, chief_extra={"composer_dirty": True})
        assert (await observe(cfg)).dirty is True

    async def test_an_empty_mailbox_reads_no_sessions(self, tmp_path, monkeypatch):
        cfg, reads = _setup(tmp_path, monkeypatch, events=0)
        obs = await observe(cfg)
        assert obs.pending == 0 and reads == []

    async def test_no_live_chief_is_blocked(self, tmp_path, monkeypatch):
        cfg, _ = _setup(tmp_path, monkeypatch, live=[])
        assert (await observe(cfg)).blocked == "no live chief"

    async def test_a_dead_chief_is_not_the_chief(self, tmp_path, monkeypatch):
        cfg, _ = _setup(tmp_path, monkeypatch, chief_extra={"alive": False})
        assert (await observe(cfg)).blocked == "no live chief"

    async def test_an_unreadable_host_is_blocked(self, tmp_path, monkeypatch):
        cfg, _ = _setup(tmp_path, monkeypatch, host_error="connection refused")
        obs = await observe(cfg)
        assert obs.blocked and "connection refused" in obs.blocked


# --- lifespan: canonical only, cancelled on shutdown ----------------------


async def _noop_async(*a, **k):
    return None


@pytest.fixture(autouse=True)
def _no_canonical_override(monkeypatch):
    monkeypatch.delenv(instance_role.CANONICAL_OVERRIDE_ENV, raising=False)


class TestLifespan:
    async def _doorbell_ticks(self, monkeypatch, app):
        ticks = []
        real_create_task = asyncio.create_task

        def _spy(coro, *a, **k):
            module = coro.cr_frame.f_globals.get("__name__") if coro.cr_frame else ""
            if module == bell_mod.__name__:
                coro.close()
                task = real_create_task(asyncio.sleep(3600))
                ticks.append(task)
                return task
            if coro.__qualname__ == "tick_forever":
                coro.close()
                return real_create_task(asyncio.sleep(3600))
            return real_create_task(coro, *a, **k)

        monkeypatch.setattr(server.asyncio, "create_task", _spy)
        monkeypatch.setattr(server, "_coverage_tick", lambda a: asyncio.sleep(3600))
        monkeypatch.setattr(server, "_reconcile_orphan_mirror_windows", _noop_async)
        async with server._lifespan(app):
            pass
        return ticks

    @staticmethod
    def _app():
        cfg = SimpleNamespace(jobs_coverage_interval_minutes=0, chief_doorbell=False)
        return SimpleNamespace(state=SimpleNamespace(webapp_config=cfg))

    async def test_canonical_instance_starts_and_cancels_it(self, monkeypatch):
        """Started even with the toggle off: the toggle is read every tick,
        so turning it on in Settings needs no restart."""
        monkeypatch.delenv(SESSION_HOST_PORT_ENV, raising=False)
        monkeypatch.setattr(
            instance_role, "canonical_instance",
            lambda project_root=None: (True, instance_role.REASON_CANONICAL),
        )
        ticks = await self._doorbell_ticks(monkeypatch, self._app())
        assert len(ticks) == 1 and ticks[0].cancelled()

    async def test_disposable_instance_never_starts_it(self, monkeypatch):
        """The gate's webapp must never type into a session."""
        monkeypatch.setenv(SESSION_HOST_PORT_ENV, "54321")
        assert await self._doorbell_ticks(monkeypatch, self._app()) == []


def test_the_log_file_handler_is_attached_once(tmp_path, monkeypatch):
    from app.webapp import observability

    path = tmp_path / "webapp" / "chief-doorbell.log"
    monkeypatch.setattr(observability, "_DOORBELL_LOG_PATH", path)
    target = logging.getLogger("test.chief_doorbell.handler")
    try:
        observability.ensure_doorbell_log_handler(target)
        observability.ensure_doorbell_log_handler(target)
        handlers = [h for h in target.handlers if isinstance(h, logging.FileHandler)]
        assert len(handlers) == 1
        target.info("🔔 chief doorbell rang")
        handlers[0].flush()
        assert "chief doorbell rang" in path.read_text(encoding="utf-8")
    finally:
        for h in list(target.handlers):
            target.removeHandler(h)
            h.close()
    assert os.path.exists(path)
