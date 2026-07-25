"""Construct a notifier from config.

``none`` (the default) yields no notifier, so a caller records the message as
*skipped* and carries on. ``telegram`` yields a configured
:class:`~src.notify.telegram.TelegramNotifier`. New channels (Slack, email, …)
slot in here without touching call sites.
"""

from __future__ import annotations

from src.notify.base import Notifier
from src.notify.config import TelegramConfig
from src.notify.telegram import TelegramNotifier


def build_notifier(name: str, telegram: TelegramConfig) -> Notifier | None:
    """Return a notifier for ``name`` ('none' | 'telegram'), or None for 'none'."""
    if name == "none":
        return None
    if name == "telegram":
        return TelegramNotifier(telegram.bot_token, telegram.chat_id)
    raise ValueError(f"unknown notifier: {name!r} (expected 'none' or 'telegram')")
