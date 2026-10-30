"""The hub's PC health verdict behind the Code header's chip.

The hub judges; the launcher only reads. What matters here is that nothing the
launcher cannot read ever comes out as ``ok``, and that a good answer is cached
so the page's poll does not reach the hub on every tick.
"""
from __future__ import annotations

import logging

import pytest

from src.clients import hub_health as hh


@pytest.fixture(autouse=True)
def _fresh():
    hh.reset()
    yield
    hh.reset()


def _serve(monkeypatch, body=None, raises=None):
    calls = []

    def fake(method, url, **kwargs):
        calls.append(url)
        if raises is not None:
            raise raises
        return body

    monkeypatch.setattr(hh._loopback_http, "request", fake)
    return calls


@pytest.mark.parametrize("verdict", ["ok", "watch", "act"])
def test_a_recognised_verdict_passes_through_with_its_reason_and_fix(monkeypatch, verdict):
    _serve(monkeypatch, {"verdict": verdict, "reason": "Camera Hub  has 4M handles", "fix": "Restart Camera Hub"})
    got = hh.read("http://127.0.0.1:8000")
    assert got == {"verdict": verdict, "why": None, "reason": "Camera Hub has 4M handles", "fix": "Restart Camera Hub"}


def test_it_reads_the_hubs_machines_health_route(monkeypatch):
    calls = _serve(monkeypatch, {"verdict": "ok"})
    hh.read("http://127.0.0.1:8000/")
    assert calls == ["http://127.0.0.1:8000/admin/api/machines/health"]


@pytest.mark.parametrize(
    "body, why",
    [
        ({"verdict": "unknown", "reason": "no sample yet"}, "hub"),
        ({"verdict": "fine"}, "unreadable"),
        ({"reason": "no verdict key"}, "unreadable"),
        (["ok"], "unreadable"),
        (None, "unreadable"),
    ],
)
def test_an_answer_that_is_not_a_verdict_is_unknown_never_ok(monkeypatch, body, why):
    _serve(monkeypatch, body)
    got = hh.read("http://127.0.0.1:8000")
    assert got["verdict"] == "unknown"
    assert got["why"] == why


def test_a_hub_that_does_not_answer_is_unknown(monkeypatch):
    _serve(monkeypatch, raises=hh.HubHealthError("local-llm-hub unreachable", status=503))
    got = hh.read("http://127.0.0.1:8000")
    assert (got["verdict"], got["why"]) == ("unknown", "unreachable")


def test_no_hub_url_is_unknown_and_never_asks(monkeypatch):
    calls = _serve(monkeypatch, {"verdict": "ok"})
    got = hh.read("  ")
    assert (got["verdict"], got["why"]) == ("unknown", "disabled")
    assert calls == []


def test_a_good_answer_is_cached_and_a_failed_one_for_less_time(monkeypatch):
    calls = _serve(monkeypatch, {"verdict": "act", "reason": "r"})
    hh.read("http://h", now=100.0)
    hh.read("http://h", now=100.0 + hh.OK_CACHE_SECONDS - 1)
    assert len(calls) == 1
    hh.read("http://h", now=100.0 + hh.OK_CACHE_SECONDS + 1)
    assert len(calls) == 2

    hh.reset()
    calls = _serve(monkeypatch, raises=hh.HubHealthError("down", status=503))
    hh.read("http://h", now=100.0)
    hh.read("http://h", now=100.0 + hh.FAILED_CACHE_SECONDS - 1)
    assert len(calls) == 1
    hh.read("http://h", now=100.0 + hh.FAILED_CACHE_SECONDS + 1)
    assert len(calls) == 2


def test_going_unknown_and_coming_back_each_leave_one_breadcrumb(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="launcher.slowreq")
    _serve(monkeypatch, raises=hh.HubHealthError("down", status=503))
    hh.read("http://h", now=0.0)
    hh.read("http://h", now=100.0)  # still down: no second line
    _serve(monkeypatch, {"verdict": "ok"})
    hh.read("http://h", now=200.0)
    lines = [r.getMessage() for r in caplog.records if r.name == "launcher.slowreq"]
    assert len(lines) == 2
    assert "hub health unknown (unreachable)" in lines[0]
    assert "readable again: ok" in lines[1]


def test_the_route_serves_the_verdict_and_reads_the_configured_hub(webapp_client, monkeypatch):
    client, app, _ = webapp_client
    app.state.webapp_config.llm_hub_url = "http://hub.test:8000"
    calls = _serve(monkeypatch, {"verdict": "watch", "reason": "r", "fix": "f"})
    body = client.get("/api/system/health").json()
    assert body["verdict"] == "watch"
    assert calls == ["http://hub.test:8000/admin/api/machines/health"]


def test_the_route_says_unknown_when_the_hub_is_unset(webapp_client):
    client, app, _ = webapp_client
    app.state.webapp_config.llm_hub_url = ""
    body = client.get("/api/system/health").json()
    assert (body["verdict"], body["why"]) == ("unknown", "disabled")
