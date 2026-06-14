"""The local-llm-hub chat loopback client (`src.llm_client`, issue #210).

Thin ``requests`` wrapper (via ``_loopback_http``) over the hub's OpenAI-shape
``/v1/chat/completions`` plus pure URL / payload / extraction helpers — so we
stub the shared ``requests.request`` for the round-trip and assert the error
mapping, and exercise the builders directly.
"""

from __future__ import annotations

import pytest

from src import _loopback_http, llm_client


class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


def _completion(content):
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


def test_chat_url_builds_path():
    assert (
        llm_client.chat_url("http://127.0.0.1:8000/")
        == "http://127.0.0.1:8000/v1/chat/completions"
    )


def test_payload_uses_haiku_and_driving_prompt():
    p = llm_client.build_summary_payload("a long reply")
    assert p["model"] == "claude-haiku-4-5"
    assert p["stream"] is False
    assert p["messages"][0]["role"] == "system"
    assert "driving" in p["messages"][0]["content"].lower()
    assert p["messages"][1] == {"role": "user", "content": "a long reply"}


def test_summarize_returns_trimmed_content(monkeypatch):
    captured = {}

    def fake_request(method, url, **kwargs):
        captured["url"] = url
        captured["json"] = kwargs.get("json")
        captured["verify"] = kwargs.get("verify")
        return _Resp(200, _completion("  Ship it. Decide: merge now?  "))

    monkeypatch.setattr(_loopback_http.requests, "request", fake_request)
    out = llm_client.summarize("http://127.0.0.1:8000", "a long reply")
    assert out == "Ship it. Decide: merge now?"
    assert captured["url"] == "http://127.0.0.1:8000/v1/chat/completions"
    assert captured["json"]["messages"][1]["content"] == "a long reply"
    # Plain HTTP loopback — the hub serves no TLS, so verify stays True.
    assert captured["verify"] is True


def test_summarize_handles_list_content(monkeypatch):
    monkeypatch.setattr(
        _loopback_http.requests, "request",
        lambda *a, **k: _Resp(200, _completion(
            [{"type": "text", "text": "Gist here."}]
        )),
    )
    assert llm_client.summarize("http://127.0.0.1:8000", "x") == "Gist here."


def test_summarize_empty_completion_raises_502(monkeypatch):
    monkeypatch.setattr(
        _loopback_http.requests, "request",
        lambda *a, **k: _Resp(200, _completion("   ")),
    )
    with pytest.raises(llm_client.LlmError) as exc:
        llm_client.summarize("http://127.0.0.1:8000", "x")
    assert exc.value.status == 502


def test_summarize_connection_failure_raises_503(monkeypatch):
    def fake_request(method, url, **kwargs):
        raise _loopback_http.requests.RequestException("connection refused")

    monkeypatch.setattr(_loopback_http.requests, "request", fake_request)
    with pytest.raises(llm_client.LlmError) as exc:
        llm_client.summarize("http://127.0.0.1:8000", "x")
    assert exc.value.status == 503
