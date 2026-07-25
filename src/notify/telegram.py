"""Telegram delivery via the Bot API.

Sends a one-line plain-text message to a single configured chat using the Bot
API ``sendMessage`` endpoint. Uses the standard library only (``urllib``) so a
consuming app takes on no extra dependency for one HTTPS POST.

The bot token and chat id are secrets: they come from the consuming app's
ignored config and are never committed.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from src.notify.base import NotifierError

_API = "https://api.telegram.org/bot{token}/sendMessage"
_TIMEOUT_SECONDS = 15


class TelegramNotifier:
    """A :class:`~src.notify.base.Notifier` that delivers text to one Telegram chat."""

    def __init__(self, bot_token: str, chat_id: str) -> None:
        if not bot_token or not chat_id:
            raise NotifierError("Telegram notifier needs both a bot token and a chat id")
        self._token = bot_token
        self._chat_id = chat_id

    def send_text(self, text: str) -> None:
        """Deliver a plain-text message to the configured chat.

        Raises :class:`NotifierError` on any transport, decode, or Bot-API
        rejection so the caller can retry, log, or swallow it as appropriate.
        """
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
        except urllib.error.URLError as exc:
            raise NotifierError(f"Telegram request failed: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise NotifierError("Telegram returned a non-JSON response") from exc
        if not body.get("ok"):
            raise NotifierError(f"Telegram rejected the message: {body.get('description')}")
