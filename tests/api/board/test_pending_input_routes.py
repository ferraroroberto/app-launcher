"""Discarding a pending message: the webapp proxy, its passkey gate, and the
session-host route that answers it."""

from __future__ import annotations

from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from app.session_host import server
from app.webapp.middleware import _terminal_guard_level
from src.clients import session as session_client

_ROUTE = "/api/claude-code/sessions/s1/pending-input/e1"


def test_discard_is_passkey_gated():
    assert _terminal_guard_level(_ROUTE) == "passkey"


def test_discard_refused_off_tailnet(webapp_client):
    client, _, _ = webapp_client
    assert client.delete(_ROUTE).status_code == 403


def test_discard_forwards_and_mirrors_the_hosts_verdict(webapp_client, _bypass_gate):
    client, _, overrides = webapp_client
    host = overrides["session"]
    host.discard_pending_input.return_value = {"reason": "discarded", "ids": ["e1", "e2"]}

    resp = client.delete(_ROUTE)

    assert resp.status_code == 200
    assert resp.json()["ids"] == ["e1", "e2"]
    assert host.discard_pending_input.call_args.args == (8446, "s1", "e1")

    host.discard_pending_input.return_value = {"reason": "discarding", "ids": ["e1"]}
    assert client.delete(_ROUTE).status_code == 202


def test_a_refusal_keeps_the_hosts_status_and_detail(webapp_client, _bypass_gate):
    client, _, overrides = webapp_client
    overrides["session"].discard_pending_input.side_effect = session_client.SessionHostError(
        "The agent is between turns and taking it now", status=409
    )

    resp = client.delete(_ROUTE)

    assert resp.status_code == 409
    assert "between turns" in resp.json()["detail"]


def test_session_host_route_answers_with_the_discards_status(monkeypatch):
    session = MagicMock()
    session.discard_pending_input.return_value = (202, {"reason": "discarding", "ids": ["e1"]})
    monkeypatch.setattr(server.manager, "get", lambda sid: session)

    resp = TestClient(server.app).delete("/sessions/s1/pending-input/e1")

    assert resp.status_code == 202
    assert resp.json()["reason"] == "discarding"
    session.discard_pending_input.assert_called_once_with("e1")


def test_session_host_route_refuses_a_session_that_tracks_nothing(monkeypatch):
    session = MagicMock(spec=["submit_input", "to_api"])
    monkeypatch.setattr(server.manager, "get", lambda sid: session)

    resp = TestClient(server.app).delete("/sessions/s1/pending-input/e1")

    assert resp.status_code == 409
