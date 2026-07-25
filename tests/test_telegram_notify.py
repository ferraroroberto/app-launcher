"""Unit tests for the vendored Telegram notifier (src/notify/).

Offline by construction: no real token, chat, or network — ``urlopen`` is
monkeypatched. Covers the importable contract every consuming app relies on,
mirroring the byte-for-byte guarantee of the tray's vendored primitives.
"""

from __future__ import annotations

import json
import urllib.error
from contextlib import contextmanager
from typing import Any

import pytest

from src.notify import (
    NotifierError,
    TelegramConfig,
    TelegramNotifier,
    build_notifier,
)
from src.notify import telegram as telegram_mod


def test_build_notifier_branches() -> None:
    assert build_notifier("none", TelegramConfig("", "")) is None
    assert isinstance(
        build_notifier("telegram", TelegramConfig("t", "c")), TelegramNotifier
    )
    with pytest.raises(ValueError):
        build_notifier("carrier-pigeon", TelegramConfig("t", "c"))


def test_telegram_requires_credentials() -> None:
    with pytest.raises(NotifierError):
        TelegramNotifier("", "")
    with pytest.raises(NotifierError):
        TelegramNotifier("token", "")


@contextmanager
def _fake_response(payload: dict[str, Any]):
    class _Resp:
        def read(self) -> bytes:
            return json.dumps(payload).encode("utf-8")

    yield _Resp()


def test_send_text_success(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_urlopen(request: Any, timeout: int = 0):  # noqa: ANN401
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _fake_response({"ok": True})

    monkeypatch.setattr(telegram_mod.urllib.request, "urlopen", fake_urlopen)
    TelegramNotifier("TOKEN", "CHAT").send_text("alarm triggered")

    assert "botTOKEN/sendMessage" in captured["url"]
    assert captured["body"]["chat_id"] == "CHAT"
    assert captured["body"]["text"] == "alarm triggered"


def test_send_text_rejected_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(request: Any, timeout: int = 0):  # noqa: ANN401
        return _fake_response({"ok": False, "description": "chat not found"})

    monkeypatch.setattr(telegram_mod.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(NotifierError, match="chat not found"):
        TelegramNotifier("TOKEN", "CHAT").send_text("hi")


def test_send_text_transport_error_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(request: Any, timeout: int = 0):  # noqa: ANN401
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr(telegram_mod.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(NotifierError, match="Telegram request failed"):
        TelegramNotifier("TOKEN", "CHAT").send_text("hi")


def test_send_text_non_json_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    @contextmanager
    def _bad_response():
        class _Resp:
            def read(self) -> bytes:
                return b"<html>502 Bad Gateway</html>"

        yield _Resp()

    def fake_urlopen(request: Any, timeout: int = 0):  # noqa: ANN401
        return _bad_response()

    monkeypatch.setattr(telegram_mod.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(NotifierError, match="non-JSON"):
        TelegramNotifier("TOKEN", "CHAT").send_text("hi")
