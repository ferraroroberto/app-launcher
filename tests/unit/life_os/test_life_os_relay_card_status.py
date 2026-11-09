"""The relay reads a session's *declared card status*, not the raw hook stamp.

Unlike its sibling relay tests, nothing here replaces ``_turn_facts``: the real
function runs over a Board whose derived cards are scripted, so a session at
rest under any declared status is judged by ``card_status`` and not by a
literal list in the router. Fictional sessions throughout.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.webapp.routers import life_os_relay
from src.board import card_status
from tests.unit.life_os.test_life_os_relay import (  # noqa: F401
    PREFIX,
    _conversation_session,
    relay,
)
from tests.unit.life_os.test_life_os_relay_stop import (  # noqa: F401
    _reopen,
    _stop,
    stop_host,
)

# Captured at import, before any fixture swaps the router's facts for a fake.
REAL_TURN_FACTS = life_os_relay._turn_facts


@pytest.fixture
def derived_host(stop_host, monkeypatch):  # noqa: F811
    """``stop_host`` with the router's real ``_turn_facts`` over scripted cards."""
    host = stop_host
    host.cards = {}
    host.turn_at = {}

    def merge(live, rows, **_kw):
        return [{"session_id": s["session_id"], "status": host.cards.get(s["session_id"])}
                for s in live if s["session_id"] in host.cards]

    def attach(live, rows):
        return [{**s, "last_turn_at": host.turn_at.get(s["session_id"])} for s in live]

    monkeypatch.setattr(life_os_relay, "_turn_facts", REAL_TURN_FACTS)
    monkeypatch.setattr(life_os_relay.board, "merge_sessions", merge)
    monkeypatch.setattr(life_os_relay.board, "attach_last_turn", attach)
    return host


def _caller(host):
    """A real ``_Caller`` over the host's config, sessions and ``health`` profile."""
    profile = SimpleNamespace(id="health")
    return life_os_relay._Caller(cfg=host.client.app.state.webapp_config, profile=profile,
                                 skill=None, sessions=host.sessions)


def _reopen_with_card(host, status):
    body = _reopen(host)
    sid = body["session_id"]
    host.cards[sid] = status
    capture = Path(host.client.app.state.webapp_config.life_os_dir) / body["conversation"]
    host.turn_at[sid] = capture.stat().st_mtime - 5
    return sid


@pytest.mark.parametrize("status", sorted(card_status.CARD_STATUSES))
def test_turn_facts_report_the_derived_card_status(derived_host, status):
    sid = _reopen_with_card(derived_host, status)
    caller = _caller(derived_host)
    facts = asyncio.run(life_os_relay._turn_facts(caller))
    # ``unknown`` means the Board cannot place the session: not a known status.
    assert facts[sid]["status"] == (None if status == card_status.UNKNOWN else status)


def test_a_session_with_no_card_has_no_known_status(derived_host):
    sid = _reopen_with_card(derived_host, card_status.IDLE)
    del derived_host.cards[sid]
    caller = _caller(derived_host)
    assert asyncio.run(life_os_relay._turn_facts(caller))[sid]["status"] is None


@pytest.mark.parametrize("status", sorted(card_status.QUIT_SAFE))
def test_stop_closes_a_session_at_rest_under_any_quit_safe_status(derived_host, status):
    sid = _reopen_with_card(derived_host, status)
    resp = _stop(derived_host, sid)
    assert resp.status_code == 200, resp.text
    assert derived_host.stops == [(sid, "quit")]


@pytest.mark.parametrize(
    "status", sorted(set(card_status.CARD_STATUSES) - card_status.QUIT_SAFE))
def test_stop_refuses_every_other_status(derived_host, status):
    sid = _reopen_with_card(derived_host, status)
    resp = _stop(derived_host, sid)
    assert resp.status_code == 409
    assert resp.json()["detail"]["state"] == "busy"
    assert derived_host.stops == []


def test_idle_finished_is_quit_safe_so_a_stamp_the_old_literal_lacked_is_not_busy():
    assert card_status.IDLE_FINISHED in card_status.QUIT_SAFE
    assert card_status.IDLE_FINISHED not in ("idle", "needs-you")


@pytest.mark.parametrize("status", sorted(card_status.TURN_ENDED))
def test_the_first_turn_is_ready_under_any_ended_status(derived_host, monkeypatch, status):
    monkeypatch.setattr(life_os_relay, "NEW_READY_WAIT_SECONDS", 0.05)
    sid = "ready-1"
    derived_host.sessions.append(_conversation_session(sid, "x"))
    derived_host.cards[sid] = status
    derived_host.turn_at[sid] = time.time() + 30
    caller = _caller(derived_host)
    assert asyncio.run(life_os_relay._await_first_idle(caller, sid, 0.0)) is True


@pytest.mark.parametrize("status", [card_status.WORKING, card_status.UNKNOWN])
def test_the_first_turn_is_not_ready_while_working_or_unknown(derived_host, monkeypatch, status):
    monkeypatch.setattr(life_os_relay, "NEW_READY_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(life_os_relay, "NEW_READY_POLL_SECONDS", 0.01)
    sid = "ready-2"
    derived_host.sessions.append(_conversation_session(sid, "x"))
    derived_host.cards[sid] = status
    derived_host.turn_at[sid] = time.time() + 30
    caller = _caller(derived_host)
    assert asyncio.run(life_os_relay._await_first_idle(caller, sid, 0.0)) is False


def test_read_reports_the_derived_status(derived_host):
    sid = _reopen_with_card(derived_host, card_status.IDLE_FINISHED)
    body = derived_host.client.get(
        f"{PREFIX}/read", headers=derived_host.headers, params={"session_id": sid}).json()
    assert body["status"] == card_status.IDLE_FINISHED


def test_no_status_literal_is_left_in_the_router():
    source = Path(life_os_relay.__file__).read_text(encoding="utf-8")
    # ``unknown`` is also the relay's refusal state name, not a card status here.
    declared = (card_status.CARD_STATUSES.keys() | {card_status.NEEDS_YOU_STAMP}) - {"unknown"}
    for literal in declared:
        assert f'"{literal}"' not in source and f"'{literal}'" not in source, literal


def test_read_does_not_call_an_unplaceable_session_settled(derived_host):
    sid = _reopen_with_card(derived_host, card_status.UNKNOWN)
    body = derived_host.client.get(
        f"{PREFIX}/read", headers=derived_host.headers, params={"session_id": sid}).json()
    assert body["status"] is None


def test_declared_relay_groupings():
    assert card_status.QUIT_SAFE == {"idle", "idle-finished"}
    assert card_status.TURN_ENDED == set(card_status.CARD_STATUSES) - {"working", "unknown"}
