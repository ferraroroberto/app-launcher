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
    lt.reset_title_cache()


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
    assert lk.fold_links(path) == {
        "groups": [], "total": 0, "partial": False, "offset": 0, "read_from": None}


_ROUTE = "/api/claude-code/sessions/s1/links"


_bypass_gate = bt._bypass_gate
_serve = bt._serve


def test_route_is_passkey_and_refused_off_tailnet(webapp_client):
    from src.middleware import _terminal_guard_level
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


def test_a_title_less_republish_and_a_reply_quote_keep_the_artifacts_name(tmp_path):
    url = "https://claude.ai/artifact/QqRrSsTt112233"
    path = _write(tmp_path / "s.jsonl", [
        {"type": "assistant", "timestamp": "2026-10-01T10:00:00Z", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Artifact",
             "input": {"url": url, "title": "Sample Board"}}]}},
        _tool_result("2026-10-01T10:00:01Z", "t1", "Updated"),
        {"type": "assistant", "timestamp": "2026-10-01T10:01:00Z", "message": {"content": [
            {"type": "tool_use", "id": "t2", "name": "Artifact", "input": {"url": url, "asset": True}}]}},
        _tool_result("2026-10-01T10:01:01Z", "t2", "Uploaded to " + url),
        _text("2026-10-01T10:02:00Z", "See " + url),
    ])
    links = _by_group(lk.fold_links(path))["artifacts"]
    assert [(x["label"], x["title"]) for x in links] == [("Sample Board", "Sample Board")]


def test_an_unnamed_artifact_row_keeps_its_short_id_never_the_shell_title(tmp_path):
    path = _write(tmp_path / "s.jsonl", [
        _text("2026-10-01T10:02:00Z", "See https://claude.ai/artifact/UuVvWwXx445566"),
    ])
    result = lt.attach_titles(lk.fold_links(path), web=lambda url: None)
    link = _by_group(result)["artifacts"][0]
    assert link["title"] is None and link["label"] == "Artifact UuVvWwXx"


def _artifact_call(ts: str, tool_id: str, **inputs: Any) -> Dict[str, Any]:
    return {"type": "assistant", "timestamp": ts, "message": {"id": "m" + tool_id, "content": [
        {"type": "tool_use", "id": tool_id, "name": "Artifact", "input": inputs}]}}


def test_only_the_sessions_own_successful_publish_gets_an_artifacts_row(tmp_path):
    mine = "https://claude.ai/artifact/MineMineMine0001"
    listing = ("Quickstart. Design systems: https://claude.ai/artifact/DesignSys0000001 "
               "https://claude.ai/artifact/DesignSys0000002 https://claude.ai/artifact/DesignSys0000003")
    path = _write(tmp_path / "s.jsonl", [
        _artifact_call("2026-10-01T10:00:00Z", "q1", action="quickstart", intent="other"),
        _tool_result("2026-10-01T10:00:01Z", "q1", listing),
        _artifact_call("2026-10-01T10:00:02Z", "l1", action="list"),
        _tool_result("2026-10-01T10:00:03Z", "l1", "Published artifacts: https://claude.ai/artifact/ListedListed0001"),
        _artifact_call("2026-10-01T10:00:04Z", "o1", action="open", url="https://claude.ai/artifact/OpenedOpened0001"),
        _tool_result("2026-10-01T10:00:05Z", "o1", "Opened https://claude.ai/artifact/OpenedOpened0001"),
        _artifact_call("2026-10-01T10:00:06Z", "x1", file_path="/x/page.html", icon="chart"),
        {"type": "user", "timestamp": "2026-10-01T10:00:07Z", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "x1", "is_error": True,
             "content": "refused, live version at https://claude.ai/artifact/RefusedRefused001"}]}},
        _artifact_call("2026-10-01T10:00:08Z", "p1", file_path="/x/page.html", icon="chart"),
        _tool_result("2026-10-01T10:00:09Z", "p1", "Published /x/page.html at " + mine),
    ])
    links = _by_group(lk.fold_links(path))["artifacts"]
    assert [(x["url"], x["label"]) for x in links] == [(mine, "page.html")]


def test_an_artifact_url_a_reply_wrote_still_gets_a_row_beside_the_published_one(tmp_path):
    quoted = "https://claude.ai/artifact/QuotedQuoted00001"
    path = _write(tmp_path / "s.jsonl", [
        _artifact_call("2026-10-01T10:00:00Z", "q1", action="quickstart", intent="other"),
        _tool_result("2026-10-01T10:00:01Z", "q1", "Design system https://claude.ai/artifact/DesignSys0000001"),
        _text("2026-10-01T10:00:02Z", "Here is the one I meant: " + quoted),
    ])
    assert [x["url"] for x in _by_group(lk.fold_links(path))["artifacts"]] == [quoted]


def _numbered(count: int) -> List[Dict[str, Any]]:
    return [_text("2026-10-01T10:%02d:00Z" % i, "Page http://localhost:3000/p%02d" % i) for i in range(count)]


def _urls(result: Dict[str, Any]) -> List[str]:
    return [x["url"].rsplit("/", 1)[-1] for g in result["groups"] for x in g["links"]]


def test_each_request_scans_one_window_and_before_pages_back_to_the_start(tmp_path, monkeypatch):
    path = _write(tmp_path / "s.jsonl", _numbered(30))
    line = len(path.read_bytes().splitlines(keepends=True)[0])
    # A window of exactly ten lines: its start falls on a line boundary, which
    # the next older window must not lose.
    monkeypatch.setattr(lk, "SCAN_CEILING", line * 10)
    first = lk.fold_links(path)
    assert first["partial"] is True and first["read_from"] == "2026-10-01T10:20:00Z"
    assert _urls(first) == ["p%02d" % i for i in range(29, 19, -1)]
    seen, window, hops = list(_urls(first)), first, 0
    while window["partial"]:
        older = lk.fold_links(path, before=window["offset"])
        assert window["offset"] - older["offset"] <= lk.SCAN_CEILING
        assert not set(_urls(older)) & set(seen)
        seen += _urls(older)
        window, hops = older, hops + 1
    assert hops == 2 and window["offset"] == 0 and window["read_from"] == "2026-10-01T10:00:00Z"
    assert sorted(seen) == ["p%02d" % i for i in range(30)]


def test_a_window_cut_mid_line_starts_on_the_next_whole_line(tmp_path, monkeypatch):
    path = _write(tmp_path / "s.jsonl", _numbered(12))
    line = len(path.read_bytes().splitlines(keepends=True)[0])
    monkeypatch.setattr(lk, "SCAN_CEILING", line * 4 + 17)
    first = lk.fold_links(path)
    older = lk.fold_links(path, before=first["offset"])
    assert _urls(first) == ["p%02d" % i for i in range(11, 7, -1)]
    assert _urls(older) == ["p%02d" % i for i in range(7, 3, -1)]


def test_a_small_transcript_is_whole_and_before_zero_reads_nothing(tmp_path):
    path = _write(tmp_path / "s.jsonl", _numbered(3))
    whole = lk.fold_links(path)
    assert whole["partial"] is False and whole["offset"] == 0 and whole["total"] == 3
    assert whole["read_from"] == "2026-10-01T10:00:00Z"
    assert lk.fold_links(path, before=0)["total"] == 0


def test_route_pages_back_with_before(webapp_client, _bypass_gate, monkeypatch, tmp_path):
    client, _, overrides = webapp_client
    path = _write(tmp_path / "s.jsonl", _numbered(8))
    line = len(path.read_bytes().splitlines(keepends=True)[0])
    monkeypatch.setattr(lk, "SCAN_CEILING", line * 4)
    _serve(overrides, monkeypatch, path)
    first = client.get(_ROUTE).json()
    older = client.get(_ROUTE, params={"before": first["offset"]}).json()
    assert first["partial"] is True and older["partial"] is False
    assert first["total"] == 4 and older["total"] == 4
    assert client.get(_ROUTE, params={"before": -1}).status_code == 422


def test_an_artifacts_name_is_the_call_title_then_the_page_title_then_the_file_name(tmp_path):
    def row(i: int, **inputs: Any) -> List[Dict[str, Any]]:
        return [_artifact_call("2026-10-01T10:0%d:00Z" % i, "t%d" % i, **inputs),
                _tool_result("2026-10-01T10:0%d:01Z" % i, "t%d" % i,
                             "Published https://claude.ai/artifact/Page%02dPage%02dPage" % (i, i))]

    titled = tmp_path / "titled.html"
    titled.write_text("<html><head>\n<title> Weekend &amp;\n Plans </title></head></html>", encoding="utf-8")
    plain = tmp_path / "plain.html"
    plain.write_text("<html><body>no title</body></html>", encoding="utf-8")
    late = tmp_path / "late.html"
    late.write_text("<!-- " + "x" * lk.HEAD_BYTES + " --><title>Too far in</title>", encoding="utf-8")
    notes = tmp_path / "notes.txt"
    notes.write_text("<title>Not a page</title>", encoding="utf-8")
    path = _write(tmp_path / "s.jsonl",
                  row(1, file_path=str(titled), title="Call Title")
                  + row(2, file_path=str(titled))
                  + row(3, file_path=str(plain))
                  + row(4, file_path=str(tmp_path / "gone.html"))
                  + row(5, file_path=str(late))
                  + row(6, file_path=str(notes))
                  + row(7, file_path="relative/page.html"))
    links = _by_group(lk.fold_links(path))["artifacts"]
    assert {x["url"].split("/")[-1][4:6]: x["label"] for x in links} == {
        "01": "Call Title", "02": "Weekend & Plans", "03": "plain.html", "04": "gone.html",
        "05": "late.html", "06": "notes.txt", "07": "page.html"}


def test_a_publish_call_in_an_older_window_names_the_row_once_that_window_is_read(tmp_path, monkeypatch):
    url = "https://claude.ai/artifact/OldOldOldOld0001"
    rows = [_artifact_call("2026-10-01T09:00:00Z", "t1", title="Early Board"),
            _tool_result("2026-10-01T09:00:01Z", "t1", "Published " + url)]
    rows += _numbered(10)
    rows.append(_text("2026-10-01T11:00:00Z", "Back to " + url))
    path = _write(tmp_path / "s.jsonl", rows)
    line = len(path.read_bytes().splitlines(keepends=True)[3])
    monkeypatch.setattr(lk, "SCAN_CEILING", line * 5)
    first = lk.fold_links(path)
    assert [(x["label"], x["title"]) for x in _by_group(first)["artifacts"]] == [("Artifact OldOldOl", None)]
    oldest = first
    while oldest["partial"]:
        oldest = lk.fold_links(path, before=oldest["offset"])
    assert [(x["label"], x["title"]) for x in _by_group(oldest)["artifacts"]] == [("Early Board", "Early Board")]


def test_a_quoted_artifact_the_session_listed_takes_the_listed_title(tmp_path):
    quoted = "https://claude.ai/artifact/ListedOneListed01"
    other = "https://claude.ai/artifact/NotListedNotListd2"
    listing = ("2 artifacts (newest first):\n"
               "- (mine) Garden Planner — %s — updated 2026-10-08\n"
               "- (shared) Something Else — https://claude.ai/artifact/ElseElseElse0003 — updated 2026-10-07" % quoted)
    path = _write(tmp_path / "s.jsonl", [
        _artifact_call("2026-10-01T10:00:00Z", "l1", action="list"),
        _tool_result("2026-10-01T10:00:01Z", "l1", listing),
        _text("2026-10-01T10:02:00Z", "Open %s and %s" % (quoted, other)),
    ])
    links = _by_group(lk.fold_links(path))["artifacts"]
    assert {x["url"].rsplit("/", 1)[-1]: (x["label"], x["title"]) for x in links} == {
        "ListedOneListed01": ("Garden Planner", "Garden Planner"),
        "NotListedNotListd2": ("Artifact NotListe", None)}


def test_a_publish_name_beats_the_listed_one(tmp_path):
    url = "https://claude.ai/artifact/PublishedPublish01"
    path = _write(tmp_path / "s.jsonl", [
        _artifact_call("2026-10-01T10:00:00Z", "l1", action="list"),
        _tool_result("2026-10-01T10:00:01Z", "l1", "- (mine) Old Name — %s — updated 2026-10-08" % url),
        _artifact_call("2026-10-01T10:01:00Z", "p1", title="Fresh Name"),
        _tool_result("2026-10-01T10:01:01Z", "p1", "Published " + url),
    ])
    assert [x["label"] for x in _by_group(lk.fold_links(path))["artifacts"]] == ["Fresh Name"]
