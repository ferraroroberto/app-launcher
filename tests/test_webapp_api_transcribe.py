"""/api/transcribe — compose-bar voice dictation proxy (issue #165).

The webapp proxies a recorded audio blob to the sibling voice-transcriber's
session API over loopback and returns the transcript. The voice client is
mocked (see conftest ``overrides["voice"]``) so these run with no live
voice-transcriber on :8443.
"""

from __future__ import annotations

import pytest


class TestTranscribeGate:
    """/api/transcribe carries the terminal's Tailscale-only + passkey gate
    (issue #165). The TestClient connects as host 'testclient' (not loopback,
    not tailnet), so it is refused — the gate logic itself lives in the
    middleware tests; this just pins that the route is wired into it."""

    def test_refused_off_tailnet(self, webapp_client):
        client, _, overrides = webapp_client
        resp = client.post(
            "/api/transcribe",
            files={"file": ("r.webm", b"audio", "audio/webm")},
        )
        assert resp.status_code == 403
        overrides["voice"].transcribe.assert_not_called()


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
