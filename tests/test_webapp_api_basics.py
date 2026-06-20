"""Basic webapp routes: /healthz, /, /api/status, /api/claude-code/flags."""

from __future__ import annotations

import re


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

    def test_index_is_no_cache(self, webapp_client):
        # Index must always be revalidated — without this, the PWA can
        # hold a stale shell that references a JS bundle that no longer
        # exists. Cache hygiene contract for issue #30.
        client, _, _ = webapp_client
        resp = client.get("/")
        assert "no-cache" in resp.headers.get("cache-control", "")

    def test_index_stamps_asset_urls(self, webapp_client):
        # Every /static/<name>.(css|js) referenced from the index must
        # carry an ?v=<8-hex-chars> stamp so iOS can't cache across an
        # asset edit.
        client, _, _ = webapp_client
        resp = client.get("/")
        body = resp.text
        assert "/static/styles.css?v=" in body
        assert "/static/main.js?v=" in body
        # No literal ?v=18 left over from the manual era. Match the complete
        # legacy stamp (the closing quote) — a substring "?v=18" also matches
        # valid 8-hex fleet hashes that happen to begin "18…" (the 8-hex
        # format itself is enforced by the loop below).
        assert '?v=18"' not in body
        # Stamps are 8 hex chars.
        stamps = re.findall(r"/static/[\w\-.]+\.(?:css|js)\?v=([a-f0-9]+)", body)
        assert stamps, "expected at least one stamped asset URL"
        for stamp in stamps:
            assert re.fullmatch(r"[a-f0-9]{8}", stamp), stamp


class TestStaticCaching:
    def test_js_served_immutable_year(self, webapp_client):
        # Hashed assets get year-long immutable cache — safe because the
        # URL changes on edit.
        client, _, _ = webapp_client
        resp = client.get("/static/main.js")
        assert resp.status_code == 200
        cache_control = resp.headers.get("cache-control", "")
        assert "max-age=31536000" in cache_control
        assert "immutable" in cache_control

    def test_js_imports_get_stamped(self, webapp_client):
        # JS files have their own ES-module imports rewritten at serve
        # time, so editing state.js invalidates everything that imports
        # it transitively (via the shared fleet hash).
        client, _, _ = webapp_client
        resp = client.get("/static/main.js")
        body = resp.text
        # main.js imports ./state.js, ./api.js, etc — all should be stamped.
        assert re.search(r"from\s+['\"]\./state\.js\?v=[a-f0-9]{8}['\"]", body)
        assert re.search(r"from\s+['\"]\./api\.js\?v=[a-f0-9]{8}['\"]", body)


class TestVersion:
    def test_version_shape(self, webapp_client):
        client, _, _ = webapp_client
        resp = client.get("/api/version")
        assert resp.status_code == 200
        body = resp.json()
        assert set(body.keys()) == {"git_sha", "built_at", "asset_hash"}
        assert isinstance(body["git_sha"], str) and body["git_sha"]
        assert isinstance(body["built_at"], str) and body["built_at"]
        assert isinstance(body["asset_hash"], str)


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


class TestAgents:
    def test_agents_shape(self, webapp_client):
        client, _, _ = webapp_client
        resp = client.get("/api/agents")
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body["agents"], list) and body["agents"]
        ids = {a["id"] for a in body["agents"]}
        assert {"claude", "antigravity", "copilot"} <= ids
        for a in body["agents"]:
            assert set(a) == {"id", "label", "available", "fullscreen"}
            assert isinstance(a["available"], bool)
            assert isinstance(a["fullscreen"], bool)


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
        assert body["permission_mode"] == "auto"
        # The always-on flags are surface-area for the SPA's badge; if the
        # tuple ever changes, this test catches it loudly.
        assert "--remote-control" in body["always_on_flags"]
        # The permission flag is user-selectable now — not always-on.
        assert "--dangerously-skip-permissions" not in body["always_on_flags"]
        # Computed flags are a string; sanity-check that the model/effort
        # and the default (auto) permission mode round-trip through the formatter.
        assert "--model opus" in body["computed_flags"]
        assert "--effort high" in body["computed_flags"]
        assert "--permission-mode auto" in body["computed_flags"]
