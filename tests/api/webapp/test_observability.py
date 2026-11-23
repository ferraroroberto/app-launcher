"""Slow-request breadcrumb middleware (issue #386).

In-process via ``TestClient`` — no live tray, no file handler attached, so
assertions go through ``caplog`` on the ``launcher.slowreq`` logger.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.webapp.observability import request_timing_options
from src.request_timing import RequestStats, RequestTimingMiddleware, TimedExecutor

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

    app.add_middleware(RequestTimingMiddleware, **{**request_timing_options(), "slow_s": slow_s})
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
    mw = RequestTimingMiddleware(None, **{**request_timing_options(), "slow_s": 999.0, "inflight_warn": 2})

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
    mw = RequestTimingMiddleware(None, **request_timing_options())
    assert mw.slow_s == 7.5
    assert mw.inflight_warn == 12


# --- per-request timings and budgets ---------------------------------------


def _timed_client(**mw_kwargs) -> TestClient:
    pool = TimedExecutor("probe", max_workers=1)
    app = FastAPI()

    @app.get("/api/board")
    async def board():
        await asyncio.get_running_loop().run_in_executor(pool, lambda: None)
        await asyncio.sleep(0.06)
        return {"ok": True}

    @app.get("/api/quiet")
    async def quiet():
        return {"ok": True}

    options = {**request_timing_options(), "slow_s": 999.0, "stats": RequestStats(), **mw_kwargs}
    app.add_middleware(RequestTimingMiddleware, **options)
    return TestClient(app)


def _slow_lines(caplog):
    return [r.getMessage() for r in caplog.records if r.name == _SLOW_LOGGER]


def test_every_api_response_carries_queue_worker_and_total():
    client = _timed_client()
    header = client.get("/api/board").headers["server-timing"]
    assert "queue;dur=" in header and "worker;dur=" in header and 'desc="probe"' in header
    assert "app;dur=" in header
    quiet = client.get("/api/quiet").headers["server-timing"]
    assert 'desc="none"' in quiet  # no thread used, said plainly


def test_over_soft_limit_logs_over_budget_with_the_split(caplog):
    client = _timed_client(budget_overrides=lambda: {"board": [0.01, 5.0]})
    with caplog.at_level(logging.WARNING, logger=_SLOW_LOGGER):
        client.get("/api/board")
    lines = _slow_lines(caplog)
    assert any("over budget" in line and "(board)" in line and "pool=probe" in line for line in lines)
    assert not any("budget breach" in line for line in lines)


def test_over_hard_limit_logs_a_breach_with_the_split(caplog):
    client = _timed_client(budget_overrides=lambda: {"board": [0.01, 0.02]})
    with caplog.at_level(logging.WARNING, logger=_SLOW_LOGGER):
        client.get("/api/board")
    assert any(
        "budget breach" in line and "queue_ms=" in line and "worker_ms=" in line
        and "total_ms=" in line and "pool=probe" in line
        for line in _slow_lines(caplog)
    )


def test_a_request_inside_its_budget_stays_silent(caplog):
    client = _timed_client(budget_overrides=lambda: {"board": [30.0, 60.0]})
    with caplog.at_level(logging.WARNING, logger=_SLOW_LOGGER):
        client.get("/api/board")
    assert not _slow_lines(caplog)


def test_slow_line_carries_the_split(caplog):
    client = _make_client()
    with caplog.at_level(logging.WARNING, logger=_SLOW_LOGGER):
        client.get("/slow")
    assert any("total_ms=" in line for line in _slow_lines(caplog))


def test_request_stats_reports_the_table_and_the_cache_age_without_scanning(webapp_client):
    client, app, _ = webapp_client
    app.state.webapp_config.request_budgets = {"board": [2.0, 4.0]}
    res = client.get("/api/request-stats", params={"window_s": 60})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["budgets"]["board"] == {"soft_s": 2.0, "hard_s": 4.0}
    assert body["budgets"]["send"] == {"soft_s": 5.0, "hard_s": 10.0}
    assert body["window_s"] == 60
    assert body["max_inflight"] >= 1  # this very request
    assert body["git_status_age_s"] is None  # no scan has run, and asking started none
    assert getattr(app.state, "git_status_cache", None) in (None, {})
    assert client.get("/api/request-stats", params={"window_s": 0}).status_code == 422
