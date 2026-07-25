"""Notifier configuration value objects.

Deliberately *just* the resolved values — no environment-variable names, no file
paths, no app-specific precedence rules. Each consuming app reads its own
secrets (from ``.env`` / ``config/*.json`` / its own config layer, under
whatever ``*_TELEGRAM_*`` env names it prefers) and hands the resolved strings
to :class:`TelegramConfig`. That keeps this vendored copy byte-identical across
apps while their config conventions vary freely.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TelegramConfig:
    """Resolved Telegram Bot API credentials. Both are secrets — keep them in ignored config."""

    bot_token: str
    chat_id: str
