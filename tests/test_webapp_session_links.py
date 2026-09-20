"""Provider-native web links on the session payloads (issues #879, #1096).

``provider_web_url``/``attach_provider_web_urls`` moved out of
``routers/sessions.py`` into ``routers/_helpers.py`` in #1096, so the Board
router can enrich its cards with the same ``web_url`` without one router
importing another (``_helpers``'s stated job).
"""

import pytest

from app.webapp.routers import _helpers
from app.webapp.routers._helpers import attach_provider_web_urls, provider_web_url

_WRAPPED_CARD = (
    "before\x1b[1Bhttps://claude.ai/code/session_\r\n"
    "\x1b[3G011QSPhSiZdi9GB8skTjx16P\x1b[K after"
)
_LINK = "https://claude.ai/code/session_011QSPhSiZdi9GB8skTjx16P"


@pytest.fixture(autouse=True)
def _clear_provider_url_cache():
    """The memo is module state — no test may inherit another's entries."""
    _helpers._PROVIDER_URL_CACHE.clear()
    yield
    _helpers._PROVIDER_URL_CACHE.clear()


def test_claude_web_url_is_recovered_from_wrapped_terminal_output(tmp_path):
    transcript = tmp_path / "claude.transcript"
    transcript.write_text(_WRAPPED_CARD, encoding="utf-8")

    assert provider_web_url("claude", transcript) == _LINK


def test_codex_has_no_web_session_url(tmp_path):
    transcript = tmp_path / "codex.transcript"
    transcript.write_text(
        "thread id 01a0819b-c4f9-70a3-83df-72445d31a231",
        encoding="utf-8",
    )

    assert provider_web_url("codex", transcript) == ""


def _stub_transcripts(monkeypatch, tmp_path, bodies):
    """Point ``audit.transcript_path`` at a per-session file under tmp_path."""
    for sid, body in bodies.items():
        (tmp_path / f"{sid}.transcript").write_text(body, encoding="utf-8")
    monkeypatch.setattr(
        _helpers.audit, "transcript_path", lambda sid: tmp_path / f"{sid}.transcript"
    )


def test_attach_sets_web_url_on_every_row_and_scans_only_pty(monkeypatch, tmp_path):
    """Non-PTY rows still get the key — the client must never see it absent."""
    _stub_transcripts(monkeypatch, tmp_path, {"s-pty": _WRAPPED_CARD})
    rows = [
        {"session_id": "s-pty", "kind": "pty", "agent": "claude"},
        {"session_id": "s-remote", "kind": "remote", "agent": "claude"},
        # A Board state-only card: no session id at all (#1096 sends these
        # through the same call, so the None must not reach transcript_path).
        {"session_id": None, "kind": "external", "agent": "claude"},
    ]

    attach_provider_web_urls(rows)

    assert [r["web_url"] for r in rows] == [_LINK, "", ""]


def test_a_captured_link_is_memoized_but_an_empty_one_is_rescanned(
    monkeypatch, tmp_path
):
    """The Coding and Board polls share a 5s cadence and overlap.

    A hit must not be re-scanned; a miss must be, or a session whose
    remote-control card has simply not printed yet would be pinned to '' for
    the life of the process.
    """
    _stub_transcripts(
        monkeypatch, tmp_path, {"s-hit": _WRAPPED_CARD, "s-miss": "no card yet"}
    )
    scanned = []
    real = _helpers.provider_web_url

    def _counting(agent, transcript):
        scanned.append(transcript.name)
        return real(agent, transcript)

    monkeypatch.setattr(_helpers, "provider_web_url", _counting)

    rows = [
        {"session_id": "s-hit", "kind": "pty", "agent": "claude"},
        {"session_id": "s-miss", "kind": "pty", "agent": "claude"},
    ]
    attach_provider_web_urls(rows)
    assert [r["web_url"] for r in rows] == [_LINK, ""]
    assert scanned == ["s-hit.transcript", "s-miss.transcript"]

    second = [dict(r) for r in rows]
    attach_provider_web_urls(second)
    assert [r["web_url"] for r in second] == [_LINK, ""]
    # s-hit served from the memo; s-miss scanned again.
    assert scanned == [
        "s-hit.transcript", "s-miss.transcript", "s-miss.transcript",
    ]


async def test_board_cards_carry_the_same_provider_link_as_the_coding_tab(
    monkeypatch, tmp_path
):
    """#1096: the Board payload never called this, so the drawer's Rename said
    *Not available yet* for a live Claude PTY session the Coding tab linked
    fine — the dialog is shared, and it reads ``web_url``.

    Drives ``get_board`` directly with every other input stubbed, the same
    shape as ``test_webapp_board_merge_nonblocking.py``.
    """
    from types import SimpleNamespace

    from app.webapp.routers import board as board_router

    _stub_transcripts(monkeypatch, tmp_path, {"s-board": _WRAPPED_CARD})
    card = {
        "session_id": "s-board", "kind": "pty", "agent": "claude",
        "project": "proj", "status": "working", "age_seconds": 5, "alive": True,
    }
    empty = {"available": False, "stale": False, "updated_at": None, "rows": {}}
    monkeypatch.setattr(board_router.board, "merge_sessions", lambda *a, **k: [card])
    monkeypatch.setattr(board_router, "_read_live_sessions", lambda port: ([], None))
    monkeypatch.setattr(board_router.board, "read_sessions_state", lambda p: empty)
    monkeypatch.setattr(board_router.board, "read_active_issues", lambda p: empty)
    monkeypatch.setattr(board_router.board, "jobs_attention", lambda: [])
    monkeypatch.setattr(board_router, "_read_quota_lines", lambda cfg: [])
    monkeypatch.setattr(
        board_router, "_refresh_codex_for_lines", lambda cfg, lines: None
    )
    monkeypatch.setattr(
        board_router.board_chief, "_reconcile_chief_labels", lambda live, rows: live
    )
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                webapp_config=SimpleNamespace(
                    sessions_state_file=str(tmp_path / "sessions-state.json"),
                    session_host_port=8446,
                    claude_config_dir=str(tmp_path / "fleet-config"),
                )
            )
        )
    )

    body = await board_router.get_board(request)

    on_board = [
        c
        for column in body["columns"].values()
        for c in column
        if c.get("session_id") == "s-board"
    ]
    assert on_board, f"the session card never reached a column: {body['columns']}"
    assert on_board[0]["web_url"] == _LINK


def test_the_memo_is_bounded_over_a_long_lived_tray(monkeypatch, tmp_path):
    """Dead session ids accumulate; the cap keeps that from growing forever."""
    monkeypatch.setattr(_helpers, "_PROVIDER_URL_CACHE_MAX", 4)
    bodies = {f"s{i}": _WRAPPED_CARD for i in range(6)}
    _stub_transcripts(monkeypatch, tmp_path, bodies)

    for sid in bodies:
        rows = [{"session_id": sid, "kind": "pty", "agent": "claude"}]
        attach_provider_web_urls(rows)
        # Every row still resolves, whatever the cache did on the way.
        assert rows[0]["web_url"] == _LINK

    assert len(_helpers._PROVIDER_URL_CACHE) <= 4
