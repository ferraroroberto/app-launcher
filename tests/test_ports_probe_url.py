"""The port-listeners probe carries each listener's Open URL (#1129).

Listener rows became action-rows whose tap opens the app, the same as a
running app's Open, so ``GET /api/ports/probe`` now builds that URL server
side: ``<scheme>://<tailnet_host>:<port>/``, with the scheme from a loopback
TLS probe. The probe can take up to a second a port and the Apps tab polls
the route, so each live (pid, port) is probed once. Without a
``tailnet_host`` there is no URL, and nothing is probed.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace

import httpx

from app.webapp.routers import misc
from src.diagnostics import PortOwner


def _owners():
    return [
        PortOwner(pid=11, port=8501, name="pythonw.exe"),
        PortOwner(pid=12, port=8443, name="python.exe"),
    ]


def _probe_calls(monkeypatch):
    calls = []

    def fake_scheme(port):
        calls.append(port)
        return "https" if port == 8443 else "http"

    monkeypatch.setattr(misc, "detect_local_scheme", fake_scheme)
    monkeypatch.setattr(misc, "list_app_listeners", _owners)
    monkeypatch.setattr(misc, "_LISTENER_SCHEMES", {})
    return calls


def test_listeners_carry_their_tailnet_url(webapp_client, monkeypatch):
    client, app, _ = webapp_client
    calls = _probe_calls(monkeypatch)
    app.state.app_config = replace(app.state.app_config, tailnet_host="box.example.ts.net")

    rows = client.get("/api/ports/probe").json()["listeners"]
    urls = {r["port"]: r["url"] for r in rows}
    assert urls == {
        8501: "http://box.example.ts.net:8501/",
        8443: "https://box.example.ts.net:8443/",
    }

    # A second poll reuses each live listener's scheme.
    client.get("/api/ports/probe")
    assert sorted(calls) == [8443, 8501], calls


def test_no_tailnet_host_means_no_url_and_no_probe(webapp_client, monkeypatch):
    client, app, _ = webapp_client
    calls = _probe_calls(monkeypatch)
    app.state.app_config = replace(app.state.app_config, tailnet_host="")

    rows = client.get("/api/ports/probe").json()["listeners"]
    assert [r["url"] for r in rows] == [None, None]
    assert calls == []


def test_probe_scan_leaves_the_event_loop_free(webapp_client, monkeypatch):
    """#1262: psutil's listener scan takes ~1 s and ran on the event loop, so
    every other request on the webapp waited for it, and the Apps poll calls
    this every 5 s. Off the loop, a cheap request answers mid-scan."""
    _, app, _ = webapp_client
    monkeypatch.setattr(misc, "_LISTENER_SCHEMES", {})
    app.state.app_config = replace(app.state.app_config, tailnet_host="")

    def slow_scan():
        time.sleep(1.0)
        return _owners()

    monkeypatch.setattr(misc, "list_app_listeners", slow_scan)

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            started = time.perf_counter()
            probe = asyncio.create_task(client.get("/api/ports/probe"))
            await asyncio.sleep(0.2)
            health = await client.get("/healthz")
            health_at = time.perf_counter() - started
            return health.status_code, health_at, (await probe).status_code

    health, health_at, probe_status = asyncio.run(run())
    assert health == 200 and probe_status == 200
    assert health_at < 0.7, (
        f"/healthz answered {health_at:.2f}s after the probe started, behind "
        "its 1 s scan: the scan is blocking the event loop"
    )
