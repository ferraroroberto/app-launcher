"""Slow-request breadcrumb middleware (issue #386).

In-process via ``TestClient`` — no live tray, no file handler attached, so
assertions go through ``caplog`` on the ``launcher.slowreq`` logger.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.webapp.observability import SlowRequestLogMiddleware

_SLOW_LOGGER = "launcher.slowreq"


def _make_client(slow_s: float = 0.05) -> TestClient:
    app = FastAPI()

    @app.get("/fast")
    async def fast():
        return {"ok": True}

    @app.get("/slow")
    async def slow():
        await asyncio.sleep(slow_s * 3)
        return {"ok": True}

    app.add_middleware(SlowRequestLogMiddleware, slow_s=slow_s)
    return TestClient(app)


def test_slow_request_leaves_breadcrumb(caplog):
    client = _make_client()
    with caplog.at_level(logging.WARNING, logger=_SLOW_LOGGER):
        res = client.get("/slow")
    assert res.status_code == 200
    lines = [r.getMessage() for r in caplog.records if r.name == _SLOW_LOGGER]
    assert any("slow request" in line and "/slow" in line for line in lines)
    # The breadcrumb names method, status and elapsed — the fields that let
    # the next wedge be classified without a repro.
    assert any("GET" in line and "200" in line for line in lines)


def test_fast_request_stays_silent(caplog):
    client = _make_client()
    with caplog.at_level(logging.WARNING, logger=_SLOW_LOGGER):
        res = client.get("/fast")
    assert res.status_code == 200
    assert not [r for r in caplog.records if r.name == _SLOW_LOGGER]


def test_inflight_breadcrumb_thresholded_and_rate_limited(caplog):
    # Drive the in-flight logic directly: TestClient is serial, so real
    # concurrent requests can't populate the registry in a unit test.
    mw = SlowRequestLogMiddleware(app=None, slow_s=999.0, inflight_warn=2)

    with caplog.at_level(logging.WARNING, logger=_SLOW_LOGGER):
        mw._inflight = {1: ("GET", "/a", 90.0), 2: ("GET", "/b", 95.0)}
        mw._maybe_log_inflight(now=100.0)  # at threshold, not over — silent
        assert not caplog.records

        mw._inflight[3] = ("POST", "/c", 99.0)
        mw._maybe_log_inflight(now=100.0)  # over threshold — logs
        assert len(caplog.records) == 1
        msg = caplog.records[0].getMessage()
        assert "3 requests in flight" in msg
        assert "/a" in msg  # names the oldest

        mw._maybe_log_inflight(now=110.0)  # within the 30 s floor — silent
        assert len(caplog.records) == 1

        mw._maybe_log_inflight(now=140.0)  # floor elapsed — logs again
        assert len(caplog.records) == 2


def test_env_defaults_applied(monkeypatch):
    monkeypatch.setenv("LAUNCHER_SLOW_REQUEST_S", "7.5")
    monkeypatch.setenv("LAUNCHER_INFLIGHT_WARN", "12")
    mw = SlowRequestLogMiddleware(app=None)
    assert mw.slow_s == 7.5
    assert mw.inflight_warn == 12
