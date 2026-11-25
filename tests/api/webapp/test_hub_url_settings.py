"""Settings > Local hub: the hub URL is saved through /api/config only after the hub answers."""
from __future__ import annotations

import pytest

from src.clients import hub_health, hub_url
from src.webapp_config import load_webapp_config


def _hub_answers(monkeypatch, body=None, raises=None):
    """Stand in for the hub's /healthz; returns the URLs asked."""
    asked = []

    def fake(method, url, **kwargs):
        asked.append(url)
        if raises is not None:
            raise raises
        return {"ok": True, "service": "local-llm-hub"} if body is None else body

    monkeypatch.setattr(hub_url._loopback_http, "request", fake)
    return asked


def test_the_config_carries_the_hub_url_and_the_local_address(webapp_client):
    client, _, _ = webapp_client
    body = client.get("/api/config").json()
    assert body["llm_hub_url"] == "http://127.0.0.1:8000"
    assert body["llm_hub_local_url"] == "http://127.0.0.1:8000"


def test_setting_then_clearing_the_url_applies_without_a_restart(webapp_client, monkeypatch):
    client, app, _ = webapp_client
    asked = _hub_answers(monkeypatch)
    assert client.post("/api/config", json={"llm_hub_url": "http://192.168.1.20:8000/"}).status_code == 200
    assert asked == ["http://192.168.1.20:8000/healthz"]
    assert app.state.webapp_config.llm_hub_url == "http://192.168.1.20:8000"
    assert load_webapp_config().llm_hub_url == "http://192.168.1.20:8000"
    assert client.get("/api/config").json()["llm_hub_url"] == "http://192.168.1.20:8000"

    # Off needs no check: nothing is asked.
    assert client.post("/api/config", json={"llm_hub_url": ""}).status_code == 200
    assert asked == ["http://192.168.1.20:8000/healthz"]
    assert app.state.webapp_config.llm_hub_url == ""
    assert client.get("/api/status").json()["tts"] is False
    assert hub_health.read(app.state.webapp_config.llm_hub_url)["why"] == "disabled"


def test_an_unreachable_url_is_refused_with_its_reason_and_nothing_is_saved(webapp_client, monkeypatch):
    client, app, _ = webapp_client
    _hub_answers(monkeypatch, raises=hub_url.HubUrlError(
        "local-llm-hub unreachable at http://192.168.1.20:8000/healthz (connection refused)", status=503))
    resp = client.post("/api/config", json={"llm_hub_url": "http://192.168.1.20:8000"})
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Nothing answered at http://192.168.1.20:8000 within 3 s. Is the hub running?"
    assert app.state.webapp_config.llm_hub_url == "http://127.0.0.1:8000"
    assert load_webapp_config().llm_hub_url == "http://127.0.0.1:8000"


def test_something_that_is_not_a_hub_is_refused(webapp_client, monkeypatch):
    client, app, _ = webapp_client
    _hub_answers(monkeypatch, body={"ok": False})
    resp = client.post("/api/config", json={"llm_hub_url": "http://127.0.0.1:9999"})
    assert resp.status_code == 400
    assert "not as a local-llm-hub" in resp.json()["detail"]
    assert app.state.webapp_config.llm_hub_url == "http://127.0.0.1:8000"


@pytest.mark.parametrize("value", [
    "127.0.0.1:8000",                    # no scheme
    "ftp://127.0.0.1:8000",              # wrong scheme
    "http://example.com",                # a public host
    "http://8.8.8.8:8000",               # a public address
    "http://127.0.0.1:8000/v1",          # a path
    "http://127.0.0.1:8000/?x=1",        # a query
    "http://user:pw@127.0.0.1:8000",     # credentials
    "http://127.0.0.1:notaport",         # a bad port
    5,                                   # not text
])
def test_a_badly_shaped_url_is_refused_before_the_hub_is_asked(webapp_client, monkeypatch, value):
    client, app, _ = webapp_client
    asked = _hub_answers(monkeypatch)
    assert client.post("/api/config", json={"llm_hub_url": value}).status_code == 400
    assert asked == []
    assert app.state.webapp_config.llm_hub_url == "http://127.0.0.1:8000"


@pytest.mark.parametrize("value,expected", [
    ("  http://localhost:8000/ ", "http://localhost:8000"),
    ("https://192.168.0.5", "https://192.168.0.5"),
    ("http://100.64.1.2:8000", "http://100.64.1.2:8000"),
    ("", ""),
    ("   ", ""),
])
def test_normalize_accepts_loopback_and_lan_addresses(value, expected):
    assert hub_url.normalize(value) == expected
