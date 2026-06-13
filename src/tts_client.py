"""Thin HTTP client for the local-llm-hub TTS endpoint (issue #203).

The Coding terminal's 🔊 read-aloud button can synthesize the agent's last
reply with the hub's high-quality Orpheus voice instead of the on-device Web
Speech voice. The hub exposes an OpenAI-shape ``POST /v1/audio/speech`` at
``http://127.0.0.1:8000`` that streams a WAV back when ``stream_format`` is
``"audio"`` — the webapp proxies to it over loopback so the phone never talks
to the hub directly.

Unlike the sibling voice-transcriber / photo-ocr clients, the hub binds
loopback only and serves **plain HTTP** (no self-signed TLS), so calls use
``verify=True`` (the default) and a plain ``http://`` base.

``health`` is a blocking ``requests`` call wrapped in ``asyncio.to_thread`` by
the router; the streamed synth itself is forwarded with ``httpx`` straight in
the router (mirroring the ``/api/transcribe/.../events`` SSE proxy), so this
module owns only the cheap health probe plus the URL / payload builders.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import requests

from src import _loopback_http

logger = logging.getLogger(__name__)

# The hub auto-loads Orpheus as the `audio_speech` role; name it explicitly so
# a host with several TTS backends still routes here (the hub resolves the
# model through its registry first, falling back to the role alias).
DEFAULT_MODEL = "orpheus"
# Orpheus ships eight voices; "tara" is the documented default (issue #203).
DEFAULT_VOICE = "tara"
VALID_VOICES = ("tara", "leah", "jess", "leo", "dan", "mia", "zac", "zoe")

# A short reply synthesizes quickly, but allow ample headroom for a cold
# Orpheus model loading its weights on the first take after the hub boots.
_HEALTH_TIMEOUT = 5.0


class TtsError(_loopback_http.LoopbackError):
    """Raised when the local-llm-hub is unreachable or returns an error."""


def health(base_url: str) -> bool:
    """Return True when the hub answers its ``/health`` probe with ``ok``.

    Raises :class:`TtsError` (status 503) when the hub is unreachable, so the
    caller can distinguish "configured but down" from "answered, not ok".
    """
    url = f"{base_url.rstrip('/')}/health"
    body = _loopback_http.request(
        "GET", url, error=TtsError, service="local-llm-hub",
        timeout=_HEALTH_TIMEOUT, allow_empty=True,
    )
    return bool(isinstance(body, dict) and body.get("status") == "ok")


def speech_url(base_url: str) -> str:
    """Upstream OpenAI-shape speech endpoint for ``POST /v1/audio/speech``."""
    return f"{base_url.rstrip('/')}/v1/audio/speech"


def build_speech_payload(
    text: str,
    voice: Optional[str] = None,
    model: Optional[str] = None,
    speed: Optional[float] = None,
) -> Dict[str, Any]:
    """Build the streamed-WAV request body for the hub speech endpoint.

    ``stream_format="audio"`` makes the hub hold the upstream connection open
    and forward bytes as they arrive (low time-to-first-audio); the open-ended
    RIFF header lets the browser play the WAV incrementally. An unknown
    ``voice`` falls back to the Orpheus default rather than erroring.
    """
    chosen_voice = voice if voice in VALID_VOICES else DEFAULT_VOICE
    payload: Dict[str, Any] = {
        "model": model or DEFAULT_MODEL,
        "input": text,
        "voice": chosen_voice,
        "response_format": "wav",
        "stream_format": "audio",
    }
    if speed is not None:
        payload["speed"] = speed
    return payload
