"""The voice-transcriber loopback client (`src.voice_client`, issue #165).

Thin ``requests`` wrappers over the voice-transcriber session API — so we
stub ``requests.post`` and assert the two-call shape (create → upload) plus
the error mapping.
"""

from __future__ import annotations

import pytest

from src import voice_client


class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


def test_transcribe_creates_then_uploads(monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("/api/sessions"):
            return _Resp(200, {"session_id": "14-32-07-abcd"})
        return _Resp(200, {"transcript": "buy milk", "language": "en"})

    monkeypatch.setattr(voice_client.requests, "post", fake_post)

    result = voice_client.transcribe(
        "https://127.0.0.1:8443/", "rec.webm", b"audio", "audio/webm", "en"
    )

    assert result["transcript"] == "buy milk"
    # Two calls: create (no double slash from the trailing-slash base) then
    # upload against the returned session id.
    assert calls[0][0] == "https://127.0.0.1:8443/api/sessions"
    assert calls[0][1]["json"] == {"language": "en"}
    assert calls[1][0] == "https://127.0.0.1:8443/api/sessions/14-32-07-abcd/upload"
    assert calls[1][1]["params"] == {"language": "en"}
    # Multipart field name must be 'file' per the session API contract.
    assert "file" in calls[1][1]["files"]
    # Self-signed loopback cert → verify must be disabled on every call.
    assert all(kw.get("verify") is False for _, kw in calls)


def test_transcribe_no_language_omits_it(monkeypatch):
    def fake_post(url, **kwargs):
        if url.endswith("/api/sessions"):
            assert kwargs["json"] == {}
            return _Resp(200, {"session_id": "s1"})
        assert kwargs["params"] is None
        return _Resp(200, {"transcript": "x"})

    monkeypatch.setattr(voice_client.requests, "post", fake_post)
    voice_client.transcribe("https://127.0.0.1:8443", "r.webm", b"a", "audio/webm")


def test_transcribe_upstream_error_raises(monkeypatch):
    def fake_post(url, **kwargs):
        if url.endswith("/api/sessions"):
            return _Resp(200, {"session_id": "s1"})
        return _Resp(503, {"detail": "ffmpeg not installed"})

    monkeypatch.setattr(voice_client.requests, "post", fake_post)
    with pytest.raises(voice_client.VoiceTranscriberError) as exc:
        voice_client.transcribe("https://127.0.0.1:8443", "r.webm", b"a", "audio/webm")
    assert exc.value.status == 503
    assert "ffmpeg" in str(exc.value)


def test_transcribe_connection_failure_is_503(monkeypatch):
    def fake_post(url, **kwargs):
        raise voice_client.requests.RequestException("connection refused")

    monkeypatch.setattr(voice_client.requests, "post", fake_post)
    with pytest.raises(voice_client.VoiceTranscriberError) as exc:
        voice_client.transcribe("https://127.0.0.1:8443", "r.webm", b"a", "audio/webm")
    assert exc.value.status == 503
