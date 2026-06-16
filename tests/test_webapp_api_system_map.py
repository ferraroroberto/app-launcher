"""Fleet system map endpoints (issue #173).

Covers:
  - GET /api/system-map/status — availability flips on the PNG's presence
    under ``claude_config_dir``; stays reachable off-tailnet (token-only).
  - GET /api/system-map/image — 200 + image/png when present, 404 when the
    PNG is absent (gate bypassed by treating TestClient as loopback).
  - the gate — the image endpoint is refused over the Cloudflare tunnel and
    off the tailnet, exactly like the live-terminal endpoints.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# A 1x1 PNG is overkill — the route just streams the file with our media
# type — so any bytes on disk at the expected path stand in for the render.
_MAP_BYTES = b"\x89PNG\r\n\x1a\n stub render"


def _make_map(tmp_path: Path) -> Path:
    """Create a stub ``architecture/system-map.png`` and return the root dir."""
    root = tmp_path / "fleet-config"
    arch = root / "architecture"
    arch.mkdir(parents=True)
    (arch / "system-map.png").write_bytes(_MAP_BYTES)
    return root


class TestSystemMapStatus:
    def test_available_when_png_present(self, webapp_client, tmp_path):
        client, app, _ = webapp_client
        app.state.webapp_config.claude_config_dir = str(_make_map(tmp_path))
        body = client.get("/api/system-map/status").json()
        assert body["available"] is True

    def test_unavailable_when_png_missing(self, webapp_client, tmp_path):
        client, app, _ = webapp_client
        # Dir exists but no architecture/system-map.png in it.
        app.state.webapp_config.claude_config_dir = str(tmp_path / "nope")
        body = client.get("/api/system-map/status").json()
        assert body["available"] is False

    def test_status_reachable_off_tailnet(self, webapp_client, tmp_path):
        """The status probe is token-only (not Tailscale-gated) so the SPA can
        decide the section's visibility even over the public tunnel — unlike
        the image endpoint, the default off-tailnet TestClient is NOT refused."""
        client, app, _ = webapp_client
        app.state.webapp_config.claude_config_dir = str(_make_map(tmp_path))
        resp = client.get("/api/system-map/status")
        assert resp.status_code == 200


class TestSystemMapImage:
    """Treat the TestClient host as loopback so the Tailscale gate is skipped
    and the route logic (present → 200, absent → 404) is exercised; the gate
    itself is covered by TestSystemMapGate below."""

    @pytest.fixture(autouse=True)
    def _bypass_gate(self, monkeypatch):
        from app.webapp import middleware
        monkeypatch.setattr(
            middleware,
            "LOOPBACK_HOSTS",
            frozenset({"testclient", "127.0.0.1", "::1", "localhost"}),
        )

    def test_returns_png_bytes(self, webapp_client, tmp_path):
        client, app, _ = webapp_client
        app.state.webapp_config.claude_config_dir = str(_make_map(tmp_path))
        resp = client.get("/api/system-map/image")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/png"
        assert resp.content == _MAP_BYTES

    def test_404_when_png_missing(self, webapp_client, tmp_path):
        client, app, _ = webapp_client
        app.state.webapp_config.claude_config_dir = str(tmp_path / "nope")
        resp = client.get("/api/system-map/image")
        assert resp.status_code == 404


class TestSystemMapGate:
    def test_image_refused_over_cloudflare(self, webapp_client, tmp_path):
        client, app, _ = webapp_client
        app.state.webapp_config.claude_config_dir = str(_make_map(tmp_path))
        resp = client.get(
            "/api/system-map/image", headers={"Cf-Ray": "abc-123"}
        )
        assert resp.status_code == 403
        assert "public tunnel" in resp.json()["detail"].lower()

    def test_image_refused_off_tailnet(self, webapp_client, tmp_path):
        # Default TestClient host 'testclient' is neither loopback nor in the
        # Tailscale range → the "tailnet" gate refuses it (even though the PNG
        # exists), so the map never leaves the tailnet.
        client, app, _ = webapp_client
        app.state.webapp_config.claude_config_dir = str(_make_map(tmp_path))
        resp = client.get("/api/system-map/image")
        assert resp.status_code == 403
