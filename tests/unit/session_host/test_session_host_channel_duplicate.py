"""One live session per Telegram channel label, enforced by the session-host.

Two webapp processes can ask for the same profile at once, and the webapp's own
lock and already-running check are per-process. The host is the one place both
requests meet, so it refuses the second spawn itself. The slow spawn is injected
(a gate the test opens), never a CPU burn.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.webapp.session_host import server
from src.session_host import SessionManager
from src.session_hosting.channel_claims import DuplicateChannelSession


def _fake_session(label: str, *, alive: bool = True) -> MagicMock:
    session = MagicMock()
    session.label = label
    session.alive = alive
    session.session_id = f"sid-{label}-{time.monotonic_ns()}"
    return session


def _manager_with_gated_spawn(monkeypatch):
    """A manager whose ``_spawn_pty`` blocks until ``release`` is set, then
    registers a session like the real one does."""
    manager = SessionManager()
    entered = threading.Event()
    release = threading.Event()

    def slow_spawn(project_dir, name, flags, agent, rows, cols, history, label):
        entered.set()
        assert release.wait(5), "the test never released the spawn"
        session = _fake_session(label)
        with manager._lock:
            manager._sessions[session.session_id] = session
        return session

    monkeypatch.setattr(manager, "_spawn_pty", slow_spawn)
    return manager, entered, release


def _create(manager, label, results):
    try:
        results.append(manager.create("p", "n", "", "claude", label=label))
    except Exception as exc:  # noqa: BLE001 - the test inspects what was raised
        results.append(exc)


class TestConcurrentChannelSpawns:
    def test_a_second_spawn_while_the_first_is_starting_is_refused(
        self, monkeypatch
    ):
        manager, entered, release = _manager_with_gated_spawn(monkeypatch)
        first: list = []
        worker = threading.Thread(
            target=_create, args=(manager, "telegram:garden", first)
        )
        worker.start()
        assert entered.wait(5)

        with pytest.raises(DuplicateChannelSession) as refused:
            manager.create("p", "n", "", "claude", label="telegram:garden")
        assert "already being started" in str(refused.value)

        release.set()
        worker.join(5)
        assert len(first) == 1 and not isinstance(first[0], Exception)
        assert len(manager.list()) == 1

    def test_a_spawn_after_the_first_registered_names_its_session(
        self, monkeypatch
    ):
        manager, entered, release = _manager_with_gated_spawn(monkeypatch)
        release.set()
        live = manager.create("p", "n", "", "claude", label="telegram:garden")
        with pytest.raises(DuplicateChannelSession) as refused:
            manager.create("p", "n", "", "claude", label="telegram:garden")
        assert live.session_id[:8] in str(refused.value)

    def test_the_claim_is_released_when_the_spawn_fails(self, monkeypatch):
        manager = SessionManager()
        calls = []

        def spawn(*args):
            calls.append(args)
            if len(calls) == 1:
                raise OSError("spawn blew up")
            return _fake_session("telegram:garden")

        monkeypatch.setattr(manager, "_spawn_pty", spawn)
        with pytest.raises(OSError):
            manager.create("p", "n", "", "claude", label="telegram:garden")
        manager.create("p", "n", "", "claude", label="telegram:garden")
        assert len(calls) == 2

    def test_another_profile_and_a_dead_session_do_not_block(self, monkeypatch):
        manager = SessionManager()
        dead = _fake_session("telegram:garden", alive=False)
        manager._sessions[dead.session_id] = dead
        monkeypatch.setattr(
            manager, "_spawn_pty", lambda *a: _fake_session(a[-1])
        )
        manager.create("p", "n", "", "claude", label="telegram:garden")
        manager.create("p", "n", "", "claude", label="telegram:kitchen")

    def test_an_unlabelled_or_other_labelled_session_is_unrestricted(
        self, monkeypatch
    ):
        manager = SessionManager()
        live = _fake_session("chief")
        manager._sessions[live.session_id] = live
        monkeypatch.setattr(
            manager, "_spawn_pty", lambda *a: _fake_session(a[-1])
        )
        manager.create("p", "n", "", "claude", label="chief")
        manager.create("p", "n", "", "claude", label="")
        manager.create("p", "n", "", "claude", label="")


def test_the_endpoint_answers_409_with_the_reason(monkeypatch):
    def refuse(*args, **kwargs):
        raise DuplicateChannelSession("telegram:garden", "abcdef123456")

    monkeypatch.setattr(server.manager, "create", refuse)
    resp = TestClient(server.app).post(
        "/sessions",
        json={"project_dir": r"C:\proj", "name": "n", "flags": "",
              "label": "telegram:garden"},
    )
    assert resp.status_code == 409
    assert "telegram:garden is live in session abcdef12" in resp.json()["detail"]
    assert "one session" in resp.json()["detail"]
