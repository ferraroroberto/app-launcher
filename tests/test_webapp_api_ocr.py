"""/api/ocr — compose-bar screenshot OCR proxy (issue #171).

The webapp proxies a captured screenshot to the sibling photo-ocr's
single-shot ``POST /api/extract`` over loopback and returns the extracted
text. The photo-ocr client is mocked (see conftest ``overrides["photo_ocr"]``)
so these run with no live photo-ocr on :8444. Mirrors
``test_webapp_api_transcribe.py``.
"""

from __future__ import annotations

import pytest


class TestOcrGate:
    """The /api/ocr route carries the terminal's Tailscale-only + passkey
    gate, like /api/transcribe — the OCR text feeds straight into the
    terminal compose bar. The TestClient connects as host 'testclient'
    (not loopback, not tailnet), so it is refused."""

    def test_ocr_refused_off_tailnet(self, webapp_client):
        client, _, overrides = webapp_client
        resp = client.post(
            "/api/ocr",
            files={"files": ("shot.png", b"img", "image/png")},
        )
        assert resp.status_code == 403
        overrides["photo_ocr"].extract.assert_not_called()


class TestOcr:
    """Treat the TestClient host as loopback so the terminal gate is skipped
    and the proxy logic is exercised (gate covered by TestOcrGate)."""

    @pytest.fixture(autouse=True)
    def _bypass_gate(self, monkeypatch):
        from app.webapp import middleware
        monkeypatch.setattr(
            middleware,
            "LOOPBACK_HOSTS",
            frozenset({"testclient", "127.0.0.1", "::1", "localhost"}),
        )

    def test_proxies_image_and_returns_text(self, webapp_client):
        client, _, overrides = webapp_client
        overrides["photo_ocr"].extract.return_value = {
            "text": "invoice #42 total 19.99",
            "model": "gemini_flash",
            "session_id": "po-1",
        }
        resp = client.post(
            "/api/ocr",
            files={"files": ("screenshot.png", b"fake-img", "image/png")},
        )
        assert resp.status_code == 200
        assert resp.json()["text"] == "invoice #42 total 19.99"
        # base URL (sample default), [(name, bytes, ctype)], model, prompt.
        args = overrides["photo_ocr"].extract.call_args
        assert args.args[0] == "https://127.0.0.1:8444"
        assert args.args[1] == [("screenshot.png", b"fake-img", "image/png")]
        assert args.args[2] is None  # no ?model= — photo-ocr's default applies
        assert args.args[3] is None  # no ?prompt_id=

    def test_proxies_multiple_images(self, webapp_client):
        """The collation case: several shots forwarded as one list."""
        client, _, overrides = webapp_client
        overrides["photo_ocr"].extract.return_value = {"text": "merged doc"}
        resp = client.post(
            "/api/ocr",
            files=[
                ("files", ("a.png", b"1", "image/png")),
                ("files", ("b.png", b"2", "image/png")),
            ],
        )
        assert resp.status_code == 200
        blobs = overrides["photo_ocr"].extract.call_args.args[1]
        assert [b[0] for b in blobs] == ["a.png", "b.png"]
        assert [b[1] for b in blobs] == [b"1", b"2"]

    def test_model_and_prompt_query_forwarded(self, webapp_client):
        client, _, overrides = webapp_client
        overrides["photo_ocr"].extract.return_value = {"text": "x"}
        resp = client.post(
            "/api/ocr?model=gemini_pro&prompt_id=code-fenced",
            files={"files": ("s.png", b"x", "image/png")},
        )
        assert resp.status_code == 200
        args = overrides["photo_ocr"].extract.call_args
        assert args.args[2] == "gemini_pro"
        assert args.args[3] == "code-fenced"

    def test_empty_image_rejected(self, webapp_client):
        client, _, overrides = webapp_client
        resp = client.post(
            "/api/ocr",
            files={"files": ("s.png", b"", "image/png")},
        )
        assert resp.status_code == 400
        overrides["photo_ocr"].extract.assert_not_called()

    def test_disabled_when_url_unset(self, webapp_client):
        client, app, overrides = webapp_client
        app.state.webapp_config.photo_ocr_url = ""
        resp = client.post(
            "/api/ocr",
            files={"files": ("s.png", b"img", "image/png")},
        )
        assert resp.status_code == 503
        overrides["photo_ocr"].extract.assert_not_called()

    def test_upstream_error_maps_to_http_status(self, webapp_client):
        client, _, overrides = webapp_client
        photo = overrides["photo_ocr"]
        photo.extract.side_effect = photo.PhotoOcrError(
            "photo-ocr unreachable", status=503
        )
        resp = client.post(
            "/api/ocr",
            files={"files": ("s.png", b"img", "image/png")},
        )
        assert resp.status_code == 503
        assert "unreachable" in resp.json()["detail"]


class TestStatusOcrFlag:
    def test_status_reports_screenshot_ocr_enabled(self, webapp_client):
        client, _, _ = webapp_client
        resp = client.get("/api/status")
        assert resp.status_code == 200
        assert resp.json()["screenshot_ocr"] is True

    def test_status_reports_screenshot_ocr_disabled(self, webapp_client):
        client, app, _ = webapp_client
        app.state.webapp_config.photo_ocr_url = ""
        resp = client.get("/api/status")
        assert resp.status_code == 200
        assert resp.json()["screenshot_ocr"] is False
