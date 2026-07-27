"""``GET /healthz`` (app/session_host/server.py) — issue #615.

The session-host is excluded from ``tray.bat --restart``'s reclaim sweep
(project-scaffolding#35, to protect live PTYs), so it can keep running code
that's days old with nothing surfacing that. ``/healthz`` now reports the
build identity (``git_sha``/``started_at``) this process loaded at start —
the same mechanism the webapp's own ``/api/version`` already uses — so a
caller (``GET /api/version``'s own freshness check, or a human curling this
port directly) can tell whether the session-host is running current code.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.session_host import server


def test_healthz_reports_build_identity():
    client = TestClient(server.app)

    resp = client.get("/healthz")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["service"] == "session-host"
    assert isinstance(body["sessions"], int)
    assert body["git_sha"] == server._IDENTITY["git_sha"]
    assert body["started_at"] == server._IDENTITY["captured_at"]
    assert isinstance(body["git_sha"], str) and body["git_sha"]
    assert isinstance(body["started_at"], str) and body["started_at"]
