"""Thin HTTP client for the local-llm-hub chat endpoint (issue #210).

The Coding terminal's read-aloud control offers a "summarize & read" action: the
agent's last reply is condensed by the hub's cheap ``claude_haiku`` into a
short, driving-oriented gist (the essence + any decision to take) before it is
spoken. This module owns the single non-streaming chat call to the hub's
OpenAI-shape ``POST /v1/chat/completions`` at ``http://127.0.0.1:8000``; the
webapp proxies to it over loopback so the phone never talks to the hub directly.

Like :mod:`src.tts_client`, the hub binds loopback only and serves plain HTTP
(no self-signed TLS), so calls use ``verify=True`` (the default) and a plain
``http://`` base. The call is a blocking ``requests`` round-trip wrapped in
``asyncio.to_thread`` by the router.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from src import _loopback_http

logger = logging.getLogger(__name__)

# Cheap, fast model for a short summary — the hub routes this to the local
# Claude Code subscription (see the home-stack local-llm-hub).
DEFAULT_MODEL = "claude_haiku"

# Driving-mode system prompt: the listener is at the wheel and needs the gist
# plus any decision, not a transcript. Kept terse so Haiku returns plain
# speakable prose (no markdown, no preamble).
SUMMARY_SYSTEM_PROMPT = (
    "You summarize an AI coding assistant's reply for someone who is driving "
    "and listening hands-free. In 2-3 short sentences, give the essence of the "
    "reply and explicitly call out any decision or action the listener needs to "
    "take. Plain spoken prose only: no markdown, no lists, no code, no preamble "
    "such as 'Here is a summary'. If the reply asks the listener a question, "
    "lead with that question."
)

# A summary is a short generation, but allow ample headroom for a cold model
# loading its weights on the first call after the hub boots.
_SUMMARIZE_TIMEOUT = 60.0


class LlmError(_loopback_http.LoopbackError):
    """Raised when the local-llm-hub chat endpoint is unreachable or errors."""


def chat_url(base_url: str) -> str:
    """Upstream OpenAI-shape chat endpoint for ``POST /v1/chat/completions``."""
    return f"{base_url.rstrip('/')}/v1/chat/completions"


def build_summary_payload(text: str, model: Optional[str] = None) -> Dict[str, Any]:
    """Build the chat-completions body that asks the hub to summarize ``text``."""
    return {
        "model": model or DEFAULT_MODEL,
        "messages": [
            {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        "stream": False,
    }


def _extract_content(body: Any) -> str:
    """Pull the assistant message text out of an OpenAI-shape completion.

    Returns ``""`` for any unexpected shape so the caller can raise a clean
    502 rather than leaking a KeyError to the phone. Handles both a plain
    string ``content`` and the list-of-parts form some backends return.
    """
    try:
        message = body["choices"][0]["message"]
        content = message.get("content")
    except (KeyError, IndexError, TypeError):
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [
            p.get("text", "")
            for p in content
            if isinstance(p, dict) and p.get("type") in (None, "text")
        ]
        return " ".join(t for t in parts if t).strip()
    return ""


def summarize(base_url: str, text: str, model: Optional[str] = None) -> str:
    """Return a short, driving-oriented summary of ``text`` from the hub.

    Raises :class:`LlmError` (status 503) when the hub is unreachable, the
    upstream status when it answers ``>= 400``, or 502 when it returns an
    unparseable / empty completion.
    """
    body = _loopback_http.request(
        "POST",
        chat_url(base_url),
        error=LlmError,
        service="local-llm-hub",
        timeout=_SUMMARIZE_TIMEOUT,
        json=build_summary_payload(text, model=model),
        allow_empty=False,
    )
    summary = _extract_content(body)
    if not summary:
        raise LlmError("local-llm-hub returned an empty summary")
    return summary
