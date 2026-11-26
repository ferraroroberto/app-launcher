"""Guard: concerns that have drifted into a second copy before keep one owner.

A `/codebase-audit` duplication bucket flagged, two audits running, the "read
JSON; missing file is empty, unreadable warns and is empty" block hand-rolled
in dozens of ``src/`` modules beside ``src/_json_io.py``'s atomic writer, and
two functions in ``src/diagnostics.py`` answering "which PIDs LISTEN on this
port" with different error semantics.

A later bucket flagged the same class again: a forked copy of the vendored poll
helper, a hand-written poll loop beside it, and a second run-progress tracker.

Each reinvention has a tell a reader can scan for, so the next copy fails the
gate instead of waiting for the next audit.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
_PRODUCT_DIRS = (_ROOT / "src", _ROOT / "app")

# The reader itself, plus the readers whose "missing" and "unreadable" are
# different answers (they raise, or return a distinct unreadable state), which
# ``read_json_or`` deliberately does not express.
_HAND_ROLLED_READER_OK = {
    "src/_json_io.py",
    "src/chief_session_end.py",
    "src/desktop/lid_keep_awake.py",
    "src/inspiration/ideas.py",
}

_JSON_READ_THEN_MISSING_RE = re.compile(
    r"json\.loads\([^\n]*\.read_text\([^\n]*\n(?:[^\n]*\n){0,1}\s*except FileNotFoundError"
)

_LISTENER_OWNER = "src/diagnostics.py"

_STATIC = _ROOT / "app" / "webapp" / "static"
_JS_EXPORT_RE = re.compile(r"^export\s+(?:async\s+)?function\s+(\w+)", re.MULTILINE)
_TIMER_POLL_RE = re.compile(r"setTimeout\([^;]*POLL[A-Z_]*_MS")
# Loops that predate this gate. The set only shrinks.
_TIMER_POLL_OK = {"channel-sessions.js", "progress/progress-sheet.js", "settings/inspiration.js"}
_RUN_TRACKER_OWNER = "src/progress/run_tracker.py"


def _product_files() -> list[tuple[str, str]]:
    return [
        (path.relative_to(_ROOT).as_posix(), path.read_text(encoding="utf-8"))
        for base in _PRODUCT_DIRS
        for path in sorted(base.rglob("*.py"))
    ]


def test_tolerant_json_reads_go_through_read_json_or() -> None:
    offenders = [
        rel
        for rel, code in _product_files()
        if rel not in _HAND_ROLLED_READER_OK and _JSON_READ_THEN_MISSING_RE.search(code)
    ]
    assert not offenders, (
        "a hand-rolled 'read JSON, missing file is the default' block; call "
        "src._json_io.read_json_or instead:\n  " + "\n  ".join(offenders)
    )


def test_listening_port_lookup_stays_in_diagnostics() -> None:
    offenders = [
        rel
        for rel, code in _product_files()
        if rel != _LISTENER_OWNER and "CONN_LISTEN" in code
    ]
    assert not offenders, (
        "a second 'who LISTENs on this port' scan; call "
        f"{_LISTENER_OWNER}'s listener_pids instead:\n  " + "\n  ".join(offenders)
    )


def _static_js(vendored: bool) -> list[tuple[str, str]]:
    return [
        (path.relative_to(_STATIC).as_posix(), path.read_text(encoding="utf-8"))
        for path in sorted(_STATIC.rglob("*.js"))
        if (path.relative_to(_STATIC).parts[0] == "_vendored") is vendored
        and path.relative_to(_STATIC).parts[0] != "vendor"
    ]


def test_no_app_module_forks_a_vendored_helper() -> None:
    owned = {name for _, code in _static_js(True) for name in _JS_EXPORT_RE.findall(code)}
    offenders = [
        f"{rel}: {name}"
        for rel, code in _static_js(False)
        for name in _JS_EXPORT_RE.findall(code)
        if name in owned
    ]
    assert "startPoll" in owned
    assert not offenders, f"a second export of a _vendored helper; import it: {offenders}"


def test_page_polls_are_not_hand_rolled_on_set_timeout() -> None:
    offenders = [
        rel for rel, code in _static_js(False) if rel not in _TIMER_POLL_OK and _TIMER_POLL_RE.search(code)
    ]
    assert not offenders, f"a hand-rolled poll loop; use _vendored/poll startPoll: {offenders}"


def test_watched_run_progress_has_one_tracker() -> None:
    offenders = [
        rel
        for rel, code in _product_files()
        if rel != _RUN_TRACKER_OWNER and '"current": None' in code and '"finished_at": None' in code
    ]
    assert not offenders, f"a second run tracker; drive {_RUN_TRACKER_OWNER}: {offenders}"


def test_read_json_or_separates_missing_corrupt_and_wrong_shape(tmp_path, caplog) -> None:
    from src._json_io import read_json_or

    assert read_json_or(tmp_path / "none.json", {"d": 1}, "x") == {"d": 1}
    assert not caplog.records

    good = tmp_path / "good.json"
    good.write_text('{"a": 1}', encoding="utf-8")
    assert read_json_or(good, {}, "x", expect=dict) == {"a": 1}
    assert read_json_or(good, [], "x", expect=list) == []

    bad = tmp_path / "bad.json"
    bad.write_text("{nope", encoding="utf-8")
    assert read_json_or(bad, {}, "thing", expect=dict) == {}
    assert "thing unreadable" in caplog.text


def test_kill_route_reports_an_unreadable_socket_table(monkeypatch) -> None:
    import asyncio

    import pytest
    from fastapi import HTTPException

    from app.webapp.routers import misc

    monkeypatch.setattr(misc, "listener_pids", lambda port: None)
    with pytest.raises(HTTPException) as caught:
        asyncio.run(misc.kill_port(8445))
    assert caught.value.status_code == 503
