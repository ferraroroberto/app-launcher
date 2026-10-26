"""``POST /sessions/{sid}/rename`` (app/session_host/server.py) — issue #458.

The session-host HTTP surface for the manual title override: forwards the
body's ``title`` to ``SessionManager.rename`` and returns the session's
``to_api()`` (which now carries ``manual_title``), or 404 for an unknown id.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from app.session_host import server


def test_rename_forwards_title_and_returns_session(monkeypatch):
    session = MagicMock()
    session.to_api.return_value = {"session_id": "sid", "manual_title": "custom"}
    monkeypatch.setattr(server.manager, "get", lambda sid: session)
    renamed: dict = {}
    monkeypatch.setattr(
        server.manager, "rename",
        lambda sid, title: renamed.update(sid=sid, title=title) or session,
    )
    client = TestClient(server.app)

    resp = client.post("/sessions/sid/rename", json={"title": "custom"})

    assert resp.status_code == 200
    assert resp.json() == {"session_id": "sid", "manual_title": "custom"}
    assert renamed == {"sid": "sid", "title": "custom"}


def test_rename_unknown_session_returns_404(monkeypatch):
    monkeypatch.setattr(server.manager, "get", lambda sid: None)
    client = TestClient(server.app)

    resp = client.post("/sessions/no-such/rename", json={"title": "x"})

    assert resp.status_code == 404


def test_rename_defaults_missing_title_to_empty_string(monkeypatch):
    session = MagicMock()
    session.to_api.return_value = {"session_id": "sid", "manual_title": ""}
    monkeypatch.setattr(server.manager, "get", lambda sid: session)
    renamed: dict = {}
    monkeypatch.setattr(
        server.manager, "rename",
        lambda sid, title: renamed.update(sid=sid, title=title) or session,
    )
    client = TestClient(server.app)

    resp = client.post("/sessions/sid/rename", json={})

    assert resp.status_code == 200
    assert renamed == {"sid": "sid", "title": ""}
