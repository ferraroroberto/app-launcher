"""The local-llm-hub TTS loopback client (`src.tts_client`, issue #203).

Thin ``requests`` wrapper over the hub's ``/health`` probe plus pure URL /
payload builders — so we stub ``requests.request`` for health and assert the
error mapping, and exercise the builders directly.
"""

from __future__ import annotations

import pytest

from src import tts_client


class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


def test_health_true_when_hub_ok(monkeypatch):
    captured = {}

    def fake_request(method, url, **kwargs):
        captured["url"] = url
        captured["verify"] = kwargs.get("verify")
        return _Resp(200, {"status": "ok"})

    monkeypatch.setattr(tts_client.requests, "request", fake_request)
    assert tts_client.health("http://127.0.0.1:8000/") is True
    # No double slash from the trailing-slash base.
    assert captured["url"] == "http://127.0.0.1:8000/health"
    # Plain HTTP loopback — the hub serves no TLS, so verify stays True.
    assert captured["verify"] is True


def test_health_false_when_status_not_ok(monkeypatch):
    monkeypatch.setattr(
        tts_client.requests, "request",
        lambda *a, **k: _Resp(200, {"status": "starting"}),
    )
    assert tts_client.health("http://127.0.0.1:8000") is False


def test_health_connection_failure_raises_503(monkeypatch):
    def fake_request(method, url, **kwargs):
        raise tts_client.requests.RequestException("connection refused")

    monkeypatch.setattr(tts_client.requests, "request", fake_request)
    with pytest.raises(tts_client.TtsError) as exc:
        tts_client.health("http://127.0.0.1:8000")
    assert exc.value.status == 503


def test_speech_url_builds_path():
    assert (
        tts_client.speech_url("http://127.0.0.1:8000/")
        == "http://127.0.0.1:8000/v1/audio/speech"
    )


def test_payload_defaults_to_orpheus_streaming_pcm():
    p = tts_client.build_speech_payload("hello world")
    assert p["model"] == "orpheus"
    assert p["input"] == "hello world"
    assert p["voice"] == "tara"
    # Headerless PCM16 streaming for Web Audio playback (#206) — not WAV.
    assert p["response_format"] == "pcm"
    assert p["stream_format"] == "audio"
    assert "speed" not in p  # omitted unless explicitly set


def test_payload_unknown_voice_falls_back_to_default():
    p = tts_client.build_speech_payload("hi", voice="bogus")
    assert p["voice"] == "tara"


def test_payload_honours_known_voice_model_and_speed():
    p = tts_client.build_speech_payload("hi", voice="leo", model="orpheus", speed=1.2)
    assert p["voice"] == "leo"
    assert p["model"] == "orpheus"
    assert p["speed"] == 1.2
