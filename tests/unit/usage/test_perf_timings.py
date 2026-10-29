"""Real-device timings (#1924): the router's checks, the store's pruning, the
summary maths, the ``Server-Timing`` header and the browser half's leg maths
(``tests/js/perf_timing.test.mjs``, skipped without Node).

Every value is invented.
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.subprocess_flags import NO_WINDOW
from src.usage import perf_timings as perf

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_TOKEN = "invented-bearer"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}
_SID = "0123456789abcdef0123456789abcdef"


@pytest.fixture
def perf_db(tmp_path, monkeypatch):
    data_dir = tmp_path / "runtime-data"
    monkeypatch.setenv("APP_LAUNCHER_DATA_DIR", str(data_dir))
    return data_dir / "perf.sqlite3"


def _batch(*rows, **envelope):
    return {"device_class": "phone", "os_class": "ios", "display": "pwa", "asset": "abcd1234",
            "conn_type": None, "rows": list(rows), **envelope}


def _api_row(**legs):
    row = {"kind": "api", "status": 200, "path": f"/api/claude-code/sessions/{_SID}/input?x=1",
           "queue": 5, "connect": 0, "wait": 900, "server": 120, "download": 30, "total": 950}
    row.update(legs)
    return row


def _stored(db: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM timings ORDER BY id").fetchall()
    finally:
        conn.close()


class TestRouter:
    def test_needs_the_bearer_token_off_loopback(self, perf_db, webapp_client):
        client, app, _ = webapp_client
        app.state.webapp_config.auth_token = _TOKEN
        assert client.post("/api/perf/timings", json=_batch(_api_row())).status_code == 401
        assert client.get("/api/perf/summary").status_code == 401
        assert not perf_db.exists()

    def test_stores_route_templates_never_ids_or_queries(self, perf_db, webapp_client):
        client, _, _ = webapp_client
        load = {"kind": "load", "nav_type": "navigate", "cached": False, "dns": 3, "connect": 20,
                "tls": 40, "ttfb": 90, "download": 10, "dom_ready": 300, "boot": 1500,
                "last_path": "/api/jobs?quiet=1", "last_wait": 400, "last_server": 350, "last_download": 5}
        resp = client.post("/api/perf/timings", json=_batch(_api_row(), load))
        assert resp.json() == {"accepted": 2, "rejected": 0}
        api, stored_load = _stored(perf_db)
        assert api["path"] == "/api/claude-code/sessions/{sid}/input"
        assert _SID not in str(dict(api)) and "?" not in api["path"]
        assert api["build"] and api["device_class"] == "phone" and api["display"] == "pwa"
        assert stored_load["path"] == "/api/jobs" and stored_load["cached"] == 0
        assert stored_load["boot"] == 1500

    @pytest.mark.parametrize("bad", [
        {"kind": "secret"},
        _api_row(total=500),                           # under the slow threshold
        _api_row(path="/api/no/such/route/at/all"),    # no route serves it
        _api_row(path="/static/main.js"),
        _api_row(path="/api/perf/timings"),            # its own sends
        _api_row(wait=-1),
        _api_row(server=perf.MAX_LEG_MS + 1),
        _api_row(download="12"),
        _api_row(status=True),
    ])
    def test_drops_a_row_that_fails_a_check(self, perf_db, webapp_client, bad):
        client, _, _ = webapp_client
        resp = client.post("/api/perf/timings", json=_batch(bad))
        assert resp.json() == {"accepted": 0, "rejected": 1}
        assert not perf_db.exists() or _stored(perf_db) == []

    def test_rejects_a_bad_envelope(self, perf_db, webapp_client):
        client, _, _ = webapp_client
        assert client.post("/api/perf/timings", json=_batch(device_class="laptop")).status_code == 400
        too_many = _batch(*[_api_row()] * (perf.MAX_ROWS_PER_BATCH + 1))
        assert client.post("/api/perf/timings", json=too_many).status_code == 400

    def test_summary_reports_absent_then_recorded(self, perf_db, webapp_client):
        client, _, _ = webapp_client
        assert client.get("/api/perf/summary").json()["log_exists"] is False
        client.post("/api/perf/timings", json=_batch(_api_row()))
        report = client.get("/api/perf/summary?days=1").json()
        assert report["log_exists"] is True and report["rows"] == 1
        assert report["api"]["phone"]["legs"]["network"]["median"] == 780


def test_templater_names_included_routes_and_prefers_literals():
    """Routes added through ``include_router`` must be found whichever
    FastAPI version is installed (newer ones wrap them in a lazy router that a
    walk of ``app.routes`` cannot read), and ``/runs/latest`` must beat
    ``/runs/{run_id}``."""
    from types import SimpleNamespace

    from fastapi import APIRouter, FastAPI

    from app.webapp.routers import perf_timings as router_mod

    inner = APIRouter(prefix="/api/runs")
    inner.add_api_route("/{run_id}", lambda run_id: {}, methods=["GET"])
    inner.add_api_route("/latest", lambda: {}, methods=["GET"])
    outer = APIRouter()
    outer.include_router(inner)
    app = FastAPI()
    app.include_router(outer)

    template = router_mod._templater(SimpleNamespace(app=app))
    assert template("/api/runs/abc123") == "/api/runs/{run_id}"
    assert template("/api/runs/latest") == "/api/runs/latest"
    assert template("/api/runs/abc/extra") is None
    assert template("/api/nothing") is None


def test_server_timing_on_api_responses_only(webapp_client):
    client, _, _ = webapp_client
    header = client.get("/api/version").headers.get("server-timing", "")
    assert header.startswith("app;dur=")
    assert float(header.split("=", 1)[1]) >= 0
    assert "server-timing" not in client.get("/").headers


def test_prunes_by_age_and_row_cap(tmp_path):
    store = perf.PerfTimings(tmp_path / "perf.sqlite3", max_age_days=90, max_rows=3)
    row = perf.validate_row(_api_row(path="/api/jobs"), lambda p: p)
    env = {"device_class": "desktop", "os_class": "windows"}
    now = datetime(2026, 6, 1, tzinfo=UTC)
    store.record(env, [row], build="b", now=now - timedelta(days=91))
    store.record(env, [row], build="b", now=now)
    assert len(_stored(store.db_path)) == 1, "a row past 90 days survived"
    store.record(env, [row] * 4, build="b", now=now)
    assert len(_stored(store.db_path)) == 3


def test_summary_median_p95_network_and_unknown(tmp_path):
    store = perf.PerfTimings(tmp_path / "perf.sqlite3")
    now = datetime(2026, 6, 1, tzinfo=UTC)
    rows = [perf.validate_row(_api_row(path="/api/board", total=t, wait=t - 50, server=s), lambda p: p)
            for t, s in ((900, 100), (1000, 200), (1100, None), (5000, 4000))]
    store.record({"device_class": "phone", "os_class": "ios"}, rows, build="b", now=now)
    report = store.summary(7, now=now)
    legs = report["api"]["phone"]["legs"]
    assert legs["total"] == {"n": 4, "median": 1050.0, "p95": 5000.0}
    assert legs["server"]["n"] == 3, "an unknown server leg was counted"
    assert legs["network"] == {"n": 3, "median": 750.0, "p95": 950.0}
    assert report["slowest_paths"][0]["path"] == "/api/board"
    assert report["loads"] == {}
    assert perf.percentile([1.0], 0.95) == 1.0


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required")
def test_browser_leg_maths_under_node():
    result = subprocess.run(
        ["node", str(_REPO_ROOT / "tests" / "js" / "perf_timing.test.mjs")],
        cwd=_REPO_ROOT, capture_output=True, text=True, timeout=30, creationflags=NO_WINDOW,
    )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "OK" in result.stdout
