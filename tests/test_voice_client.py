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


# --- streamed helpers (#168) -------------------------------------------


def test_create_session_posts_language(monkeypatch):
    captured = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["json"] = kwargs.get("json")
        return _Resp(200, {"session_id": "s1"})

    monkeypatch.setattr(voice_client.requests, "post", fake_post)
    out = voice_client.create_session("https://127.0.0.1:8443/", "es")
    assert out["session_id"] == "s1"
    assert captured["url"] == "https://127.0.0.1:8443/api/sessions"
    assert captured["json"] == {"language": "es"}


def test_send_chunk_posts_raw_body(monkeypatch):
    captured = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["data"] = kwargs.get("data")
        captured["headers"] = kwargs.get("headers")
        return _Resp(200, {"raw_bytes": 3})

    monkeypatch.setattr(voice_client.requests, "post", fake_post)
    out = voice_client.send_chunk("https://127.0.0.1:8443", "s1", b"abc", "audio/mp4")
    assert out["raw_bytes"] == 3
    assert captured["url"] == "https://127.0.0.1:8443/api/sessions/s1/chunk"
    assert captured["data"] == b"abc"
    assert captured["headers"]["Content-Type"] == "audio/mp4"


def test_finish_returns_transcript(monkeypatch):
    def fake_post(url, **kwargs):
        assert url == "https://127.0.0.1:8443/api/sessions/s1/finish"
        return _Resp(200, {"transcript": "done", "language": "en"})

    monkeypatch.setattr(voice_client.requests, "post", fake_post)
    out = voice_client.finish("https://127.0.0.1:8443", "s1")
    assert out["transcript"] == "done"


def test_finish_upstream_error_raises(monkeypatch):
    def fake_post(url, **kwargs):
        return _Resp(502, {"detail": "whisper blew up"})

    monkeypatch.setattr(voice_client.requests, "post", fake_post)
    with pytest.raises(voice_client.VoiceTranscriberError) as exc:
        voice_client.finish("https://127.0.0.1:8443", "s1")
    assert exc.value.status == 502


def test_events_url_builds_path():
    assert (
        voice_client.events_url("https://127.0.0.1:8443/", "s1")
        == "https://127.0.0.1:8443/api/sessions/s1/events"
    )
