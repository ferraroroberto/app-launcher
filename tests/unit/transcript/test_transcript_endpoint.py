"""Coding-tab transcript: the page and entry endpoints."""

from __future__ import annotations

import json
import logging
import os
from typing import List

import pytest

from src import board
from src.transcript_flavors import _shared as tf_shared

from tests.unit.transcript._support import (
    AGY_CAPTURE,
    COPILOT_CAPTURE,
    GROK_CAPTURE,
    _BRIDGE,
    _CONV,
    _RESUMED,
    _codex,
    _conversation,
    _kinds,
    _live,
    _png_b64,
    _registry_entry,
    _user,
    _write_jsonl,
)


# --------------------------------------------------------------- router


def test_transcript_path_classified_passkey():
    from app.webapp.middleware import _terminal_guard_level
    assert _terminal_guard_level("/api/claude-code/sessions/abc/transcript") == "passkey"


def test_transcript_refused_off_tailnet(webapp_client):
    client, _, _ = webapp_client
    assert client.get("/api/claude-code/sessions/s1/transcript").status_code == 403


class TestTranscriptEndpoint:

    def test_an_unreachable_session_host_is_not_a_missing_session(
        self, webapp_client, _bypass_gate
    ):
        """#1308: a list the host would not answer says nothing about the
        session, so it must not be reported as the terminal
        `session_not_found` the client latches off on."""
        from src.clients.session import SessionHostError

        client, _, overrides = webapp_client
        overrides["session"].list_sessions.side_effect = SessionHostError("timed out")
        for query in ("", "?after=0&size=0"):
            body = client.get(f"/api/claude-code/sessions/s1/transcript{query}").json()
            assert body["available"] is False, query
            assert body["reason"] == "session_host_unreachable", query
        entry = client.get("/api/claude-code/sessions/s1/transcript/entry?offset=0").json()
        assert entry["reason"] == "session_host_unreachable"
        image = client.get("/api/claude-code/sessions/s1/transcript/image?offset=0&n=0")
        assert image.status_code == 503
        # Once the host answers again, an absent session is absent again.
        overrides["session"].list_sessions.side_effect = None
        overrides["session"].list_sessions.return_value = []
        body = client.get("/api/claude-code/sessions/s1/transcript").json()
        assert body["reason"] == "session_not_found"

    def test_unknown_session(self, webapp_client, _bypass_gate):
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = []
        body = client.get("/api/claude-code/sessions/s1/transcript").json()
        assert body["available"] is False and body["reason"] == "session_not_found"
        assert body["entries"] == [] and body["next_cursor"] is None

    def test_detached_claude_row_reads_its_claimed_history(
        self, webapp_client, _bypass_gate, monkeypatch, tmp_path
    ):
        # #966: a detached row has no PTY capture, but the reader never uses one.
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = [_live(kind="remote")]
        path = _write_jsonl(tmp_path / "t.jsonl", _conversation(2))
        monkeypatch.setattr(board, "state_row_for_session", lambda live, rows, sid: {"transcript_path": str(path)})
        body = client.get("/api/claude-code/sessions/s1/transcript").json()
        assert body["available"] is True and body["source"] == "native"
        assert "prompt 1 p" in [e["text"] for e in body["entries"] if e["kind"] == "user"]

    def test_live_claude_session_with_no_state_row_still_reads_its_history(
        self, webapp_client, _bypass_gate, monkeypatch, tmp_path
    ):
        """#1023 regression: the hook deletes a session's row on `/resume`
        and `/clear` and only the next prompt writes it back, so a live
        session is rowless for as long as its user reads rather than types.
        Before the filesystem fallback that answered `no_transcript` over a
        conversation sitting readable on disk, while Terminal mode showed
        it. The row stays first when there is one."""
        from src.session_view import transcript_locate

        client, _, overrides = webapp_client
        session = _live(project_dir=r"E:\work\project", started_at=1_000_000.0)
        overrides["session"].list_sessions.return_value = [session]
        monkeypatch.setattr(board, "state_row_for_session", lambda live, rows, sid: None)
        monkeypatch.setattr(transcript_locate, "_CLAUDE_PROJECTS_DIR", tmp_path)
        folder = tmp_path / "E--work-project"
        folder.mkdir()
        path = _write_jsonl(folder / "conv.jsonl", _conversation(2))
        os.utime(path, (1_000_100.0, 1_000_100.0))

        body = client.get("/api/claude-code/sessions/s1/transcript").json()
        assert body["available"] is True and body["source"] == "native"
        assert "prompt 1 p" in [e["text"] for e in body["entries"] if e["kind"] == "user"]
        # A second live session in the same folder makes "newest here"
        # meaningless — refuse rather than show the wrong conversation.
        overrides["session"].list_sessions.return_value = [
            session, _live(sid="s2", project_dir=r"E:\work\project"),
        ]
        refused = client.get("/api/claude-code/sessions/s1/transcript").json()
        assert refused["available"] is False and refused["reason"] == "no_transcript"

    def test_channel_session_in_a_shared_folder_reads_its_own_conversation(
        self, webapp_client, _bypass_gate, monkeypatch, tmp_path
    ):
        """#1393 end to end: a rowless Remote Control session whose folder is
        shared by two live siblings reads the conversation its own bridge id
        names, and each sibling reads its own, not this one's. The link comes
        from the session's PTY capture (#1399), not the session-host's row."""
        from src.session_view import transcript_locate

        from src import audit

        client, _, overrides = webapp_client
        # The session-host's rows carry no link: the router reads it off each
        # session's PTY capture, as the Coding tab and the Board do (#1399).
        live = [
            _live(sid=f"s-{k}", project_dir=r"E:\work\project", started_at=2_000_000.0)
            for k in "abc"
        ]
        overrides["session"].list_sessions.return_value = live
        captures = tmp_path / "captures"
        captures.mkdir()
        for k in "abc":
            (captures / f"s-{k}.transcript").write_text(
                f"Remote Control active\nhttps://claude.ai/code/{_BRIDGE[k]}\n",
                encoding="utf-8",
            )
        monkeypatch.setattr(audit, "transcript_path", lambda sid: captures / f"{sid}.transcript")
        monkeypatch.setattr(board, "state_row_for_session", lambda live, rows, sid: None)
        projects, registry = tmp_path / "projects", tmp_path / "sessions"
        monkeypatch.setattr(transcript_locate, "_CLAUDE_PROJECTS_DIR", projects)
        monkeypatch.setattr(transcript_locate, "_CLAUDE_SESSIONS_DIR", registry)
        folder = projects / "E--work-project"
        folder.mkdir(parents=True)
        for pid, key in enumerate("abc", start=100):
            _registry_entry(registry, pid, key)
            path = _write_jsonl(folder / f"{_CONV[key]}.jsonl", _conversation(2))
            os.utime(path, (1_000_100.0, 1_000_100.0))
        # Distinguish the conversations by content.
        _write_jsonl(folder / f"{_CONV['b']}.jsonl", _conversation(3))

        def prompts(sid: str) -> List[str]:
            body = client.get(f"/api/claude-code/sessions/{sid}/transcript").json()
            assert body["available"] is True, body
            return [e["text"] for e in body["entries"] if e["kind"] == "user"]

        assert "prompt 2 pp" not in prompts("s-a")
        assert "prompt 2 pp" in prompts("s-b")
        assert "prompt 2 pp" not in prompts("s-c")

    def test_resumed_claude_session_shows_history_before_its_first_prompt(
        self, webapp_client, _bypass_gate, monkeypatch, tmp_path, caplog
    ):
        """#1155 end to end: a session launched with `--resume <id>`, rowless
        and sharing its folder with another live session, reads its
        conversation; a fresh neighbour still answers no_transcript, and the
        log line names the guard that refused."""
        from src.session_view import transcript_locate

        client, _, overrides = webapp_client
        resumed = _live(
            project_dir=r"E:\work\project", started_at=2_000_000.0,
            flags=f"--resume {_RESUMED} --model opus",
        )
        neighbour = _live(sid="s2", project_dir=r"E:\work\project", started_at=2_000_000.0)
        overrides["session"].list_sessions.return_value = [resumed, neighbour]
        monkeypatch.setattr(board, "state_row_for_session", lambda live, rows, sid: None)
        monkeypatch.setattr(transcript_locate, "_CLAUDE_PROJECTS_DIR", tmp_path)
        folder = tmp_path / "E--work-project"
        folder.mkdir()
        path = _write_jsonl(folder / f"{_RESUMED}.jsonl", _conversation(2))
        os.utime(path, (1_000_100.0, 1_000_100.0))

        body = client.get("/api/claude-code/sessions/s1/transcript").json()
        assert body["available"] is True and body["source"] == "native"
        assert "prompt 1 p" in [e["text"] for e in body["entries"] if e["kind"] == "user"]

        with caplog.at_level("INFO"):
            refused = client.get("/api/claude-code/sessions/s2/transcript").json()
        assert refused["available"] is False and refused["reason"] == "no_transcript"
        assert "no_transcript (refused: folder_shared)" in caplog.text

    def test_detached_codex_row_reads_its_rollout(self, webapp_client, _bypass_gate, monkeypatch, tmp_path):
        from app.webapp.routers import session_transcript as router_mod
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = [_live(kind="remote", agent="codex")]
        path = _write_jsonl(tmp_path / "rollout.jsonl", [
            _codex({"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hey"}]}),
        ])
        monkeypatch.setattr(router_mod, "find_codex_transcript", lambda session: path)
        body = client.get("/api/claude-code/sessions/s1/transcript").json()
        assert body["available"] is True and body["source"] == "codex"
        monkeypatch.setattr(router_mod, "find_codex_transcript", lambda session: None)
        assert client.get("/api/claude-code/sessions/s1/transcript").json()["reason"] == "no_transcript"

    def test_grok_row_reads_its_updates_stream(self, webapp_client, _bypass_gate, monkeypatch):
        """A Grok session resolves through the state row's `transcript_path`
        — the same non-Codex path Claude uses — and reports its own source
        name rather than being mislabelled as the Codex reader."""
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = [_live(agent="grok")]
        monkeypatch.setattr(
            board, "state_row_for_session",
            lambda live, rows, sid: {"transcript_path": str(GROK_CAPTURE)},
        )
        body = client.get("/api/claude-code/sessions/s1/transcript").json()
        assert body["available"] is True and body["source"] == "grok"
        assert body["tool_errors"] == "reported"
        assert [e["kind"] for e in body["entries"]][:3] == ["user", "thinking", "assistant"]
        # The first exchange's answer — the capture's last assistant belongs
        # to #1020's failing-read exchange, which is one short line.
        entry = [e for e in body["entries"] if e["kind"] == "assistant"][1]
        full = client.get(
            f"/api/claude-code/sessions/s1/transcript/entry?offset={entry['offset']}"
        ).json()
        assert full["available"] is True
        assert full["text"].startswith("The `docs` folder")

    def test_image_route_serves_a_transcript_image_unstored(
        self, webapp_client, _bypass_gate, monkeypatch, tmp_path
    ):
        """#1265: an entry's image ref fetches the decoded bytes, typed from
        the bytes themselves and never stored (``no-store``); a ref that
        isn't an image is a 404, like a bad offset."""
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = [_live(agent="claude")]
        png = _png_b64()
        path = _write_jsonl(tmp_path / "c.jsonl", [_user([
            {"type": "text", "text": "Look"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": png}},
        ])])
        monkeypatch.setattr(
            board, "state_row_for_session",
            lambda live, rows, sid: {"transcript_path": str(path)},
        )
        entry = client.get("/api/claude-code/sessions/s1/transcript").json()["entries"][0]
        assert png[:12] not in json.dumps(entry)
        ref = entry["images"][0]
        res = client.get(f"/api/claude-code/sessions/s1/transcript/image?offset={ref['offset']}&n={ref['n']}")
        assert res.status_code == 200
        assert res.headers["content-type"] == "image/png"          # sniffed, not the declared jpeg
        assert res.headers["cache-control"] == "no-store"
        assert res.content.startswith(b"\x89PNG")
        missing = client.get(f"/api/claude-code/sessions/s1/transcript/image?offset={ref['offset']}&n=1")
        assert missing.status_code == 404

    def test_grok_unclaimed_row_is_no_transcript_not_a_stale_neighbour(
        self, webapp_client, _bypass_gate, monkeypatch
    ):
        """Correlation fails safe (#1012): Grok keeps a session folder per
        working directory, including ones that no longer exist (a removed
        worktree). When the claim walk assigns this session no row, the
        answer is `no_transcript` — never a nearby folder's stream, which
        would show another session's text.
        """
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = [_live(agent="grok")]
        monkeypatch.setattr(board, "state_row_for_session", lambda live, rows, sid: None)
        body = client.get("/api/claude-code/sessions/s1/transcript").json()
        assert body["available"] is False and body["reason"] == "no_transcript"
        assert body["entries"] == []

    def test_copilot_row_reads_its_event_log(
        self, webapp_client, _bypass_gate, monkeypatch
    ):
        """A Copilot session has no state row either — its hooks do not fire
        in the interactive TUI — so the route correlates its event log from
        the filesystem and reports its own source name rather than falling
        through to Codex's."""
        from app.webapp.routers import session_transcript as router_mod
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = [_live(agent="copilot")]
        monkeypatch.setattr(
            router_mod, "find_copilot_transcript", lambda session: COPILOT_CAPTURE,
        )
        body = client.get("/api/claude-code/sessions/s1/transcript").json()
        assert body["available"] is True and body["source"] == "copilot"
        # Copilot cannot mark every failure, and the page says so (#1020).
        assert body["tool_errors"] == "partial"
        assert [e["kind"] for e in body["entries"]][:3] == ["system", "user", "tool_call"]
        # The no-tools reply — the capture's last assistant belongs to
        # #1020's failing-view exchange, which is one short line.
        entry = [e for e in body["entries"] if e["kind"] == "assistant"][2]
        full = client.get(
            f"/api/claude-code/sessions/s1/transcript/entry?offset={entry['offset']}"
        ).json()
        assert full["available"] is True
        assert full["text"].startswith("Bounded file reading keeps log viewers")
        # An ambiguous correlation (two sessions launched in one folder inside
        # the window) is "no transcript", never a neighbour's conversation.
        monkeypatch.setattr(router_mod, "find_copilot_transcript", lambda session: None)
        assert client.get(
            "/api/claude-code/sessions/s1/transcript"
        ).json()["reason"] == "no_transcript"

    def test_antigravity_row_reads_its_conversation_log(
        self, webapp_client, _bypass_gate, monkeypatch
    ):
        """An Antigravity session has no state row at all — the fleet's
        `session_state` hook does not reach it — so the route resolves its
        log from the filesystem and reports its own source name."""
        from app.webapp.routers import session_transcript as router_mod
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = [_live(agent="antigravity")]
        monkeypatch.setattr(
            router_mod, "find_antigravity_transcript",
            lambda session, live: AGY_CAPTURE,
        )
        body = client.get("/api/claude-code/sessions/s1/transcript").json()
        assert body["available"] is True and body["source"] == "antigravity"
        assert [e["kind"] for e in body["entries"]][:3] == ["user", "thinking", "tool_call"]
        entry = [e for e in body["entries"] if e["kind"] == "assistant"][0]
        full = client.get(
            f"/api/claude-code/sessions/s1/transcript/entry?offset={entry['offset']}"
        ).json()
        assert full["available"] is True
        assert full["text"].startswith("Based on the directory listing")
        # Correlation refused (a second live session in the folder, or a
        # session that has not prompted yet) is "no transcript", never a
        # neighbour's conversation.
        monkeypatch.setattr(
            router_mod, "find_antigravity_transcript", lambda session, live: None
        )
        assert client.get(
            "/api/claude-code/sessions/s1/transcript"
        ).json()["reason"] == "no_transcript"

    # `ssh` on purpose: it is the one registered agent that is not a coding
    # harness and so will never have a history file. Every coding agent the
    # launcher hosts now has a reader (#1015 was the last), so picking one of
    # them here would mean repointing these two tests again, as #1013 had to
    # when Pi stopped being unsupported.
    def test_detached_unsupported_agent(self, webapp_client, _bypass_gate):
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = [_live(kind="remote", agent="ssh")]
        assert client.get("/api/claude-code/sessions/s1/transcript").json()["reason"] == "unsupported_agent"

    def test_unsupported_agent(self, webapp_client, _bypass_gate):
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = [_live(agent="ssh")]
        assert client.get("/api/claude-code/sessions/s1/transcript").json()["reason"] == "unsupported_agent"

    def test_no_transcript_when_no_row_or_no_file(self, webapp_client, _bypass_gate, monkeypatch, tmp_path):
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = [_live()]
        monkeypatch.setattr(board, "state_row_for_session", lambda live, rows, sid: None)
        assert client.get("/api/claude-code/sessions/s1/transcript").json()["reason"] == "no_transcript"
        monkeypatch.setattr(
            board, "state_row_for_session",
            lambda live, rows, sid: {"transcript_path": str(tmp_path / "gone.jsonl")},
        )
        assert client.get("/api/claude-code/sessions/s1/transcript").json()["reason"] == "no_transcript"

    @pytest.mark.parametrize(
        ("failing_fn", "url"),
        [
            ("transcript_page", "/api/claude-code/sessions/s1/transcript"),
            ("entry_full_text", "/api/claude-code/sessions/s1/transcript/entry?offset=0"),
        ],
        ids=["page", "entry"],
    )
    def test_read_failure_is_distinct_from_missing(
        self, webapp_client, _bypass_gate, monkeypatch, tmp_path, failing_fn, url
    ):
        from app.webapp.routers import session_transcript as router_mod
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = [_live()]
        path = _write_jsonl(tmp_path / "t.jsonl", [_user("hi")])
        monkeypatch.setattr(board, "state_row_for_session", lambda live, rows, sid: {"transcript_path": str(path)})

        def boom(*args, **kwargs):
            raise PermissionError("locked")

        monkeypatch.setattr(router_mod, failing_fn, boom)
        body = client.get(url).json()
        assert body["available"] is False and body["reason"] == "read_failed"

    def test_happy_path_pages_and_never_logs_bodies(
        self, webapp_client, _bypass_gate, monkeypatch, tmp_path, caplog
    ):
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = [_live()]
        path = _write_jsonl(tmp_path / "t.jsonl", _conversation(6, tool_calls_per_turn=1))
        monkeypatch.setattr(board, "state_row_for_session", lambda live, rows, sid: {"transcript_path": str(path)})

        with caplog.at_level(logging.INFO):
            first = client.get("/api/claude-code/sessions/s1/transcript?limit=4").json()
        assert first["available"] is True and first["source"] == "native"
        assert first["session_id"] == "s1" and first["reason"] is None
        assert [e["text"] for e in first["entries"] if e["kind"] == "user"] == ["prompt 4 pppp", "prompt 5 ppppp"]
        assert isinstance(first["next_cursor"], int)
        older = client.get(
            f"/api/claude-code/sessions/s1/transcript?limit=4&before={first['next_cursor']}"
        ).json()
        assert [e["text"] for e in older["entries"] if e["kind"] == "user"] == ["prompt 2 pp", "prompt 3 ppp"]
        joined = "\n".join(r.getMessage() for r in caplog.records)
        assert "prompt 5" not in joined and "reply 5" not in joined
        assert "transcript s1" in joined  # the breadcrumb itself is there

    def test_codex_source(self, webapp_client, _bypass_gate, monkeypatch, tmp_path):
        from app.webapp.routers import session_transcript as router_mod
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = [_live(agent="codex")]
        path = _write_jsonl(tmp_path / "rollout.jsonl", [
            _codex({"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hey"}]}),
            _codex({"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "yo"}]}),
        ])
        monkeypatch.setattr(router_mod, "find_codex_transcript", lambda session: path)
        body = client.get("/api/claude-code/sessions/s1/transcript").json()
        assert body["source"] == "codex" and _kinds(body["entries"]) == ["user", "assistant"]

    def test_limit_is_validated(self, webapp_client, _bypass_gate, monkeypatch):
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = [_live()]
        assert client.get("/api/claude-code/sessions/s1/transcript?limit=0").status_code == 422
        assert client.get("/api/claude-code/sessions/s1/transcript?limit=101").status_code == 422
        assert client.get("/api/claude-code/sessions/s1/transcript?before=-1").status_code == 422


def test_transcript_entry_path_classified_passkey():
    """#985: a distinct guard-table row — '.../transcript/entry' does not
    end with '/transcript', so the sibling route's rule does not cover it
    for free (the exact omission class #997 found on the send route)."""
    from app.webapp.middleware import _terminal_guard_level
    assert _terminal_guard_level("/api/claude-code/sessions/abc/transcript/entry") == "passkey"
    assert _terminal_guard_level("/api/claude-code/sessions/abc/transcript") == "passkey"


def test_transcript_entry_refused_off_tailnet(webapp_client):
    client, _, _ = webapp_client
    assert client.get("/api/claude-code/sessions/s1/transcript/entry?offset=0").status_code == 403


class TestTranscriptEntryEndpoint:

    def test_offset_is_required(self, webapp_client, _bypass_gate):
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = [_live()]
        assert client.get("/api/claude-code/sessions/s1/transcript/entry").status_code == 422
        assert client.get("/api/claude-code/sessions/s1/transcript/entry?offset=-1").status_code == 422

    def test_unknown_session_shares_the_page_routes_reasons(self, webapp_client, _bypass_gate):
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = []
        body = client.get("/api/claude-code/sessions/s1/transcript/entry?offset=0").json()
        assert body["available"] is False and body["reason"] == "session_not_found"

    def test_happy_path_returns_uncapped_text_and_never_logs_body(
        self, webapp_client, _bypass_gate, monkeypatch, tmp_path, caplog
    ):
        monkeypatch.setattr(tf_shared, "USER_TEXT_CAP", 5)
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = [_live()]
        path = _write_jsonl(tmp_path / "t.jsonl", [_user("a much longer prompt than the cap")])
        monkeypatch.setattr(board, "state_row_for_session", lambda live, rows, sid: {"transcript_path": str(path)})
        page = client.get("/api/claude-code/sessions/s1/transcript").json()
        entry = page["entries"][0]
        assert entry["truncated"] is True

        with caplog.at_level(logging.INFO):
            body = client.get(
                f"/api/claude-code/sessions/s1/transcript/entry?offset={entry['offset']}"
            ).json()
        assert body == {
            "available": True, "reason": None, "session_id": "s1",
            "text": "a much longer prompt than the cap", "truncated": False,
        }
        joined = "\n".join(r.getMessage() for r in caplog.records)
        assert "a much longer prompt" not in joined
        assert "transcript entry s1" in joined

    def test_stale_offset_is_entry_not_found(self, webapp_client, _bypass_gate, monkeypatch, tmp_path):
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = [_live()]
        path = _write_jsonl(tmp_path / "t.jsonl", [_user("hi")])
        monkeypatch.setattr(board, "state_row_for_session", lambda live, rows, sid: {"transcript_path": str(path)})
        body = client.get("/api/claude-code/sessions/s1/transcript/entry?offset=9999").json()
        assert body["available"] is False and body["reason"] == "entry_not_found"
