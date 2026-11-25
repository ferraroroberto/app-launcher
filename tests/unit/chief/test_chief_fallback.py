"""The chief's fallback wake: what it reads, when it rings, and that 0 is off.

A fake clock throughout; no test talks to a session-host or types into a
session (the ring is a recording fake).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace

from app.webapp import chief_fallback as fb
from app.webapp.chief_doorbell import CLAIM_STALE_MS, Observation, SkipLog
from app.webapp.chief_fallback import Facts, Fallback

NOW_MS = 1_800_000_000_000
MIN = 60.0
IDLE = dict(chief_sid="chief-sid", status="idle-finished", output_age=1.0)


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")


def _cfg(tmp_path: Path, minutes: int = 30, **kw) -> SimpleNamespace:
    base = dict(
        chief_fallback_minutes=minutes, chief_worker_cap=3, chief_worker_cap_scope="chief",
        chief_inbox_dir=str(tmp_path / "inbox"), chief_plan_file=str(tmp_path / "plan.json"),
        chief_managed_file=str(tmp_path / "managed.json"), session_host_port=1,
    )
    return SimpleNamespace(**{**base, **kw})


def _chief(dirty=False, **kw) -> Observation:
    return Observation(pending=None, dirty=dirty, **{**IDLE, **kw})


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class _Ring:
    def __init__(self) -> None:
        self.calls = []

    async def __call__(self, cfg, obs, why, *, log):
        self.calls.append(why)
        log.info("🔔 chief %s rang", why)
        return "ring"


# --- what it reads ---------------------------------------------------------


def test_oldest_pending_age_reads_publish_and_stale_claim_stamps(tmp_path):
    inbox = tmp_path / "inbox"
    assert fb.oldest_pending_age(inbox, NOW_MS) is None
    _touch(inbox / f"{NOW_MS - 5 * 60_000}-a.json")
    _touch(inbox / "no-stamp.json")
    _touch(inbox / ".event.x.tmp")
    _touch(inbox / ".claimed" / f"{NOW_MS - 60_000}-fresh.json")  # held, not pending
    assert fb.oldest_pending_age(inbox, NOW_MS) == 300.0
    _touch(inbox / ".claimed" / f"{NOW_MS - CLAIM_STALE_MS * 2}-stale.json")
    assert fb.oldest_pending_age(inbox, NOW_MS) == CLAIM_STALE_MS * 2 / 1000


def test_queued_rows_and_free_slots(tmp_path):
    cfg = _cfg(tmp_path)
    plan = Path(cfg.chief_plan_file)
    assert fb.queued_rows(plan) == 0
    plan.write_text(json.dumps({"version": 1, "queue": [
        {"repo": "r", "ref": "#1", "status": "queued"},
        {"repo": "r", "ref": "#2", "status": "building"},
        {"repo": "r", "ref": "#3", "status": "queued"},
    ]}), encoding="utf-8")
    assert fb.queued_rows(plan) == 2
    plan.write_text("not json", encoding="utf-8")
    assert fb.queued_rows(plan) is None

    cards = [
        {"session_id": "w1", "alive": True, "kind": "pty"},
        {"session_id": "mine", "alive": True, "kind": "pty"},  # owner's own, unmanaged
        {"session_id": "chief-sid", "alive": True, "kind": "pty", "label": "chief"},
        {"session_id": "tg", "alive": True, "kind": "pty", "label": "telegram:x"},
        {"session_id": "ext", "alive": None, "kind": "external"},
        {"session_id": "w2", "alive": False, "kind": "pty"},
    ]
    Path(cfg.chief_managed_file).write_text(json.dumps({"w1": {}, "w2": {}}), encoding="utf-8")
    assert fb.free_slots(cfg, cards, "chief-sid") == 2
    assert fb.free_slots(_cfg(tmp_path, chief_worker_cap_scope="all"), cards, "chief-sid") == 1
    Path(cfg.chief_managed_file).write_text("[]", encoding="utf-8")
    assert fb.free_slots(cfg, cards, "chief-sid") is None  # unreadable: unknown, never 3


# --- when it rings (fake clock) --------------------------------------------


async def test_missed_mail_rings_after_n_minutes_and_logs_the_reason(tmp_path, caplog):
    """The doorbell missed an event: nothing until it has waited N minutes,
    then one ring whose log line names why, then nothing for N minutes."""
    clock, ring, check, skips = _Clock(), _Ring(), Fallback(), SkipLog("fallback", fb.logger)
    facts = {"f": Facts(oldest_age=29 * MIN, queued=0, chief=_chief())}

    async def observe(cfg):
        return facts["f"]

    async def tick():
        return await fb.tick_once(_cfg(tmp_path), check, skips, now=clock,
                                  observe_fn=observe, ring_fn=ring)

    caplog.set_level(logging.INFO, logger=fb.logger.name)
    assert await tick() == "nothing waiting"
    facts["f"] = Facts(oldest_age=31 * MIN, queued=0, chief=_chief())
    assert await tick() == "ring"
    assert ring.calls == ["fallback (mailbox event waited 31 min)"]
    assert "mailbox event waited 31 min" in caplog.text
    clock.t += 29 * MIN
    assert await tick() == "rang within the last period"
    clock.t += 2 * MIN
    assert await tick() == "ring"
    assert len(ring.calls) == 2


def test_free_slots_with_queued_rows_ring_once():
    check = Fallback()
    facts = Facts(oldest_age=None, queued=2, free_slots=1, chief=_chief())
    assert check.decide(facts, 30, 0.0) == (
        "ring", "1 free slot(s) while 2 plan row(s) queued")
    assert check.decide(facts, 30, 10 * MIN)[0] == "rang within the last period"
    full = Facts(oldest_age=None, queued=2, free_slots=0, chief=_chief())
    assert Fallback().decide(full, 30, 0.0)[0] == "nothing waiting"


def test_it_keeps_the_doorbell_safety():
    waiting = dict(oldest_age=40 * MIN, queued=0)
    assert Fallback().decide(Facts(**waiting, chief=_chief(status="working")), 30, 0.0)[0] \
        == "chief not idle (working)"
    assert Fallback().decide(Facts(**waiting, chief=_chief(dirty=True)), 30, 0.0)[0] \
        == "composer dirty"
    blocked = Observation(pending=None, blocked="no live chief")
    assert Fallback().decide(Facts(**waiting, chief=blocked), 30, 0.0)[0] == "no live chief"


def test_an_unknown_composer_holds_the_ring_for_n_minutes_then_rings_once():
    check = Fallback()
    facts = Facts(oldest_age=40 * MIN, queued=0, chief=_chief(dirty=None))
    assert check.decide(facts, 30, 0.0)[0] == "composer unknown"
    assert check.decide(facts, 30, 29 * MIN)[0] == "composer unknown"
    decision, why = check.decide(facts, 30, 30 * MIN)
    assert decision == "ring" and "composer unknown for 30 min" in why
    assert check.decide(facts, 30, 31 * MIN)[0] == "rang within the last period"


async def test_zero_turns_it_off(tmp_path):
    ring = _Ring()

    async def observe(cfg):  # pragma: no cover - must never run
        raise AssertionError("off must read nothing")

    decision = await fb.tick_once(_cfg(tmp_path, minutes=0), Fallback(), SkipLog(),
                                  observe_fn=observe, ring_fn=ring)
    assert decision == "off" and ring.calls == []
    assert fb.fallback_minutes(SimpleNamespace()) == 0  # a stub config is off too


async def test_observe_reads_no_sessions_while_nothing_waits(tmp_path, monkeypatch):
    async def no_chief(cfg, pending):  # pragma: no cover - must never run
        raise AssertionError("steady state must not read sessions")

    monkeypatch.setattr(fb.chief_doorbell, "read_chief", no_chief)
    facts = await fb.observe(_cfg(tmp_path))
    assert facts == Facts(oldest_age=None, queued=0)
