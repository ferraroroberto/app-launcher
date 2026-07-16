"""Unit tests for :mod:`src.notifications` (issue #66).

The notifier path runs inside the run-job executor, so the contract is:
no exception escapes, no matter how broken the HTTP stack underneath.
The tests prove (a) wiring is correct on the happy path and (b) every
failure mode degrades to silence — Pushover 5xx, JSON decode error,
hub unreachable.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src import _loopback_http, notifications as notif


# ============================================================== Notifiers


class TestNoopNotifier:
    def test_returns_none(self):
        assert notif.NoopNotifier().notify("t", "b", "info") is None


class TestPushoverNotifier:
    def test_posts_credentials_and_message(self):
        http = MagicMock()
        http.post.return_value = SimpleNamespace(status_code=200, text="OK")

        n = notif.PushoverNotifier("tok-A", "user-B", http=http)
        n.notify("hello", "the body", "error")

        http.post.assert_called_once()
        url, = http.post.call_args.args
        assert url == notif.PUSHOVER_URL
        kwargs = http.post.call_args.kwargs
        assert kwargs["data"]["token"] == "tok-A"
        assert kwargs["data"]["user"] == "user-B"
        assert kwargs["data"]["title"] == "hello"
        assert kwargs["data"]["message"] == "the body"
        # severity=error → priority 1
        assert kwargs["data"]["priority"] == 1
        assert kwargs["timeout"] == 8.0

    def test_truncates_long_message(self):
        http = MagicMock()
        http.post.return_value = SimpleNamespace(status_code=200, text="OK")
        n = notif.PushoverNotifier("t", "u", http=http)
        n.notify("x", "y" * 2000, "info")
        sent = http.post.call_args.kwargs["data"]["message"]
        assert len(sent) <= 1024
        assert sent.endswith("…")

    def test_swallows_http_exception(self):
        http = MagicMock()
        http.post.side_effect = RuntimeError("offline")
        n = notif.PushoverNotifier("t", "u", http=http)
        # Must not raise — the executor is finalising a run and cannot
        # afford to crash here.
        n.notify("x", "y", "warning")

    def test_swallows_non_2xx(self, caplog):
        http = MagicMock()
        http.post.return_value = SimpleNamespace(status_code=500, text="boom")
        n = notif.PushoverNotifier("t", "u", http=http)
        n.notify("x", "y", "warning")
        # Logged at WARNING but no raise.


# ============================================================== Factory


class TestFactory:
    def test_returns_noop_when_creds_missing(self):
        cfg = SimpleNamespace(pushover_api_token="", pushover_user_key="")
        assert isinstance(notif.build_notifier_from_config(cfg), notif.NoopNotifier)

    def test_returns_noop_when_token_missing(self):
        cfg = SimpleNamespace(pushover_api_token="", pushover_user_key="u")
        assert isinstance(notif.build_notifier_from_config(cfg), notif.NoopNotifier)

    def test_returns_pushover_when_both_set(self):
        cfg = SimpleNamespace(pushover_api_token="t", pushover_user_key="u")
        n = notif.build_notifier_from_config(cfg)
        assert isinstance(n, notif.PushoverNotifier)


# ============================================================= LLM summary
#
# summarise_failure routes through the shared src.llm_client hub client
# (issue #520), so — like tests/test_llm_client.py — the hub round-trip is
# stubbed by monkeypatching the shared `_loopback_http.requests.request`
# rather than injecting a mock http object.


class _HubResp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


def _hub_completion(content):
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


class TestSummariseFailure:
    def test_returns_text_block_first_line(self, monkeypatch):
        monkeypatch.setattr(
            _loopback_http.requests,
            "request",
            lambda *a, **k: _HubResp(
                200, _hub_completion("ModuleNotFoundError: bs4\nfull stack...")
            ),
        )
        out = notif.summarise_failure("Traceback (most recent call last)...")
        assert out == "ModuleNotFoundError: bs4"

    def test_hub_unreachable_returns_none(self, monkeypatch):
        def fake_request(*a, **k):
            raise _loopback_http.requests.RequestException("connection refused")

        monkeypatch.setattr(_loopback_http.requests, "request", fake_request)
        assert notif.summarise_failure("oops") is None

    def test_non_2xx_returns_none(self, monkeypatch):
        monkeypatch.setattr(
            _loopback_http.requests, "request", lambda *a, **k: _HubResp(503)
        )
        assert notif.summarise_failure("oops") is None

    def test_empty_tail_short_circuits(self, monkeypatch):
        calls = {"n": 0}

        def fake_request(*a, **k):
            calls["n"] += 1
            return _HubResp(200, _hub_completion("x"))

        monkeypatch.setattr(_loopback_http.requests, "request", fake_request)
        assert notif.summarise_failure("") is None
        assert calls["n"] == 0

    def test_malformed_payload_returns_none(self, monkeypatch):
        monkeypatch.setattr(
            _loopback_http.requests,
            "request",
            lambda *a, **k: _HubResp(200, {"unexpected": "shape"}),
        )
        assert notif.summarise_failure("oops") is None

    def test_base_url_from_config_is_honoured(self, monkeypatch):
        captured = {}

        def fake_request(method, url, **kwargs):
            captured["url"] = url
            return _HubResp(200, _hub_completion("root cause"))

        monkeypatch.setattr(_loopback_http.requests, "request", fake_request)
        notif.summarise_failure("oops", base_url="http://127.0.0.1:9999")
        assert captured["url"] == "http://127.0.0.1:9999/v1/chat/completions"

    def test_missing_base_url_falls_back_to_constant(self, monkeypatch):
        captured = {}

        def fake_request(method, url, **kwargs):
            captured["url"] = url
            return _HubResp(200, _hub_completion("root cause"))

        monkeypatch.setattr(_loopback_http.requests, "request", fake_request)
        notif.summarise_failure("oops", base_url="")
        assert captured["url"] == (
            f"{notif.LOCAL_LLM_BASE_URL}/v1/chat/completions"
        )
