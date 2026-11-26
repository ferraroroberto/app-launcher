"""Provider-native web links on the session payloads (issues #879, #1096).

``provider_web_url``/``attach_provider_web_urls`` moved out of
``routers/sessions.py`` into ``routers/_helpers.py`` in #1096, so the Board
router can enrich its cards with the same ``web_url`` without one router
importing another (``_helpers``'s stated job).
"""

import pytest

from app.webapp.routers import _helpers
from app.webapp.routers._helpers import attach_provider_web_urls, provider_web_url

# Synthetic session token, never a captured one: `_CLAUDE_WEB_URL_RE`
# only needs 24 alphanumerics, so a patterned body drives the same parse
# while reading as fake at a glance (#1106).
_WRAPPED_CARD = (
    "before\x1b[1Bhttps://claude.ai/code/session_\r\n"
    "\x1b[3GSYNTHETICLINKFIXTURE0879\x1b[K after"
)
_LINK = "https://claude.ai/code/session_SYNTHETICLINKFIXTURE0879"


@pytest.fixture(autouse=True)
def _clear_provider_url_cache():
    """The memo is module state — no test may inherit another's entries."""
    _helpers._PROVIDER_URL_CACHE.clear()
    _helpers._PROVIDER_URL_MISSES.clear()
    _helpers._COPILOT_IDS.clear()
    yield
    _helpers._PROVIDER_URL_CACHE.clear()
    _helpers._PROVIDER_URL_MISSES.clear()
    _helpers._COPILOT_IDS.clear()


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


def test_a_captured_link_is_memoized_and_a_miss_waits_for_the_file_to_change(
    monkeypatch, tmp_path
):
    """The Coding and Board polls share a 5s cadence and overlap.

    A hit must not be re-scanned. A miss is not re-scanned while the
    transcript is unchanged (#1605: a long-lived session with no card in
    either window re-opened its whole transcript every poll), but is once it
    grows, or a session whose remote-control card has simply not printed yet
    would be pinned to '' for the life of the process.
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
    # s-hit served from the memo; s-miss's transcript is unchanged, so not reopened.
    assert scanned == ["s-hit.transcript", "s-miss.transcript"]

    with (tmp_path / "s-miss.transcript").open("a", encoding="utf-8") as grown:
        grown.write(_WRAPPED_CARD)
    third = [dict(r) for r in rows]
    attach_provider_web_urls(third)
    assert [r["web_url"] for r in third] == [_LINK, _LINK]
    assert scanned == ["s-hit.transcript", "s-miss.transcript", "s-miss.transcript"]


def test_an_unreadable_transcript_caches_no_miss(monkeypatch, tmp_path):
    """No stat, no identity: nothing is cached, so the next poll tries again."""
    _stub_transcripts(monkeypatch, tmp_path, {})
    rows = [{"session_id": "s-gone", "kind": "pty", "agent": "claude"}]
    attach_provider_web_urls(rows)
    assert rows[0]["web_url"] == ""
    assert "s-gone" not in _helpers._PROVIDER_URL_MISSES


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
    monkeypatch.setattr(board_router, "read_live_sessions", lambda port: ([], None))
    monkeypatch.setattr(board_router.board, "read_sessions_state", lambda p: empty)
    monkeypatch.setattr(board_router.board, "read_active_issues", lambda p: empty)
    monkeypatch.setattr(board_router.board, "jobs_attention", lambda: [])
    monkeypatch.setattr(
        board_router.board_chief, "_reconcile_chief_labels", lambda live, rows: live
    )
    request = SimpleNamespace(
        headers={},
        app=SimpleNamespace(
            state=SimpleNamespace(
                webapp_config=SimpleNamespace(
                    sessions_state_file=str(tmp_path / "sessions-state.json"),
                    session_host_port=8446,
                    fleet_config_dir=str(tmp_path / "fleet-config"),
                    projects_dir="",
                    chief_plan_file=str(tmp_path / "chief-plan.json"),
                    copilot_web_url_prefix="",
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


# Placeholder host and synthetic id: the real host is the machine's own
# Copilot sign-in and never appears in the repo.
_GHE_HOST = "ghe.example.com"
_COPILOT_PREFIX = f"https://{_GHE_HOST}/copilot/tasks/"
_COPILOT_ID = "79c18133-52bd-4f8f-9260-b33c853158be"
# 2026-09-17T21:57:30Z; Copilot's folder is stamped 4 s later.
_COPILOT_STARTED = 1_789_682_250.0


def _copilot_folder(root, folder, created="2026-09-17T21:57:34Z"):
    """A Copilot session folder as the CLI writes it at launch: no
    ``events.jsonl`` yet, since that only appears at the first prompt."""
    (root / folder).mkdir()
    (root / folder / "workspace.yaml").write_text(
        f"id: {folder}\ncwd: E:/work/project\ncreated_at: {created}\n",
        encoding="utf-8",
    )


def _copilot_row(sid="s-cop"):
    return {"session_id": sid, "kind": "pty", "agent": "copilot",
            "project_dir": r"E:\work\project", "started_at": _COPILOT_STARTED}


def _cfg(override):
    from types import SimpleNamespace

    return SimpleNamespace(copilot_web_url_prefix=override)


@pytest.fixture
def copilot_env(monkeypatch, tmp_path):
    """Copilot's session-state dir under tmp_path, signed in to ``host``."""
    from src.session_view import transcript_locate

    monkeypatch.setattr(transcript_locate, "_COPILOT_STATE_DIR", tmp_path)
    signed_in = {"host": _GHE_HOST}
    monkeypatch.setattr(_helpers, "copilot_signed_in_host", lambda: signed_in["host"])
    return signed_in


def test_copilot_link_is_its_signed_in_enterprise_host_plus_its_session_id(
    copilot_env, tmp_path
):
    _copilot_folder(tmp_path, _COPILOT_ID)
    rows = [_copilot_row(), {**_copilot_row("s-detached"), "kind": "remote"}]

    attach_provider_web_urls(rows)
    assert [r["web_url"] for r in rows] == [_COPILOT_PREFIX + _COPILOT_ID, ""]

    # The id is cached, not the URL, so the base follows the current sign-in:
    # github.com derives nothing, and the override wins over either.
    copilot_env["host"] = "github.com"
    attach_provider_web_urls(rows)
    assert rows[0]["web_url"] == ""
    attach_provider_web_urls(rows, _cfg("https://ghe.example.org/tasks"))
    assert rows[0]["web_url"] == f"https://ghe.example.org/tasks/{_COPILOT_ID}"
    # An override that is not an http(s) URL is ignored, not glued on.
    copilot_env["host"] = _GHE_HOST
    attach_provider_web_urls(rows, _cfg("ghe.example.org/tasks/"))
    assert rows[0]["web_url"] == _COPILOT_PREFIX + _COPILOT_ID


def test_copilot_with_no_base_never_scans_copilot_sessions(copilot_env, monkeypatch):
    """A github.com sign-in with no override pays nothing for the feature."""
    copilot_env["host"] = "github.com"
    monkeypatch.setattr(
        _helpers, "copilot_session_id",
        lambda s: pytest.fail("scanned Copilot's sessions with no link base"),
    )
    rows = [_copilot_row()]
    attach_provider_web_urls(rows)
    assert rows[0]["web_url"] == ""


def test_a_copilot_miss_is_retried_only_while_the_launch_window_is_open(
    copilot_env, monkeypatch, tmp_path
):
    """Copilot creates its folder a few seconds after launch, so an early miss
    must be retried; once the window has passed no folder can still qualify,
    and the miss is cached instead of re-reading every sidecar each poll."""
    clock = {"now": _COPILOT_STARTED + 1}
    monkeypatch.setattr(_helpers.time, "time", lambda: clock["now"])
    rows = [_copilot_row()]

    attach_provider_web_urls(rows)
    assert rows[0]["web_url"] == "" and "s-cop" not in _helpers._COPILOT_IDS

    _copilot_folder(tmp_path, _COPILOT_ID)
    attach_provider_web_urls(rows)
    assert rows[0]["web_url"] == _COPILOT_PREFIX + _COPILOT_ID

    late = [{**_copilot_row("s-late"), "project_dir": r"E:\work\other"}]
    clock["now"] = _COPILOT_STARTED + 600
    attach_provider_web_urls(late)
    assert _helpers._COPILOT_IDS["s-late"] == ""


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        (f"https://{_GHE_HOST}", _GHE_HOST),
        (f"https://{_GHE_HOST.upper()}/", _GHE_HOST),
        (_GHE_HOST, _GHE_HOST),
        (f"https://{_GHE_HOST}/some/path", None),
        ("https://user@ghe.example.com", None),
    ],
)
def test_the_signed_in_host_is_read_from_copilots_own_config(
    monkeypatch, tmp_path, host, expected
):
    """The CLI records the account as an origin under a block of ``//``
    comment lines; only a bare origin may head a URL."""
    import json

    from src.session_view import copilot_host

    config = tmp_path / "config.json"
    config.write_text(
        "// written by the Copilot CLI\n// do not edit\n"
        + json.dumps({"lastLoggedInUser": {"host": host, "login": "someone"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(copilot_host, "_COPILOT_CONFIG", config)
    monkeypatch.setattr(copilot_host, "_memo", (None, None))
    assert copilot_host.copilot_signed_in_host() == expected
