"""A session's Links: URLs folded out of a transcript, grouped and
deduplicated, with anything credential-shaped removed.

Every id, host and line of content here is synthetic.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from src.session_view import link_titles as lt
from src.session_view import links as lk
from tests.unit.transcript import test_background_tasks as bt


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """No test reaches a real page or ``gh``; the cache starts empty."""
    monkeypatch.setattr(lt, "page_title", lambda url: None)
    monkeypatch.setattr(lt, "github_title", lambda url: None)
    lt.reset_cache()


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


def _tool_result(ts: str, tool_id: str, text: str) -> Dict[str, Any]:
    return {"type": "user", "timestamp": ts, "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tool_id, "content": text}]}}


def test_only_prompts_and_replies_are_listed_not_tool_noise(tmp_path):
    path = _write(tmp_path / "s.jsonl", [
        _text("2026-10-01T10:00:00Z", "please open http://localhost:3000/mine", role="user"),
        {"type": "assistant", "timestamp": "2026-10-01T10:00:01Z", "message": {"id": "m1", "content": [
            {"type": "thinking", "thinking": "maybe https://thinking.example/x"},
            {"type": "tool_use", "id": "r1", "name": "Read",
             "input": {"file_path": "/x/notes.md", "note": "https://input.example/y"}}]}},
        _tool_result("2026-10-01T10:00:02Z", "r1", "docs at https://noise.example/readme"),
        {"type": "assistant", "timestamp": "2026-10-01T10:00:03Z", "message": {"id": "m2", "content": [
            {"type": "tool_use", "id": "b1", "name": "Bash", "input": {"command": "curl https://cmd.example/z"}}]}},
        _tool_result("2026-10-01T10:00:04Z", "b1", "HTTP/1.1 301 Location: https://log.example/out"),
        _text("2026-10-01T10:00:05Z", "Done, see https://example.org/guide"),
    ])
    result = lk.fold_links(path)
    urls = sorted(x["url"] for group in result["groups"] for x in group["links"])
    assert urls == ["http://localhost:3000/mine", "https://example.org/guide"]
    assert result["total"] == 2


def test_a_sub_agents_replies_are_not_listed(tmp_path):
    side = _text("2026-10-01T10:00:00Z", "worker found https://sub.example/a")
    side["isSidechain"] = True
    path = _write(tmp_path / "s.jsonl", [side, _text("2026-10-01T10:00:01Z", "ok https://main.example/b")])
    assert [x["url"] for x in _by_group(lk.fold_links(path))["other"]] == ["https://main.example/b"]


def test_a_published_artifact_is_listed_only_from_the_artifact_result(tmp_path):
    path = _write(tmp_path / "s.jsonl", [
        {"type": "assistant", "timestamp": "2026-10-01T10:00:00Z", "message": {"id": "m1", "content": [
            {"type": "tool_use", "id": "a1", "name": "Artifact",
             "input": {"file_path": "/x/page.html", "title": "Quarterly View"}}]}},
        _tool_result("2026-10-01T10:00:01Z", "a1",
                     "Published https://claude.ai/artifact/ZzYyXxWw987654 (cdn https://cdn.example/lib.js)"),
        {"type": "assistant", "timestamp": "2026-10-01T10:00:02Z", "message": {"id": "m2", "content": [
            {"type": "tool_use", "id": "r1", "name": "Read", "input": {"file_path": "/x/a.md"}}]}},
        _tool_result("2026-10-01T10:00:03Z", "r1", "see https://claude.ai/artifact/NotMineNotMine1"),
    ])
    result = lk.fold_links(path)
    assert [g["key"] for g in result["groups"]] == ["artifacts"]
    only = result["groups"][0]["links"]
    assert [(x["url"], x["label"]) for x in only] == [
        ("https://claude.ai/artifact/ZzYyXxWw987654", "Quarterly View")]


def test_codex_shaped_rollout_lists_messages_not_tool_output(tmp_path):
    def item(ts, payload):
        return {"type": "response_item", "timestamp": ts, "payload": payload}
    path = _write(tmp_path / "s.jsonl", [
        item("2026-10-01T10:00:00Z", {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "check http://127.0.0.1:8080/"}]}),
        item("2026-10-01T10:00:01Z", {"type": "function_call", "call_id": "c1", "name": "shell",
                                      "arguments": "{\"command\": [\"curl\", \"https://cmd.example/q\"]}"}),
        item("2026-10-01T10:00:02Z", {"type": "function_call_output", "call_id": "c1",
                                      "output": "fetched https://noise.example/page"}),
        item("2026-10-01T10:00:03Z", {"type": "message", "role": "assistant", "content": [
            {"type": "output_text", "text": "PR is https://github.com/acme/widgets/pull/7"}]}),
    ])
    groups = _by_group(lk.fold_links(path, "codex"))
    assert sorted(groups) == ["github", "servers"]
    assert groups["servers"][0]["label"] == "127.0.0.1:8080"


def test_pi_shaped_history_lists_messages_not_tool_results(tmp_path):
    def msg(ts, message):
        return {"type": "message", "timestamp": ts, "message": message}
    path = _write(tmp_path / "s.jsonl", [
        msg("2026-10-01T10:00:00Z", {"role": "user", "content": [
            {"type": "text", "text": "look at https://example.org/start"}]}),
        msg("2026-10-01T10:00:01Z", {"role": "toolResult", "toolCallId": "t1", "isError": False,
                                     "content": [{"type": "text", "text": "log https://noise.example/l"}]}),
        msg("2026-10-01T10:00:02Z", {"role": "assistant", "content": [
            {"type": "text", "text": "got it: https://example.org/answer"}]}),
    ])
    urls = [x["url"] for x in _by_group(lk.fold_links(path, "pi"))["other"]]
    assert urls == ["https://example.org/answer", "https://example.org/start"]


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
    monkeypatch.setattr(lt, "page_title", lambda url: "Dev Preview")
    body = client.get(_ROUTE).json()
    assert body["available"] is True and body["total"] == 1
    assert body["groups"][0]["links"][0]["title"] == "localhost:9 · Dev Preview"


def test_a_published_artifact_carries_its_title_and_an_unpublished_one_none(tmp_path):
    path = _write(tmp_path / "s.jsonl", [
        {"type": "assistant", "timestamp": "2026-10-01T10:00:00Z", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Artifact", "input": {"title": "Weekly Report"}}]}},
        {"type": "user", "timestamp": "2026-10-01T10:00:05Z", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1",
             "content": "Published https://claude.ai/artifact/AbCdEfGh123456"}]}},
        _text("2026-10-01T10:01:00Z", "Older one https://claude.ai/artifact/ZzZzZzZz987654"),
    ])
    links = _by_group(lk.fold_links(path))["artifacts"]
    assert {x["url"].rsplit("/", 1)[-1]: x["title"] for x in links} == {
        "AbCdEfGh123456": "Weekly Report", "ZzZzZzZz987654": None}


def test_rows_run_newest_first_by_last_mention(tmp_path):
    path = _write(tmp_path / "s.jsonl", [
        _text("2026-10-01T10:00:00Z", "http://localhost:3000/a"),
        _text("2026-10-01T10:01:00Z", "http://localhost:4000/b"),
        _text("2026-10-01T10:02:00Z", "http://localhost:5000/c"),
        _text("2026-10-01T10:03:00Z", "http://localhost:3000/a"),
    ])
    rows = _by_group(lk.fold_links(path))["servers"]
    assert [x["url"].rsplit("/", 1)[-1] for x in rows] == ["a", "c", "b"]
    stamps = [x["last_seen"] for x in rows]
    assert stamps == sorted(stamps, reverse=True)


def test_route_says_no_transcript(webapp_client, _bypass_gate, monkeypatch, tmp_path):
    client, _, overrides = webapp_client
    _serve(overrides, monkeypatch, tmp_path / "missing.jsonl")
    body = client.get(_ROUTE).json()
    assert body["available"] is False and body["reason"] == "no_transcript"
