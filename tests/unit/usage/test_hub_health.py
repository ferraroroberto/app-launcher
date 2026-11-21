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


_PORTS_BODY = {
    "verdict": "act", "reason": "ports", "fix": None, "host_id": "example-host",
    "findings": [
        {"level": "act", "metric": "tcp_percent", "value": 84.5},
        {"level": "watch", "metric": "tcp_time_wait", "value": 5000},
        {"level": "act", "metric": "total_handles", "value": 5_000_000},
    ],
    "system": {"commit_percent": 68.5},
    "offenders": {"memory_mb": [{"name": "llama-server.exe", "pid": 7, "value": 24998.8}] * 4},
    "ports": {
        "stale": False,
        "tcp": {"percent": 84.5}, "udp": {"percent": 0.6},
        "top_holders": [{"name": f"p{i}.exe", "pid": i, "value": 10 - i, "tcp": 10 - i, "udp": 0} for i in range(7)],
        "last_event": {"ts": 1794766557.8, "event_id": 4231, "protocol": "tcp", "reason": "TCP ran out of ephemeral ports"},
    },
}


def test_the_machine_block_carries_the_worst_ports_percent_commit_holders_and_event():
    machine = hh.parse(_PORTS_BODY)["machine"]
    assert machine["host"] == "example-host"
    assert machine["ports_percent"] == 84.5  # tcp, the worse of tcp and udp
    assert machine["commit_percent"] == 68.5
    assert [h["name"] for h in machine["holders"]] == ["p0.exe", "p1.exe", "p2.exe", "p3.exe", "p4.exe"]
    assert len(machine["memory_holders"]) == 3
    assert machine["last_event"]["event_id"] == 4231
    # Only an act finding on a ports or commit metric is critical.
    assert machine["critical"] == [{"metric": "tcp_percent", "value": 84.5}]


def test_a_stale_port_block_leaves_ports_unmeasured_never_zero():
    body = dict(_PORTS_BODY, ports=dict(_PORTS_BODY["ports"], stale=True))
    assert hh.parse(body)["machine"]["ports_percent"] is None


def test_a_payload_with_no_machine_data_has_no_machine_block():
    assert "machine" not in hh.parse({"verdict": "ok", "reason": "fine"})


_DISK_BODY = {
    "verdict": "act", "reason": "disk", "fix": None, "host_id": "example-host",
    "findings": [{"level": "act", "metric": "disk_load", "value": 98.0}],
    "disks": [
        {"disk": "PhysicalDrive2", "drives": ["C:"], "state": "alert",
         "busy_percent": 98.0, "queue": 7.5, "read_ms": 120.4, "write_ms": 310.0},
        {"disk": "PhysicalDrive0", "drives": ["D:", "F:"], "state": "watch",
         "busy_percent": 82.0, "queue": 2.0, "read_ms": None, "write_ms": 40.0},
        {"disk": "PhysicalDrive1", "drives": ["E:"], "state": "ok",
         "busy_percent": 1.0, "queue": 0.0, "read_ms": 0.1, "write_ms": 0.3},
    ],
}


def test_a_disk_load_act_finding_is_critical_and_the_disks_are_passed_through():
    machine = hh.parse(_DISK_BODY)["machine"]
    assert machine["critical"] == [{"metric": "disk_load", "value": 98.0}]
    assert [(d["drives"], d["state"], d["busy_percent"]) for d in machine["disks"]] == [
        (["C:"], "alert", 98.0), (["D:", "F:"], "watch", 82.0), (["E:"], "ok", 1.0)]
    assert machine["disks"][0]["queue"] == 7.5 and machine["disks"][0]["write_ms"] == 310.0
    assert machine["disks"][1]["read_ms"] is None  # no latency sample is not 0 ms


def test_a_disk_load_watch_finding_is_not_critical_but_the_disk_still_shows():
    body = dict(_DISK_BODY, verdict="watch", findings=[{"level": "watch", "metric": "disk_load", "value": 82.0}])
    machine = hh.parse(body)["machine"]
    assert machine["critical"] == []
    assert machine["disks"][1]["state"] == "watch"


def test_a_hub_that_sends_no_disks_reads_as_before():
    body = {k: v for k, v in _DISK_BODY.items() if k != "disks"}
    machine = hh.parse(body)["machine"]
    assert machine["disks"] is None
    assert hh.parse(_PORTS_BODY)["machine"]["disks"] is None


@pytest.mark.parametrize("disks", ["busy", {"disk": "x"}, [], [None, 3, {}], [{"disk": "  ", "drives": []}]])
def test_a_malformed_disks_block_is_not_shown_never_healthy(disks):
    body = {"verdict": "ok", "reason": "fine", "system": {"commit_percent": 40.0}, "disks": disks}
    assert hh.parse(body)["machine"]["disks"] is None


def test_a_disk_with_a_missing_or_unrecognised_state_reads_unknown():
    disks = [{"disk": "d0", "drives": ["C:"], "busy_percent": 1.0},
             {"disk": "d1", "drives": ["D:"], "state": "fine", "busy_percent": True}]
    got = hh.parse({"verdict": "ok", "disks": disks})["machine"]["disks"]
    assert [d["state"] for d in got] == ["unknown", "unknown"]
    assert got[1]["busy_percent"] is None  # a bool is not a number


def test_the_disk_block_is_bounded():
    disks = [{"disk": f"d{i}", "drives": [f"{i}:"] * 9, "state": "ok", "busy_percent": 1.0} for i in range(20)]
    got = hh.parse({"verdict": "ok", "disks": disks})["machine"]["disks"]
    assert len(got) == 8 and all(len(d["drives"]) == 4 for d in got)
