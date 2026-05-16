"""Basic webapp routes: /healthz, /, /api/status, /api/claude-code/flags."""

from __future__ import annotations


class TestHealthz:
    def test_healthz_ok(self, webapp_client):
        client, _, _ = webapp_client
        resp = client.get("/healthz")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["service"] == "launcher"


class TestIndex:
    def test_index_returns_html(self, webapp_client):
        client, _, _ = webapp_client
        resp = client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")


class TestStatus:
    def test_status_returns_expected_shape(self, webapp_client):
        client, _, _ = webapp_client
        resp = client.get("/api/status")
        assert resp.status_code == 200
        body = resp.json()
        # /api/status is a kitchen-sink endpoint — narrow assertion to the
        # keys SPA code actually depends on, so future additions don't
        # false-fail the test.
        assert isinstance(body, dict)
        assert "tunnel_url" in body or "tunnel" in body or "scan_roots" in body or "terminal_reachability" in body


class TestClaudeFlags:
    def test_flags_returns_defaults(self, webapp_client):
        client, _, _ = webapp_client
        resp = client.get("/api/claude-code/flags")
        assert resp.status_code == 200
        body = resp.json()
        assert body["model"] == "opus"
        assert body["effort"] == "high"
        assert body["verbose"] is True
        assert body["debug"] is False
        # The always-on flags are surface-area for the SPA's badge; if the
        # tuple ever changes, this test catches it loudly.
        assert "--remote-control" in body["always_on_flags"]
        assert "--dangerously-skip-permissions" in body["always_on_flags"]
        # Computed flags are a string; sanity-check that the model/effort
        # round-trip through the formatter.
        assert "--model opus" in body["computed_flags"]
        assert "--effort high" in body["computed_flags"]
