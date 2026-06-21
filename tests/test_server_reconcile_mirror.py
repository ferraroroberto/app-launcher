"""Regression pin for issue #278 — the e2e/verify-gate mirror-window slaughter.

``_reconcile_orphan_mirror_windows`` runs on every webapp boot and closes Edge
mirror windows no live session backs. The sweep is **machine-global**
(``EnumWindows`` over the whole desktop), so a *disposable* webapp — the
e2e / verify-before-ship gate's autoboot instance, pointed at an *empty*
disposable session-host — would compute an empty live list and ``WM_CLOSE``
every real ``app-launcher-mirror-*`` window on the desktop, killing the user's
live session mirrors while the sessions survive headless on the real ``:8446``.

The fix: a disposable instance is identified by the
``LAUNCHER_SESSION_HOST_PORT`` override (set *only* by autoboot, never in
production) and skips the sweep entirely. These tests pin that the guard fires
under the gate and that the production path is otherwise untouched.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.webapp import server
from src.webapp_config import SESSION_HOST_PORT_ENV


def _app(session_host_port: int = 8446) -> SimpleNamespace:
    return SimpleNamespace(
        state=SimpleNamespace(
            webapp_config=SimpleNamespace(session_host_port=session_host_port)
        )
    )


@pytest.mark.asyncio
async def test_disposable_instance_skips_global_sweep(monkeypatch):
    """With LAUNCHER_SESSION_HOST_PORT set (autoboot), the reconcile must not
    even look at sessions, let alone close any desktop windows (#278)."""
    monkeypatch.setenv(SESSION_HOST_PORT_ENV, "54321")

    calls = {"list": 0, "close": 0}

    def _list(*a, **k):
        calls["list"] += 1
        return []

    def _close(live_sids):
        calls["close"] += 1
        return 99

    monkeypatch.setattr(server.session_client, "list_sessions", _list)
    monkeypatch.setattr(server.launcher, "close_orphan_mirror_windows", _close)

    await server._reconcile_orphan_mirror_windows(_app())

    assert calls == {"list": 0, "close": 0}, (
        "disposable webapp must skip the machine-global mirror sweep entirely"
    )


@pytest.mark.asyncio
async def test_canonical_instance_reconciles_against_live_sids(monkeypatch):
    """Without the override (the real :8445 webapp), the sweep still runs and is
    handed exactly the live session ids — the legitimate #199 cleanup."""
    monkeypatch.delenv(SESSION_HOST_PORT_ENV, raising=False)

    seen = {}

    monkeypatch.setattr(
        server.session_client,
        "list_sessions",
        lambda *a, **k: [{"session_id": "aaa"}, {"session_id": "bbb"}, {}],
    )

    def _close(live_sids):
        seen["live_sids"] = list(live_sids)
        return 0

    monkeypatch.setattr(server.launcher, "close_orphan_mirror_windows", _close)

    await server._reconcile_orphan_mirror_windows(_app())

    assert seen["live_sids"] == ["aaa", "bbb"], (
        "canonical instance must reconcile against the live session ids"
    )
