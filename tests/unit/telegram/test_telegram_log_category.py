"""Telegram log category (issue #1742).

The nightly zombie-cleanup summary is a routine log line, so it goes to
``telegram_chat_log``; job-failure alerts stay on ``telegram_chat_id``. An
unset log chat falls back to the alerts chat. The vendored Telegram primitive
is replaced by a recorder — nothing here sends a real message.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import List, Tuple

import pytest

from src import notifications as notif


class FakeTelegram:
    sent: List[Tuple[str, str]] = []

    def __init__(self, bot_token: str, chat_id: str) -> None:
        self._chat_id = chat_id

    def send_text(self, text: str) -> None:
        FakeTelegram.sent.append((self._chat_id, text))


@pytest.fixture(autouse=True)
def fake_telegram(monkeypatch):
    FakeTelegram.sent = []
    monkeypatch.setattr(notif, "TelegramNotifier", FakeTelegram)


def _cfg(**kw):
    base = dict(telegram_bot_token="tok", telegram_chat_id="alerts", telegram_chat_log="")
    base.update(kw)
    return SimpleNamespace(**base)


class TestBuilderCategory:
    def test_log_category_uses_the_log_chat_when_configured(self):
        cfg = _cfg(telegram_chat_log="log")
        notif.build_telegram_notifier_from_config(
            cfg, category=notif.TELEGRAM_CATEGORY_LOG).notify("t", "b", "info")
        assert FakeTelegram.sent == [("log", "t\n\nb")]

    def test_log_category_falls_back_to_the_alerts_chat_when_unset(self):
        notif.build_telegram_notifier_from_config(
            _cfg(), category=notif.TELEGRAM_CATEGORY_LOG).notify("t", "b", "info")
        assert [chat for chat, _ in FakeTelegram.sent] == ["alerts"]

    def test_no_category_stays_on_the_alerts_chat_even_with_a_log_chat(self):
        notif.build_telegram_notifier_from_config(
            _cfg(telegram_chat_log="log")).notify("t", "b", "error")
        assert [chat for chat, _ in FakeTelegram.sent] == ["alerts"]

    def test_log_chat_alone_without_credentials_is_a_no_op(self):
        cfg = _cfg(telegram_bot_token="", telegram_chat_log="log")
        built = notif.build_telegram_notifier_from_config(
            cfg, category=notif.TELEGRAM_CATEGORY_LOG)
        assert isinstance(built, notif.NoopNotifier)

    def test_a_config_without_the_log_field_behaves_as_before(self):
        cfg = SimpleNamespace(telegram_bot_token="tok", telegram_chat_id="alerts")
        notif.build_telegram_notifier_from_config(
            cfg, category=notif.TELEGRAM_CATEGORY_LOG).notify("t", "", "info")
        assert [chat for chat, _ in FakeTelegram.sent] == ["alerts"]
