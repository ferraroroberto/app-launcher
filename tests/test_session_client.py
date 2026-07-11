"""The loopback session-host client (`src.session_client`).

Focused on the request *shape* — these are thin `requests` wrappers, so we
stub `requests.request` and assert the JSON body the session-host receives.
"""

from __future__ import annotations

from src import session_client


class _Resp:
    status_code = 200

    def json(self):
        return {"session_id": "s1"}


def test_create_session_includes_phone_dimensions(monkeypatch):
    """The phone's terminal size rides the /sessions create body so the PTY
    spawns at the right width for a ratatui TUI (issue #126)."""
    captured: dict = {}

    def fake_request(method, url, timeout=None, **kwargs):
        captured["method"] = method
        captured["json"] = kwargs.get("json")
        return _Resp()

    monkeypatch.setattr(session_client.requests, "request", fake_request)

    session_client.create_session(
        8446, r"C:\proj", "name", "", agent="codex", rows=50, cols=42
    )

    body = captured["json"]
    assert captured["method"] == "POST"
    assert body["rows"] == 50
    assert body["cols"] == 42
    assert body["agent"] == "codex"


def test_create_session_defaults_dimensions(monkeypatch):
    """Omitted dimensions fall back to the legacy 40×120 default."""
    captured: dict = {}

    def fake_request(method, url, timeout=None, **kwargs):
        captured["json"] = kwargs.get("json")
        return _Resp()

    monkeypatch.setattr(session_client.requests, "request", fake_request)

    session_client.create_session(8446, r"C:\proj", "name", "")

    assert captured["json"]["rows"] == 40
    assert captured["json"]["cols"] == 120


def test_create_session_includes_history_lines_when_given(monkeypatch):
    """The Settings-tab-configurable scrollback depth (issue #435
    follow-up) rides the /sessions create body when the caller passes one."""
    captured: dict = {}

    def fake_request(method, url, timeout=None, **kwargs):
        captured["json"] = kwargs.get("json")
        return _Resp()

    monkeypatch.setattr(session_client.requests, "request", fake_request)

    session_client.create_session(
        8446, r"C:\proj", "name", "", agent="codex", history_lines=5000,
    )

    assert captured["json"]["history_lines"] == 5000


def test_create_session_omits_history_lines_when_not_given(monkeypatch):
    """No override → the key is absent entirely, so the session-host falls
    back to its own default rather than receiving a spurious null/zero."""
    captured: dict = {}

    def fake_request(method, url, timeout=None, **kwargs):
        captured["json"] = kwargs.get("json")
        return _Resp()

    monkeypatch.setattr(session_client.requests, "request", fake_request)

    session_client.create_session(8446, r"C:\proj", "name", "")

    assert "history_lines" not in captured["json"]
