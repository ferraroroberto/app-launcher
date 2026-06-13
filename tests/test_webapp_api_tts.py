"""/api/tts/* — read-aloud hub TTS proxy (issue #203).

The webapp proxies the agent's last reply to the local-llm-hub's OpenAI-shape
``POST /v1/audio/speech`` over loopback and streams the WAV back. ``/api/tts/health``
is a cheap up/down probe the SPA gates the 🔊 button's hub path on. The tts
client is mocked (see conftest ``overrides["tts"]``); the streaming POST mocks
``httpx`` directly, mirroring ``test_webapp_api_transcribe.py``'s SSE proxy.
"""

from __future__ import annotations

import pytest


class TestTtsGate:
    """``/api/tts/speak`` carries the terminal's Tailscale-only + passkey gate
    (the synthesized text is the agent's reply — terminal content). The
    TestClient connects as host 'testclient' (not loopback, not tailnet), so it
    is refused. ``/api/tts/health`` is innocuous and stays ungated."""

    def test_speak_refused_off_tailnet(self, webapp_client):
        client, _, _ = webapp_client
        resp = client.post("/api/tts/speak", json={"text": "hello"})
        assert resp.status_code == 403

    def test_health_allowed_off_tailnet(self, webapp_client):
        client, _, _ = webapp_client
        resp = client.get("/api/tts/health")
        assert resp.status_code == 200
        assert resp.json()["available"] is True


class TestTtsHealth:
    def test_available_when_hub_ok(self, webapp_client):
        client, _, overrides = webapp_client
        overrides["tts"].health.return_value = True
        resp = client.get("/api/tts/health")
        assert resp.json() == {"available": True}
        assert overrides["tts"].health.call_args.args[0] == "http://127.0.0.1:8000"

    def test_unavailable_when_hub_down(self, webapp_client):
        client, _, overrides = webapp_client
        tts = overrides["tts"]
        tts.health.side_effect = tts.TtsError("hub unreachable", status=503)
        resp = client.get("/api/tts/health")
        assert resp.status_code == 200
        assert resp.json() == {"available": False}

    def test_unavailable_when_url_unset(self, webapp_client):
        client, app, overrides = webapp_client
        app.state.webapp_config.llm_hub_url = ""
        resp = client.get("/api/tts/health")
        assert resp.json() == {"available": False}
        overrides["tts"].health.assert_not_called()


class TestTtsSpeak:
    """Treat the TestClient host as loopback so the terminal gate is skipped
    and the proxy logic is exercised (gate covered by TestTtsGate)."""

    @pytest.fixture(autouse=True)
    def _bypass_gate(self, monkeypatch):
        from app.webapp import middleware
        monkeypatch.setattr(
            middleware,
            "LOOPBACK_HOSTS",
            frozenset({"testclient", "127.0.0.1", "::1", "localhost"}),
        )

    def _mock_httpx(self, monkeypatch, *, status_code=200, wav=b"RIFFwave"):
        """Install a fake httpx.AsyncClient whose stream() yields `wav`."""
        from app.webapp.routers import sessions as sessions_router

        captured = {}

        class _FakeStream:
            def __init__(self):
                self.status_code = status_code

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def aiter_bytes(self):
                yield wav

            async def aread(self):
                return b""

        class _FakeClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            def stream(self, method, url, **kwargs):
                captured["method"] = method
                captured["url"] = url
                captured["json"] = kwargs.get("json")
                return _FakeStream()

        monkeypatch.setattr(sessions_router.httpx, "AsyncClient", _FakeClient)
        return captured

    def test_streams_wav_with_orpheus_payload(self, webapp_client, monkeypatch):
        client, _, _ = webapp_client
        captured = self._mock_httpx(monkeypatch, wav=b"RIFF....WAVE....")
        with client.stream("POST", "/api/tts/speak", json={"text": "ship it"}) as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("audio/wav")
            body = b"".join(resp.iter_bytes())
        assert body == b"RIFF....WAVE...."
        # The upstream call is the hub's OpenAI-shape speech endpoint with the
        # streamed-WAV Orpheus payload.
        assert captured["method"] == "POST"
        assert captured["url"] == "http://127.0.0.1:8000/v1/audio/speech"
        assert captured["json"]["model"] == "orpheus"
        assert captured["json"]["input"] == "ship it"
        assert captured["json"]["stream_format"] == "audio"

    def test_voice_forwarded(self, webapp_client, monkeypatch):
        client, _, _ = webapp_client
        captured = self._mock_httpx(monkeypatch)
        with client.stream(
            "POST", "/api/tts/speak", json={"text": "hi", "voice": "leo"}
        ) as resp:
            b"".join(resp.iter_bytes())
        assert captured["json"]["voice"] == "leo"

    def test_upstream_error_yields_empty_body(self, webapp_client, monkeypatch):
        """A hub error after the 200 is committed → a zero-byte WAV (the JS
        treats an empty body as a failure and falls back to Web Speech)."""
        client, _, _ = webapp_client
        self._mock_httpx(monkeypatch, status_code=502, wav=b"should-not-appear")
        with client.stream("POST", "/api/tts/speak", json={"text": "x"}) as resp:
            assert resp.status_code == 200
            body = b"".join(resp.iter_bytes())
        assert body == b""

    def test_empty_text_rejected(self, webapp_client):
        client, _, _ = webapp_client
        resp = client.post("/api/tts/speak", json={"text": "   "})
        assert resp.status_code == 400

    def test_disabled_when_url_unset(self, webapp_client):
        client, app, _ = webapp_client
        app.state.webapp_config.llm_hub_url = ""
        resp = client.post("/api/tts/speak", json={"text": "hello"})
        assert resp.status_code == 503


class TestStatusTtsFlag:
    def test_status_reports_tts_enabled(self, webapp_client):
        client, _, _ = webapp_client
        resp = client.get("/api/status")
        assert resp.status_code == 200
        assert resp.json()["tts"] is True

    def test_status_reports_tts_disabled(self, webapp_client):
        client, app, _ = webapp_client
        app.state.webapp_config.llm_hub_url = ""
        resp = client.get("/api/status")
        assert resp.status_code == 200
        assert resp.json()["tts"] is False
