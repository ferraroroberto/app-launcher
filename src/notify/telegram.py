"""Telegram delivery via the Bot API.

Sends a plain-text message to a single configured chat using the Bot API
``sendMessage`` endpoint, split into several calls when it exceeds Telegram's
per-message limit. Uses the standard library only (``urllib``) so a
consuming app takes on no extra dependency for one HTTPS POST.

The bot token and chat id are secrets: they come from the consuming app's
ignored config and are never committed.
"""

from __future__ import annotations

import json
import urllib.request

from src.notify.base import NotifierError

_API = "https://api.telegram.org/bot{token}/sendMessage"
_TIMEOUT_SECONDS = 15
# The Bot API documents the ``text`` parameter as "1-4096 characters"; a longer
# message is rejected, and a retry of the identical text fails the same way.
_MAX_MESSAGE_CHARS = 4096


def _chunk_text(text: str, limit: int) -> list[str]:
    """Split ``text`` into pieces of at most ``limit`` characters, exactly.

    Cuts at (and keeps) the last newline at or before ``limit`` so a line is
    never split when it fits, and ``"".join(_chunk_text(text, limit)) == text``.
    A single line longer than ``limit`` is hard-cut as a last resort.
    """
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit)
        cut = cut + 1 if cut > 0 else limit
        chunks.append(remaining[:cut])
        remaining = remaining[cut:]
    if remaining:
        chunks.append(remaining)
    return chunks


class TelegramNotifier:
    """A :class:`~src.notify.base.Notifier` that delivers text to one Telegram chat."""

    def __init__(self, bot_token: str, chat_id: str) -> None:
        if not bot_token or not chat_id:
            raise NotifierError("Telegram notifier needs both a bot token and a chat id")
        self._token = bot_token
        self._chat_id = chat_id

    def send_text(self, text: str) -> None:
        """Deliver a plain-text message to the configured chat.

        Text over Telegram's 4096-character limit is sent as several messages,
        cut at line breaks where possible, with no content lost between them.
        Delivery stops at the first failing piece.

        Raises :class:`NotifierError` on any transport, decode, or Bot-API
        rejection so the caller can retry, log, or swallow it as appropriate.
        """
        for piece in _chunk_text(text, _MAX_MESSAGE_CHARS):
            self._send_one(piece)

    def _send_one(self, text: str) -> None:
        """POST one ``sendMessage`` call; ``text`` is already within the limit."""
        payload = json.dumps({"chat_id": self._chat_id, "text": text}).encode("utf-8")
        request = urllib.request.Request(
            _API.format(token=self._token),
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
                body = json.loads(response.read().decode("utf-8"))
        except OSError as exc:
            # URLError / HTTPError, and the bare TimeoutError / ConnectionError
            # urllib lets escape once the request is sent and the response is
            # being read.
            raise NotifierError(f"Telegram request failed: {exc}") from exc
        except ValueError as exc:
            # JSONDecodeError and UnicodeDecodeError are both ValueErrors.
            raise NotifierError("Telegram returned a non-JSON or undecodable response") from exc
        if not isinstance(body, dict):
            raise NotifierError("Telegram returned a non-object JSON response")
        if not body.get("ok"):
            raise NotifierError(f"Telegram rejected the message: {body.get('description')}")
