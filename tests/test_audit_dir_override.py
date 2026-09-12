"""`LAUNCHER_AUDIT_DIR` relocates the per-session audit files (issue #913).

Before it, `src/audit.py` resolved its directory from `__file__` alone, so the
disposable webapp + session-host the e2e gate spawns — both run from this very
checkout — appended their throwaway `<sid>.log` / `<sid>.transcript` pairs to
the live `webapp/sessions` the phone reads. PR #911 had already moved the
disposable *config* into pytest's temp tree; the session files never followed.

These pin the override itself. The harness half — that the autoboot fixture
actually injects the variable into both spawns — is pinned by the isolation
breach check in `tests/e2e/conftest.py::_autoboot_server`'s teardown.
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path
from typing import Iterator

import pytest

from src import audit as _audit


@pytest.fixture
def audit_module() -> Iterator:
    """Yield `src.audit`, and restore its real module state afterwards.

    The directory is resolved once at import (the processes that need the
    override are given it at spawn time), so exercising it means reloading —
    and `src.audit` is global, so a test that leaves it reloaded against a
    doctored environment would redirect every later test in the run. The
    teardown puts the variable back *itself* before reloading rather than
    relying on monkeypatch having already unwound: fixture and monkeypatch
    teardown order is not guaranteed, and getting it backwards leaves the
    module pointing into the checkout — the exact thing #913 is about.
    """
    original = os.environ.get(_audit.AUDIT_DIR_ENV)
    try:
        yield _audit
    finally:
        if original is None:
            os.environ.pop(_audit.AUDIT_DIR_ENV, None)
        else:
            os.environ[_audit.AUDIT_DIR_ENV] = original
        importlib.reload(_audit)


def test_defaults_to_the_checkout_webapp_dir(
    audit_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(_audit.AUDIT_DIR_ENV, raising=False)
    mod = importlib.reload(audit_module)

    expected = mod.PROJECT_ROOT / "webapp" / "sessions"
    assert mod.sessions_dir() == expected
    assert mod.transcript_path("abc") == expected / "abc.transcript"
    assert mod.session_log_path("abc") == expected / "abc.log"


def test_env_relocates_sessions_and_the_cross_session_log(
    audit_module, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(_audit.AUDIT_DIR_ENV, str(tmp_path))
    mod = importlib.reload(audit_module)

    assert mod.sessions_dir() == tmp_path / "sessions"
    assert mod.transcript_path("abc") == tmp_path / "sessions" / "abc.transcript"
    assert mod.session_log_path("abc") == tmp_path / "sessions" / "abc.log"
    # The cross-session audit log moves with it — a test run's passkey and
    # lifecycle events have no business in the real terminal_audit.log either.
    assert mod._AUDIT_LOG == tmp_path / "terminal_audit.log"


def test_a_write_lands_in_the_override_and_not_the_real_dir(
    audit_module, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    real_sessions = _audit.PROJECT_ROOT / "webapp" / "sessions"
    sid = "issue913-override-probe"

    monkeypatch.setenv(_audit.AUDIT_DIR_ENV, str(tmp_path))
    mod = importlib.reload(audit_module)
    mod.session_log(sid, "ws_open", client="127.0.0.1")
    mod.session_input(sid, "hello")

    written = tmp_path / "sessions" / f"{sid}.log"
    assert written.is_file(), "the override directory got no session log"
    body = written.read_text(encoding="utf-8")
    assert "[ws_open]" in body and "hello" in body
    # The whole point: nothing reached the checkout's live directory.
    assert not (real_sessions / f"{sid}.log").exists()


def test_blank_env_falls_back_rather_than_writing_to_the_filesystem_root(
    audit_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty or whitespace value is "unset", not `Path("")` (= CWD)."""
    monkeypatch.setenv(_audit.AUDIT_DIR_ENV, "   ")
    mod = importlib.reload(audit_module)

    assert mod.sessions_dir() == mod.PROJECT_ROOT / "webapp" / "sessions"


def test_the_suite_itself_never_writes_sessions_into_the_checkout() -> None:
    """The other half of #913: the in-process real-PTY tests.

    `tests/conftest.py` redirects `LAUNCHER_AUDIT_DIR` before `src.audit` is
    imported, so the `claude`/`codex` submit probes and the #64 readback — all
    of which drive a genuine ConPTY and let `src.session_host` write a real
    transcript — land outside the checkout. Without that redirect this suite
    leaves `<id>.transcript` files in the very directory the phone reads;
    measured at 5 per gate run before the fix. The browser half is covered by
    the autoboot breach check, which cannot see these: they never leave the
    pytest process.
    """
    mod = importlib.reload(_audit)  # resolve against the live environment
    sessions = mod.sessions_dir().resolve()
    checkout = mod.PROJECT_ROOT.resolve()
    assert sessions != checkout and checkout not in sessions.parents, (
        f"the test suite resolves its session dir to {sessions}, inside the "
        f"checkout at {checkout} — tests/conftest.py must set "
        f"{mod.AUDIT_DIR_ENV} before src.audit is imported (issue #913)"
    )
