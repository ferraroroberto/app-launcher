"""Remote Desktop detection behind the missing-mic hint.

The window-station name is the whole signal: ``RDP-Tcp#n`` is remote,
``Console`` is local, and anything else, or a name Windows would not give, is
``unknown`` and never folded into either. The route adds whether the caller
is a browser on this PC, because a phone's mic is unaffected by the PC's
session. Names here are invented.
"""

from __future__ import annotations

import pytest

from app.webapp.routers import misc
from src.desktop import session_kind as sk


@pytest.mark.parametrize(
    "name, state",
    [
        ("RDP-Tcp#0", "remote"),
        ("rdp-tcp#12", "remote"),
        ("Console", "local"),
        (" console ", "local"),
        ("", "unknown"),
        ("Services", "unknown"),
        ("RDP-Tcp", "unknown"),
    ],
)
def test_a_station_name_maps_to_its_state(name, state):
    assert sk.classify(name) == state


def test_a_name_windows_will_not_give_is_unknown_not_local(monkeypatch):
    monkeypatch.setattr(sk.sys, "platform", "win32")

    def refuse():
        raise OSError("no session information")

    monkeypatch.setattr(sk, "_station_name", refuse)
    assert sk.read() == "unknown"


def test_a_non_windows_host_is_unknown(monkeypatch):
    monkeypatch.setattr(sk.sys, "platform", "linux")
    assert sk.read() == "unknown"


def test_the_route_reports_the_state_and_whether_the_caller_is_this_pc(webapp_client, monkeypatch):
    client, _app, _ = webapp_client
    monkeypatch.setattr(sk, "read", lambda: "remote")
    monkeypatch.setattr(misc, "_is_this_pc", lambda request: True)
    assert client.get("/api/desktop-session").json() == {"state": "remote", "this_pc": True}

    monkeypatch.setattr(misc, "_is_this_pc", lambda request: False)
    assert client.get("/api/desktop-session").json() == {"state": "remote", "this_pc": False}


class _Req:
    def __init__(self, host, headers=None):
        self.client = type("C", (), {"host": host})()
        self.headers = headers or {}


def test_loopback_and_the_machines_own_address_are_this_pc(monkeypatch):
    addr = type("A", (), {"address": "100.64.0.9"})()
    monkeypatch.setattr(misc.psutil, "net_if_addrs", lambda: {"Tailscale": [addr]})
    assert misc._is_this_pc(_Req("127.0.0.1"))
    assert misc._is_this_pc(_Req("100.64.0.9"))
    assert not misc._is_this_pc(_Req("100.64.0.77"))


def test_a_request_through_the_public_tunnel_is_never_this_pc(monkeypatch):
    addr = type("A", (), {"address": "100.64.0.9"})()
    monkeypatch.setattr(misc.psutil, "net_if_addrs", lambda: {"Tailscale": [addr]})
    assert not misc._is_this_pc(_Req("100.64.0.9", {"cf-connecting-ip": "203.0.113.5"}))
