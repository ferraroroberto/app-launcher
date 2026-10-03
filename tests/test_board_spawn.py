"""Board spawn helpers (``board_spawn``) — the per-launch model selector and
the readiness wait shared by the issue-start and fleet-chief launch paths.

The free-text dispatch endpoint (#302) that first exercised these is gone
(#1382); the spawn-then-type contract itself is still pinned end to end by
``test_chief_ensure.py`` (timeout kill, boot-never-quiescent cap) and
``test_board_drilldown.py`` (chief-managed marking, #1283). What those go
through the HTTP routes for, this file reaches directly: the model allowlist
and the two readiness edges (a dead session, a session-host old enough to not
report ``output_chars``).
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from app.webapp.routers import board_spawn


@pytest.fixture
def _fast_probe(monkeypatch):
    """Shrink the readiness constants so no test ever really waits."""
    monkeypatch.setattr(board_spawn, "DISPATCH_READY_CAP_S", 0.3)
    monkeypatch.setattr(board_spawn, "DISPATCH_SETTLE_S", 0.0)
    monkeypatch.setattr(board_spawn, "DISPATCH_POLL_S", 0.01)
    monkeypatch.setattr(board_spawn, "DISPATCH_LEGACY_GRACE_S", 0.0)


class TestBoardModelResolution:
    """``_agent_and_flags`` validates a Board per-launch model (#500/#505)."""

    def _cfg(self, webapp_client):
        _, app, _ = webapp_client
        return app.state.webapp_config

    def test_rejects_the_legacy_gpt5_6_alias(self, webapp_client):
        """#1004 — ``gpt5.6`` used to be silently rewritten to
        ``gpt-5.6-sol`` here, while both sibling resolvers rejected the
        identical string with a 400. No client, no catalog entry
        (``src/model_catalog.py`` carries only ``gpt-5.6-sol``) and no doc
        referenced it, so the rewrite only made this one route disagree
        with the other two. It now takes the normal unknown-model 400.
        """
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as excinfo:
            board_spawn._agent_and_flags(self._cfg(webapp_client), "gpt5.6")
        assert excinfo.value.status_code == 400
        assert "gpt5.6" in str(excinfo.value.detail)

    def test_still_accepts_the_real_catalog_value(self, webapp_client, monkeypatch):
        """The guard rail: removing the alias must not break the model it
        used to rewrite *to*."""
        monkeypatch.setattr(board_spawn.agents, "is_installed", lambda _: True)
        agent, flags = board_spawn._agent_and_flags(
            self._cfg(webapp_client), "codex:gpt-5.6-sol"
        )
        assert agent == "codex"
        assert "gpt-5.6-sol" in flags


class TestAwaitDispatchReady:

    def _host(self, monkeypatch, info):
        calls = []

        def fake_get(port, sid):
            calls.append((port, sid))
            return info

        monkeypatch.setattr(board_spawn.session_client, "get_session", fake_get)
        return calls

    def test_dead_session_504s_without_typing_into_it(
        self, _fast_probe, monkeypatch
    ):
        self._host(monkeypatch, {"alive": False})
        with pytest.raises(HTTPException) as excinfo:
            asyncio.run(board_spawn._await_dispatch_ready(8446, "s-1"))
        assert excinfo.value.status_code == 504
        assert "died" in excinfo.value.detail

    def test_legacy_host_without_output_chars_degrades_to_the_grace(
        self, _fast_probe, monkeypatch
    ):
        """A live :8446 running pre-#302 code omits ``output_chars`` — the
        wait degrades to the fixed grace instead of refusing."""
        self._host(monkeypatch, {"alive": True})
        asyncio.run(board_spawn._await_dispatch_ready(8446, "s-1"))

    def test_no_output_within_the_cap_504s(self, _fast_probe, monkeypatch):
        self._host(monkeypatch, {"alive": True, "output_chars": 0})
        with pytest.raises(HTTPException) as excinfo:
            asyncio.run(board_spawn._await_dispatch_ready(8446, "s-1"))
        assert excinfo.value.status_code == 504
        assert "no output" in excinfo.value.detail


class TestFreeTextDispatchIsGone:
    """#1382: the Add / Build / Yolo endpoint was removed, not just hidden."""

    def test_post_board_dispatch_is_404(self, webapp_client, monkeypatch):
        from app.webapp import middleware

        # Loopback, so the answer is the router's own and not the tailnet gate's.
        monkeypatch.setattr(
            middleware, "LOOPBACK_HOSTS",
            frozenset({"testclient", "127.0.0.1", "::1", "localhost"}),
        )
        client, app, _ = webapp_client
        # The 404 alone proves little (the old route also 404'd an unknown
        # repo), so pin that no route answers the path at all.
        assert "/api/board/dispatch" not in {
            getattr(r, "path", None) for r in app.routes
        }
        resp = client.post(
            "/api/board/dispatch",
            json={"repo": "x", "goal": "g", "mode": "add"},
        )
        assert resp.status_code == 404
