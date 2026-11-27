"""A config file that cannot be parsed must not switch remote protection off.

The loader answers it with defaults, and the default access token is empty,
so the webapp refuses everyone but the PC itself until the file is fixed.
Every test here works on a temp file, never the live config.
"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from src.webapp_config import load_webapp_config

BROKEN = b'{"auth_token": "synthetic-token",}'
LOOPBACK = ("127.0.0.1", 50000)
REMOTE = ("100.64.0.5", 50000)


def _build(path, content):
    """A fresh app started on ``content`` (``None``: no file at all)."""
    from app.webapp import server

    if content is None:
        path.unlink()
    else:
        path.write_bytes(content)
    return server.create_app()


@pytest.mark.parametrize(
    "content", [BROKEN, b'["synthetic-token"]', b"\xff\xfe\x00"],
    ids=["bad-json", "not-an-object", "not-utf8"],
)
def test_only_the_pc_itself_is_served(webapp_client, content):
    app = _build(webapp_client[2]["tmp_webapp_cfg_path"], content)
    remote = TestClient(app, client=REMOTE)
    tunnel = TestClient(app, client=LOOPBACK, headers={"cf-ray": "x"})

    for client in (remote, tunnel):
        for path in ("/api/config", "/api/status", "/", "/healthz"):
            assert client.get(path).status_code == 503, path
        assert client.post("/api/jobs/demo/hook").status_code == 503
    with pytest.raises(WebSocketDisconnect) as closed:
        with remote.websocket_connect("/api/jobs/demo/runs/demo/stream") as socket:
            socket.receive_json()
    assert closed.value.code == 4403

    status = TestClient(app, client=LOOPBACK).get("/api/status")
    assert status.status_code == 200
    assert "webapp_config.json could not be read" in status.json()["config_error"]


def test_the_log_names_the_file_and_the_error_once(webapp_client, caplog):
    path = webapp_client[2]["tmp_webapp_cfg_path"]
    with caplog.at_level(logging.WARNING):
        _build(path, BROKEN)
        load_webapp_config()
        load_webapp_config()

    startup = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert len(startup) == 1
    assert str(path) in startup[0] and "line 1 column" in startup[0]
    assert sum(r.name == "src.webapp_config" for r in caplog.records) == 1


def test_a_missing_file_starts_on_defaults(webapp_client):
    app = _build(webapp_client[2]["tmp_webapp_cfg_path"], None)

    response = TestClient(app, client=REMOTE).get("/api/status")

    assert response.status_code == 200
    assert response.json()["config_error"] is None


def test_an_unusable_retention_value_is_bannered_and_locks_nobody_out(webapp_client):
    app = _build(
        webapp_client[2]["tmp_webapp_cfg_path"],
        b'{"session_retention_days": "forever"}',
    )

    response = TestClient(app, client=REMOTE).get("/api/status")

    assert response.status_code == 200
    assert "session_retention_days must be" in response.json()["config_error"]
