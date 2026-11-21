"""The private-network override of the Tailscale-only terminal routes.

Two switches, both off by default: ``terminal_allow_private_lan`` admits a
private address on the terminal-grade routes, ``screen_allow_private_lan``
additionally admits it on the screen view. The token and passkey legs still
apply after either, and the public tunnel is never admitted.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src.middleware import trust
from src.middleware.terminal_gate import client_allowed, ws_terminal_gate
from src.webapp_config import WebappConfig, load_webapp_config

LAN = "192.168.1.20"
TAILNET = "100.64.0.5"
PUBLIC = "8.8.8.8"
TRANSCRIPT = "/api/claude-code/sessions/abc/transcript"
SCREEN = "/api/screen/monitors"
REFUSAL = "terminal endpoints are Tailscale-only"


def _cfg(terminal: bool = False, screen: bool = False) -> WebappConfig:
    return WebappConfig(
        terminal_allow_private_lan=terminal, screen_allow_private_lan=screen
    )


def _from(app, host: str) -> TestClient:
    return TestClient(app, client=(host, 50000))


class TestTrust:
    @pytest.mark.parametrize(
        "host",
        ["10.1.2.3", "172.16.0.1", "172.31.255.254", "192.168.0.9", "169.254.1.1", "fd12::1", "fe80::1"],
    )
    def test_private_addresses(self, host):
        assert trust.client_in_private_lan(host) is True

    @pytest.mark.parametrize(
        "host", ["8.8.8.8", "172.32.0.1", "100.64.0.1", "2001:db8::1", "testclient", ""]
    )
    def test_everything_else_is_not_private(self, host):
        assert trust.client_in_private_lan(host) is False


class TestClientAllowed:
    def test_defaults_are_off(self):
        cfg = WebappConfig()
        assert cfg.terminal_allow_private_lan is False
        assert cfg.screen_allow_private_lan is False

    def test_off_refuses_private_but_keeps_tailnet(self):
        assert client_allowed(LAN, _cfg()) is False
        assert client_allowed(TAILNET, _cfg()) is True

    def test_terminal_on_admits_private_not_public(self):
        assert client_allowed(LAN, _cfg(terminal=True)) is True
        assert client_allowed(PUBLIC, _cfg(terminal=True)) is False

    def test_screen_needs_both_switches(self):
        assert client_allowed(LAN, _cfg(terminal=True), screen=True) is False
        assert client_allowed(LAN, _cfg(terminal=True, screen=True), screen=True) is True
        # The screen switch alone never opens anything.
        assert client_allowed(LAN, _cfg(screen=True), screen=True) is False
        assert client_allowed(LAN, _cfg(screen=True)) is False

    def test_tailnet_screen_is_unaffected(self):
        assert client_allowed(TAILNET, _cfg(), screen=True) is True


class TestHttpGate:
    def test_off_refuses_a_private_client(self, webapp_client):
        _, app, _ = webapp_client
        for path in (TRANSCRIPT, SCREEN):
            resp = _from(app, LAN).get(path)
            assert resp.status_code == 403, path
            assert resp.json()["detail"] == REFUSAL

    def test_terminal_on_admits_the_terminal_routes_only(self, webapp_client):
        _, app, _ = webapp_client
        app.state.webapp_config.terminal_allow_private_lan = True
        lan = _from(app, LAN)
        assert lan.get(TRANSCRIPT).status_code != 403
        refused = lan.get(SCREEN)
        assert refused.status_code == 403
        assert refused.json()["detail"] == REFUSAL

    def test_both_on_admits_the_screen_route(self, webapp_client):
        _, app, _ = webapp_client
        app.state.webapp_config.terminal_allow_private_lan = True
        app.state.webapp_config.screen_allow_private_lan = True
        assert _from(app, LAN).get(SCREEN).status_code != 403

    def test_the_bearer_token_still_applies(self, webapp_client):
        _, app, _ = webapp_client
        app.state.webapp_config.terminal_allow_private_lan = True
        app.state.webapp_config.auth_token = "secret"
        lan = _from(app, LAN)
        assert lan.get(TRANSCRIPT).status_code == 401
        assert lan.get(TRANSCRIPT, headers={"Authorization": "Bearer secret"}).status_code != 401

    def test_the_public_tunnel_is_never_admitted(self, webapp_client):
        _, app, _ = webapp_client
        app.state.webapp_config.terminal_allow_private_lan = True
        app.state.webapp_config.screen_allow_private_lan = True
        resp = _from(app, LAN).get(TRANSCRIPT, headers={"cf-ray": "x"})
        assert resp.status_code == 403
        assert "public tunnel" in resp.json()["detail"]

    def test_status_reports_the_lan_reachability(self, webapp_client):
        _, app, _ = webapp_client
        assert _from(app, LAN).get("/api/status").json()["terminal"]["reachable"] is False
        app.state.webapp_config.terminal_allow_private_lan = True
        assert _from(app, LAN).get("/api/status").json()["terminal"] == {
            "reachable": True, "reason": "private-lan",
        }


def _ws(app, host: str, query=None) -> SimpleNamespace:
    return SimpleNamespace(
        app=app,
        client=SimpleNamespace(host=host),
        headers={},
        query_params=query or {},
        url=SimpleNamespace(path="/ws"),
    )


class TestWebSocketGate:
    def test_off_refuses_a_private_client_with_the_existing_message(self, webapp_client):
        _, app, _ = webapp_client
        assert ws_terminal_gate(_ws(app, LAN), label="terminal") == (4403, "terminal is Tailscale-only")
        assert ws_terminal_gate(_ws(app, LAN), label="screen") == (4403, "screen is Tailscale-only")

    def test_main_on_screen_off(self, webapp_client):
        _, app, _ = webapp_client
        app.state.webapp_config.terminal_allow_private_lan = True
        assert ws_terminal_gate(_ws(app, LAN), label="terminal") is None
        assert ws_terminal_gate(_ws(app, LAN), label="screen") == (4403, "screen is Tailscale-only")

    def test_both_on(self, webapp_client):
        _, app, _ = webapp_client
        app.state.webapp_config.terminal_allow_private_lan = True
        app.state.webapp_config.screen_allow_private_lan = True
        assert ws_terminal_gate(_ws(app, LAN), label="screen") is None

    def test_the_token_still_applies(self, webapp_client):
        _, app, _ = webapp_client
        cfg = app.state.webapp_config
        cfg.terminal_allow_private_lan = True
        cfg.auth_token = "secret"
        assert ws_terminal_gate(_ws(app, LAN), label="terminal") == (
            4401, "missing or invalid bearer token",
        )
        assert ws_terminal_gate(_ws(app, LAN, {"token": "secret"}), label="terminal") is None


class TestConfigSwitches:
    def test_default_off_and_round_trips(self, webapp_client):
        client, app, overrides = webapp_client
        body = client.get("/api/config").json()
        assert body["terminal_allow_private_lan"] is False
        assert body["screen_allow_private_lan"] is False

        local = _from(app, "127.0.0.1")
        assert local.post("/api/config", json={"terminal_allow_private_lan": True}).status_code == 200
        assert local.post("/api/config", json={"screen_allow_private_lan": True}).status_code == 200
        saved = load_webapp_config(overrides["tmp_webapp_cfg_path"])
        assert saved.terminal_allow_private_lan is True
        assert saved.screen_allow_private_lan is True

    @pytest.mark.parametrize("key", ["terminal_allow_private_lan", "screen_allow_private_lan"])
    @pytest.mark.parametrize("junk", ["false", 1, None])
    def test_rejects_non_booleans(self, webapp_client, key, junk):
        _, app, _ = webapp_client
        resp = _from(app, "127.0.0.1").post("/api/config", json={key: junk})
        assert resp.status_code == 400
        assert getattr(app.state.webapp_config, key) is False

    def test_screen_cannot_be_on_while_the_main_switch_is_off(self, webapp_client):
        _, app, _ = webapp_client
        resp = _from(app, "127.0.0.1").post("/api/config", json={"screen_allow_private_lan": True})
        assert resp.status_code == 400
        assert app.state.webapp_config.screen_allow_private_lan is False

    def test_main_off_takes_the_screen_switch_with_it(self, webapp_client):
        _, app, _ = webapp_client
        local = _from(app, "127.0.0.1")
        local.post("/api/config", json={"terminal_allow_private_lan": True})
        local.post("/api/config", json={"screen_allow_private_lan": True})
        assert local.post("/api/config", json={"terminal_allow_private_lan": False}).status_code == 200
        assert app.state.webapp_config.screen_allow_private_lan is False

    def test_a_bearer_token_alone_cannot_turn_it_on(self, webapp_client):
        """From a public or unlisted address the switch stays off, token or not."""
        _, app, _ = webapp_client
        app.state.webapp_config.auth_token = "secret"
        auth = {"Authorization": "Bearer secret"}
        for host in (PUBLIC, LAN):
            resp = _from(app, host).post(
                "/api/config", json={"terminal_allow_private_lan": True}, headers=auth
            )
            assert resp.status_code == 403, host
        tunnel = _from(app, "127.0.0.1").post(
            "/api/config", json={"terminal_allow_private_lan": True},
            headers={**auth, "cf-ray": "x"},
        )
        assert tunnel.status_code == 403
        assert app.state.webapp_config.terminal_allow_private_lan is False

    def test_a_tailnet_client_can_turn_it_on_and_anyone_can_turn_it_off(self, webapp_client):
        _, app, _ = webapp_client
        assert _from(app, TAILNET).post(
            "/api/config", json={"terminal_allow_private_lan": True}
        ).status_code == 200
        assert _from(app, PUBLIC).post(
            "/api/config", json={"terminal_allow_private_lan": False}
        ).status_code == 200
        assert app.state.webapp_config.terminal_allow_private_lan is False
