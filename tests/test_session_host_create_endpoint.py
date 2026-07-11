"""``POST /sessions`` (app/session_host/server.py) — request-body wiring.

Focused on ``history_lines`` (issue #435 follow-up, Settings-tab
configurable scrollback depth): the endpoint must forward it to
``SessionManager.create`` when present, and pass ``None`` (SessionManager's
own default) when the caller omits it — never a spurious 0/null override.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from app.session_host import server


def _stub_manager(monkeypatch):
    captured: dict = {}

    def fake_create(project_dir, name, flags, agent, rows=40, cols=120,
                     history_lines=None):
        captured.update(
            project_dir=project_dir, name=name, flags=flags, agent=agent,
            rows=rows, cols=cols, history_lines=history_lines,
        )
        session = MagicMock()
        session.to_api.return_value = {"session_id": "sid", "kind": "pty"}
        return session

    monkeypatch.setattr(server.manager, "create", fake_create)
    return captured


def test_create_forwards_history_lines_when_given(monkeypatch):
    captured = _stub_manager(monkeypatch)
    client = TestClient(server.app)

    resp = client.post(
        "/sessions",
        json={
            "project_dir": r"C:\proj", "name": "proj", "flags": "",
            "agent": "codex", "rows": 40, "cols": 120, "history_lines": 5000,
        },
    )

    assert resp.status_code == 200
    assert captured["history_lines"] == 5000


def test_create_defaults_history_lines_to_none_when_omitted(monkeypatch):
    captured = _stub_manager(monkeypatch)
    client = TestClient(server.app)

    resp = client.post(
        "/sessions",
        json={
            "project_dir": r"C:\proj", "name": "proj", "flags": "",
            "agent": "codex", "rows": 40, "cols": 120,
        },
    )

    assert resp.status_code == 200
    assert captured["history_lines"] is None
