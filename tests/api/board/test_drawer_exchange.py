"""GET /api/board/sessions/{sid}/exchange: agent-aware native history, the launcher capture fallback and its render cache."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src import board_exchange

from tests.api.board._support import (
    NOW,
    _assistant_line,
    _iso,
    _live_sess,
    _user_line,
    _write_jsonl,
)


# -------------------------------------------------------- exchange endpoint


class TestExchangeEndpoint:

    def test_resolves_transcript_via_state_row(
        self, webapp_client, _bypass_gate, tmp_path: Path
    ):
        client, app, overrides = webapp_client
        transcript = _write_jsonl(tmp_path / "t.jsonl", [
            _user_line("status?"),
            _assistant_line([{"type": "text", "text": "All green."}]),
        ])
        state_file = Path(app.state.webapp_config.sessions_state_file)
        state_file.write_text(json.dumps({
            "t-uuid": {"cwd": "E:/proj/app", "status": "needs-you",
                       "updated_at": _iso(NOW), "transcript_path": transcript},
        }), encoding="utf-8")
        overrides["session"].list_sessions.return_value = [
            _live_sess("sess1", "E:/proj/app", 10)
        ]
        body = client.get("/api/board/sessions/sess1/exchange").json()
        assert body["available"] is True
        assert body["assistant"]["text"] == "All green."
        assert body["user"]["text"] == "status?"

    def test_unreachable_session_host_is_not_session_ended(
        self, webapp_client, _bypass_gate
    ):
        """#1308: an unreadable list must not say "Session ended"."""
        from src.session_client import SessionHostError

        client, _, overrides = webapp_client
        overrides["session"].list_sessions.side_effect = SessionHostError("down")
        body = client.get("/api/board/sessions/ghost/exchange").json()
        assert body["available"] is False
        assert body["reason"] == "session_host_unreachable"

    def test_unknown_session_degrades(self, webapp_client, _bypass_gate):
        client, _, _ = webapp_client
        body = client.get("/api/board/sessions/ghost/exchange").json()
        assert body["available"] is False
        assert body["reason"] == "session_not_found"

    @pytest.mark.parametrize("agent, with_missing_native", [
        ("claude", True),
        ("codex", False),
    ])
    def test_launcher_capture_falls_back_when_native_exchange_is_unavailable(
        self, webapp_client, _bypass_gate, tmp_path: Path, monkeypatch,
        agent: str, with_missing_native: bool,
    ):
        """#457 repro: both a missing Claude hook transcript and a Codex
        session with no hook row still have an exact-id launcher capture."""
        from app.webapp.routers import board as board_router

        client, app, overrides = webapp_client
        capture = tmp_path / "sess1.transcript"
        capture.write_text(
            "\x1b[39m\u2022 The exact launcher capture has the latest reply.\r\n"
            "  It remains linked by session id.\r\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(
            board_router.audit, "transcript_path", lambda _sid: capture
        )

        live = _live_sess("sess1", "E:/proj/app", 10)
        live.update(agent=agent, prompt_title="show the latest exchange")
        overrides["session"].list_sessions.return_value = [live]

        if with_missing_native:
            state_file = Path(app.state.webapp_config.sessions_state_file)
            state_file.write_text(json.dumps({
                "t-uuid": {
                    "agent": "claude", "cwd": "E:/proj/app",
                    "status": "needs-you", "updated_at": _iso(NOW),
                    "transcript_path": str(tmp_path / "missing.jsonl"),
                },
            }), encoding="utf-8")

        body = client.get("/api/board/sessions/sess1/exchange").json()
        assert body["available"] is True
        assert body["source"] == "launcher"
        assert body["user"]["text"] == "show the latest exchange"
        assert body["assistant"]["text"] == (
            "The exact launcher capture has the latest reply. "
            "It remains linked by session id."
        )


# ------------------------------------------ rendered-rows cache (#1494)


def _count_renders(monkeypatch) -> list:
    """Every ``pyte`` replay ``launcher_last_exchange`` performs, recorded."""
    calls: list = []
    real = board_exchange._terminal_rows

    def counting(raw, **kwargs):
        calls.append(raw)
        return real(raw, **kwargs)

    monkeypatch.setattr(board_exchange, "_terminal_rows", counting)
    return calls


def test_unchanged_capture_is_not_rendered_twice(tmp_path: Path, monkeypatch):
    capture = tmp_path / "idle.transcript"
    capture.write_text("● First reply.\r\n", encoding="utf-8")
    renders = _count_renders(monkeypatch)
    first = board_exchange.launcher_last_exchange(capture, rows=20, cols=80)
    second = board_exchange.launcher_last_exchange(capture, rows=20, cols=80)
    assert len(renders) == 1
    assert second == first
    assert second["assistant"]["text"] == "First reply."


def test_changed_capture_is_never_served_from_the_cache(
    tmp_path: Path, monkeypatch,
):
    capture = tmp_path / "busy.transcript"
    capture.write_text("● First reply.\r\n", encoding="utf-8")
    renders = _count_renders(monkeypatch)
    assert board_exchange.launcher_last_exchange(
        capture, rows=20, cols=80
    )["assistant"]["text"] == "First reply."
    with open(capture, "a", encoding="utf-8") as fh:
        fh.write("● Second reply.\r\n")
    assert board_exchange.launcher_last_exchange(
        capture, rows=20, cols=80
    )["assistant"]["text"] == "Second reply."
    assert len(renders) == 2


def test_same_size_rewrite_is_never_served_from_the_cache(
    tmp_path: Path, monkeypatch,
):
    """A tail replaced by text of the same length differs only in mtime."""
    capture = tmp_path / "rewrite.transcript"
    capture.write_text("● Reply AAAA.\r\n", encoding="utf-8")
    renders = _count_renders(monkeypatch)
    board_exchange.launcher_last_exchange(capture, rows=20, cols=80)
    stat = capture.stat()
    capture.write_text("● Reply BBBB.\r\n", encoding="utf-8")
    import os
    os.utime(capture, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    result = board_exchange.launcher_last_exchange(capture, rows=20, cols=80)
    assert result["assistant"]["text"] == "Reply BBBB."
    assert len(renders) == 2


def test_a_resized_screen_is_rendered_again(tmp_path: Path, monkeypatch):
    capture = tmp_path / "resize.transcript"
    capture.write_text("● A reply.\r\n", encoding="utf-8")
    renders = _count_renders(monkeypatch)
    board_exchange.launcher_last_exchange(capture, rows=20, cols=80)
    board_exchange.launcher_last_exchange(capture, rows=20, cols=100)
    assert len(renders) == 2


def test_rows_cache_is_bounded(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(board_exchange, "_ROWS_CACHE_MAX", 3)
    monkeypatch.setattr(board_exchange, "_ROWS_CACHE", {})
    for i in range(6):
        capture = tmp_path / f"s{i}.transcript"
        capture.write_text("● A reply.\r\n", encoding="utf-8")
        board_exchange.launcher_last_exchange(capture, rows=20, cols=80)
    assert len(board_exchange._ROWS_CACHE) == 3
