"""A session's Links: URLs folded out of a transcript, grouped and
deduplicated, with anything credential-shaped removed.

Every id, host and line of content here is synthetic.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from src.session_view import links as lk
from tests.unit.transcript import test_background_tasks as bt


def _write(path: Path, rows: List[Dict[str, Any]]) -> Path:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return path


def _text(ts: str, text: str, role: str = "assistant") -> Dict[str, Any]:
    return {"type": role, "timestamp": ts, "message": {"role": role, "content": [
        {"type": "text", "text": text}]}}


def _by_group(result: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    return {g["key"]: g["links"] for g in result["groups"]}


def test_artifact_pr_and_preview_are_grouped_once_each_newest_first(tmp_path):
    path = _write(tmp_path / "s.jsonl", [
        {"type": "assistant", "timestamp": "2026-10-01T10:00:00Z", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Artifact",
             "input": {"file_path": "/x/report.html", "title": "Weekly Report"}}]}},
        {"type": "user", "timestamp": "2026-10-01T10:00:05Z", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1",
             "content": "Published https://claude.ai/artifact/AbCdEfGh123456"}]}},
        _text("2026-10-01T10:05:00Z", "Opened https://github.com/acme/widgets/pull/12."),
        _text("2026-10-01T10:06:00Z", "Preview at http://100.64.1.2:8123/index.html now"),
        _text("2026-10-01T10:07:00Z", "PR again: https://github.com/acme/widgets/pull/12"),
        _text("2026-10-01T10:08:00Z", "Docs https://example.org/guide/"),
    ])
    result = lk.fold_links(path)
    groups = _by_group(result)
    assert [g["key"] for g in result["groups"]] == ["artifacts", "github", "servers", "other"]
    assert result["total"] == 4
    assert groups["artifacts"][0]["label"] == "Weekly Report"
    assert groups["github"] == [dict(groups["github"][0], label="acme/widgets#12", count=2,
                                     first_seen="2026-10-01T10:05:00Z",
                                     last_seen="2026-10-01T10:07:00Z")]
    assert groups["servers"][0]["label"] == "100.64.1.2:8123"
    assert groups["other"][0]["url"] == "https://example.org/guide"


def test_newest_first_within_a_group(tmp_path):
    path = _write(tmp_path / "s.jsonl", [
        _text("2026-10-01T10:00:00Z", "http://localhost:3000/a"),
        _text("2026-10-01T10:01:00Z", "http://localhost:4000/b"),
        _text("2026-10-01T10:02:00Z", "http://localhost:3000/a"),
    ])
    urls = [x["url"] for x in _by_group(lk.fold_links(path))["servers"]]
    assert urls == ["http://localhost:3000/a", "http://localhost:4000/b"]


@pytest.mark.parametrize("raw, expected", [
    ("https://host.example/p?api_key=abc", "https://host.example/p"),
    ("https://host.example/p?token=abc&x=1", "https://host.example/p"),
    ("https://host.example/p?v=" + "A" * 40, "https://host.example/p"),
    ("https://host.example/p?page=2", "https://host.example/p?page=2"),
])
def test_credential_looking_queries_are_dropped(tmp_path, raw, expected):
    path = _write(tmp_path / "s.jsonl", [_text("2026-10-01T10:00:00Z", "see " + raw)])
    only = _by_group(lk.fold_links(path))["other"]
    assert [x["url"] for x in only] == [expected]


def test_userinfo_urls_and_dotless_hosts_are_skipped(tmp_path):
    path = _write(tmp_path / "s.jsonl", [_text(
        "2026-10-01T10:00:00Z", "https://user:pw@host.example/x and http://foo and ftp://a.b/c")])
    assert lk.fold_links(path)["groups"] == []


def test_works_on_a_non_claude_shaped_line(tmp_path):
    path = _write(tmp_path / "s.jsonl", [
        {"timestamp": "2026-10-01T10:00:00Z", "payload": {"output": "ready: http://127.0.0.1:8080/"}}])
    assert _by_group(lk.fold_links(path))["servers"][0]["label"] == "127.0.0.1:8080"


def test_empty_transcript_lists_nothing(tmp_path):
    path = _write(tmp_path / "s.jsonl", [])
    assert lk.fold_links(path) == {"groups": [], "total": 0, "partial": False}


_ROUTE = "/api/claude-code/sessions/s1/links"


_bypass_gate = bt._bypass_gate
_serve = bt._serve


def test_route_is_passkey_and_refused_off_tailnet(webapp_client):
    from app.webapp.middleware import _terminal_guard_level
    assert _terminal_guard_level("/api/claude-code/sessions/abc/links") == "passkey"
    client, _, _ = webapp_client
    assert client.get(_ROUTE).status_code == 403


def test_route_lists_links(webapp_client, _bypass_gate, monkeypatch, tmp_path):
    client, _, overrides = webapp_client
    path = _write(tmp_path / "s.jsonl", [_text("2026-10-01T10:00:00Z", "http://localhost:9/x")])
    _serve(overrides, monkeypatch, path)
    body = client.get(_ROUTE).json()
    assert body["available"] is True and body["total"] == 1


def test_route_says_no_transcript(webapp_client, _bypass_gate, monkeypatch, tmp_path):
    client, _, overrides = webapp_client
    _serve(overrides, monkeypatch, tmp_path / "missing.jsonl")
    body = client.get(_ROUTE).json()
    assert body["available"] is False and body["reason"] == "no_transcript"
