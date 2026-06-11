"""/api/transcribe — compose-bar voice dictation proxy (issues #165 / #168).

The webapp proxies recorded audio to the sibling voice-transcriber's session
API over loopback. Two shapes: single-shot (#165, one blob → transcript) and
the streamed path (#168, create → chunk → SSE events → finish). The voice
client is mocked (see conftest ``overrides["voice"]``) so these run with no
live voice-transcriber on :8443; the SSE proxy mocks ``httpx`` directly.
"""

from __future__ import annotations

import pytest


class TestTranscribeGate:
    """The transcribe routes carry the terminal's Tailscale-only + passkey
    gate (issues #165 / #168). The TestClient connects as host 'testclient'
    (not loopback, not tailnet), so it is refused — the gate logic itself
    lives in the middleware tests; this pins that the routes are wired in."""

    def test_single_shot_refused_off_tailnet(self, webapp_client):
        client, _, overrides = webapp_client
        resp = client.post(
            "/api/transcribe",
            files={"file": ("r.webm", b"audio", "audio/webm")},
        )
        assert resp.status_code == 403
        overrides["voice"].transcribe.assert_not_called()

    def test_streamed_create_refused_off_tailnet(self, webapp_client):
        client, _, overrides = webapp_client
        resp = client.post("/api/transcribe/sessions")
        assert resp.status_code == 403
        overrides["voice"].create_session.assert_not_called()

    def test_streamed_events_refused_off_tailnet(self, webapp_client):
        client, _, _ = webapp_client
        resp = client.get("/api/transcribe/sessions/vt-1/events")
        assert resp.status_code == 403


class TestTranscribe:
    """Treat the TestClient host as loopback so the terminal gate is skipped
    and the proxy logic is exercised (gate covered by TestTranscribeGate)."""

    @pytest.fixture(autouse=True)
    def _bypass_gate(self, monkeypatch):
        from app.webapp import middleware
        monkeypatch.setattr(
            middleware,
            "LOOPBACK_HOSTS",
            frozenset({"testclient", "127.0.0.1", "::1", "localhost"}),
        )

    def test_proxies_blob_and_returns_transcript(self, webapp_client):
        client, _, overrides = webapp_client
        overrides["voice"].transcribe.return_value = {
            "transcript": "buy milk and eggs",
            "language": "en",
        }
        resp = client.post(
            "/api/transcribe",
            files={"file": ("recording.webm", b"fake-audio", "audio/webm")},
        )
        assert resp.status_code == 200
        assert resp.json()["transcript"] == "buy milk and eggs"
        # base URL (sample default), filename, bytes, content-type, language.
        args = overrides["voice"].transcribe.call_args
        assert args.args[0] == "https://127.0.0.1:8443"
        assert args.args[1] == "recording.webm"
        assert args.args[2] == b"fake-audio"
        assert args.args[3] == "audio/webm"
        assert args.args[4] is None  # no ?language=

    def test_language_query_forwarded(self, webapp_client):
        client, _, overrides = webapp_client
        overrides["voice"].transcribe.return_value = {"transcript": "hola", "language": "es"}
        resp = client.post(
            "/api/transcribe?language=es",
            files={"file": ("r.mp4", b"x", "audio/mp4")},
        )
        assert resp.status_code == 200
        assert overrides["voice"].transcribe.call_args.args[4] == "es"

    def test_empty_recording_rejected(self, webapp_client):
        client, _, overrides = webapp_client
        resp = client.post(
            "/api/transcribe",
            files={"file": ("r.webm", b"", "audio/webm")},
        )
        assert resp.status_code == 400
        overrides["voice"].transcribe.assert_not_called()

    def test_disabled_when_url_unset(self, webapp_client):
        client, app, overrides = webapp_client
        app.state.webapp_config.voice_transcriber_url = ""
        resp = client.post(
            "/api/transcribe",
            files={"file": ("r.webm", b"audio", "audio/webm")},
        )
        assert resp.status_code == 503
        overrides["voice"].transcribe.assert_not_called()

    def test_upstream_error_maps_to_http_status(self, webapp_client):
        client, _, overrides = webapp_client
        voice = overrides["voice"]
        voice.transcribe.side_effect = voice.VoiceTranscriberError(
            "voice-transcriber unreachable", status=503
        )
        resp = client.post(
            "/api/transcribe",
            files={"file": ("r.webm", b"audio", "audio/webm")},
        )
        assert resp.status_code == 503
        assert "unreachable" in resp.json()["detail"]


class TestTranscribeStreaming:
    """Streamed path (#168): create / chunk / finish proxy + SSE events."""

    @pytest.fixture(autouse=True)
    def _bypass_gate(self, monkeypatch):
        from app.webapp import middleware
        monkeypatch.setattr(
            middleware,
            "LOOPBACK_HOSTS",
            frozenset({"testclient", "127.0.0.1", "::1", "localhost"}),
        )

    def test_create_proxies_to_voice_client(self, webapp_client):
        client, _, overrides = webapp_client
        overrides["voice"].create_session.return_value = {"session_id": "vt-9"}
        resp = client.post("/api/transcribe/sessions?language=en")
        assert resp.status_code == 200
        assert resp.json()["session_id"] == "vt-9"
        args = overrides["voice"].create_session.call_args
        assert args.args[0] == "https://127.0.0.1:8443"
        assert args.args[1] == "en"

    def test_chunk_forwards_raw_body(self, webapp_client):
        client, _, overrides = webapp_client
        overrides["voice"].send_chunk.return_value = {"raw_bytes": 5}
        resp = client.post(
            "/api/transcribe/sessions/vt-9/chunk",
            content=b"\x00\x01\x02\x03\x04",
            headers={"Content-Type": "audio/webm"},
        )
        assert resp.status_code == 200
        args = overrides["voice"].send_chunk.call_args
        assert args.args[0] == "https://127.0.0.1:8443"
        assert args.args[1] == "vt-9"
        assert args.args[2] == b"\x00\x01\x02\x03\x04"
        assert args.args[3] == "audio/webm"

    def test_empty_chunk_is_noop(self, webapp_client):
        client, _, overrides = webapp_client
        resp = client.post("/api/transcribe/sessions/vt-9/chunk", content=b"")
        assert resp.status_code == 200
        assert resp.json()["raw_bytes"] == 0
        overrides["voice"].send_chunk.assert_not_called()

    def test_finish_returns_transcript(self, webapp_client):
        client, _, overrides = webapp_client
        overrides["voice"].finish.return_value = {
            "transcript": "the whole note", "language": "en"
        }
        resp = client.post("/api/transcribe/sessions/vt-9/finish")
        assert resp.status_code == 200
        assert resp.json()["transcript"] == "the whole note"

    def test_finish_upstream_error_maps_status(self, webapp_client):
        client, _, overrides = webapp_client
        voice = overrides["voice"]
        voice.finish.side_effect = voice.VoiceTranscriberError("boom", status=502)
        resp = client.post("/api/transcribe/sessions/vt-9/finish")
        assert resp.status_code == 502

    def test_create_disabled_when_url_unset(self, webapp_client):
        client, app, overrides = webapp_client
        app.state.webapp_config.voice_transcriber_url = ""
        resp = client.post("/api/transcribe/sessions")
        assert resp.status_code == 503
        overrides["voice"].create_session.assert_not_called()

    def test_events_proxies_sse_stream(self, webapp_client, monkeypatch):
        """The SSE proxy forwards the upstream event-stream chunk-for-chunk."""
        client, _, _ = webapp_client
        from app.webapp.routers import sessions as sessions_router

        sse_bytes = (
            b":ok\n\n"
            b'event: partial\ndata: {"version":1,"transcript":"hi"}\n\n'
            b'event: final\ndata: {"transcript":"hi there"}\n\n'
        )

        class _FakeStream:
            status_code = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def aiter_raw(self):
                yield sse_bytes

        class _FakeClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            def stream(self, method, url):
                return _FakeStream()

        monkeypatch.setattr(sessions_router.httpx, "AsyncClient", _FakeClient)

        with client.stream("GET", "/api/transcribe/sessions/vt-9/events") as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            body = b"".join(resp.iter_bytes())
        assert b"event: partial" in body
        assert b'"transcript":"hi there"' in body

    def test_events_upstream_error_emits_sse_error(self, webapp_client, monkeypatch):
        client, _, _ = webapp_client
        from app.webapp.routers import sessions as sessions_router

        class _FakeStream:
            status_code = 404

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def aiter_raw(self):
                if False:
                    yield b""

        class _FakeClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            def stream(self, method, url):
                return _FakeStream()

        monkeypatch.setattr(sessions_router.httpx, "AsyncClient", _FakeClient)
        with client.stream("GET", "/api/transcribe/sessions/vt-9/events") as resp:
            body = b"".join(resp.iter_bytes())
        assert b"event: error" in body


class TestStatusVoiceFlag:
    def test_status_reports_voice_dictation_enabled(self, webapp_client):
        client, _, _ = webapp_client
        resp = client.get("/api/status")
        assert resp.status_code == 200
        assert resp.json()["voice_dictation"] is True

    def test_status_reports_voice_dictation_disabled(self, webapp_client):
        client, app, _ = webapp_client
        app.state.webapp_config.voice_transcriber_url = ""
        resp = client.get("/api/status")
        assert resp.status_code == 200
        assert resp.json()["voice_dictation"] is False
