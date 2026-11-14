"""Settings' "Open sessions in a new window" governs every PC mirror window.

With it off, no launch from any surface opens the dedicated Edge window,
whoever asks (a phone, a desktop browser, a script). With it on the existing
rules (``should_mirror_to_pc``) decide as before. The row menu's explicit
"Open in new window" is the one route the toggle does not gate:
``POST /api/claude-code/sessions/<sid>/mirror`` still opens a window.

Each launch path is driven end to end and the *real* ``should_mirror_to_pc``
is wrapped by a spy, so what is asserted is the decision the launch actually
took, not a stub of it. ``TestClient``'s host is not loopback, so with the
toggle on every case below is a mirror (the phone rule), which makes the
on/off pair a clean A/B.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.webapp.routers import _helpers
from src.webapp_config import SESSION_HOST_PORT_ENV


@pytest.fixture(autouse=True)
def _not_a_disposable_instance(monkeypatch) -> None:
    monkeypatch.delenv(SESSION_HOST_PORT_ENV, raising=False)


@pytest.fixture
def decisions(monkeypatch) -> list:
    """Every PC-window decision a launch took, from the real function."""
    taken: list = []
    real = _helpers.should_mirror_to_pc

    def spy(show_local_window, request, body):
        verdict = real(show_local_window, request, body)
        taken.append(verdict)
        return verdict

    monkeypatch.setattr(_helpers, "should_mirror_to_pc", spy)
    return taken


def _set(app, *, window: bool) -> None:
    cfg = app.state.webapp_config
    cfg.claude_show_local_window = True
    cfg.open_sessions_in_window = window


def _trust_test_client(monkeypatch) -> None:
    """Board and Life OS launches are terminal endpoints (Tailscale/loopback
    only); let ``TestClient`` pass that gate."""
    from src import middleware

    monkeypatch.setattr(
        middleware.trust, "LOOPBACK_HOSTS",
        frozenset({"testclient", "127.0.0.1", "::1", "localhost"}),
    )


def _coding_tab(client, app, overrides, monkeypatch) -> None:
    from app.webapp.routers import apps as apps_router

    (overrides["tmp_projects_dir"] / "live-proj").mkdir()
    monkeypatch.setattr(
        apps_router, "spawn_claude_session",
        lambda *a, **k: {"session_id": "s1", "kind": "pty", "agent": "claude"},
    )
    resp = client.post("/api/apps/live-proj/launch", json={"rows": 40, "cols": 120})
    assert resp.status_code == 200, resp.text


def _board_issue_start(client, app, overrides, monkeypatch) -> None:
    from app.webapp.routers import board as board_router

    _trust_test_client(monkeypatch)
    (overrides["tmp_projects_dir"] / "myrepo").mkdir()
    monkeypatch.setattr(
        board_router, "spawn_claude_session",
        lambda *a, **k: {"session_id": "spawned-1", "kind": "pty"},
    )
    resp = client.post(
        "/api/board/issues/start",
        json={"repo": "myrepo", "number": 42, "mode": "start"},
    )
    assert resp.status_code == 200, resp.text


def _life_os_skill(client, app, overrides, monkeypatch) -> None:
    from app.webapp.routers import life_os_spawn

    _trust_test_client(monkeypatch)
    monkeypatch.setattr(
        life_os_spawn, "spawn_claude_session",
        lambda *a, **k: {"session_id": "s1", "kind": "pty"},
    )
    resp = client.post("/api/life-os/skills/journal-daily/launch", json={"mode": "pty"})
    assert resp.status_code == 200, resp.text


@pytest.mark.parametrize(
    "launch, fixture_name",
    [
        (_coding_tab, "webapp_client"),
        (_board_issue_start, "webapp_client"),
        (_life_os_skill, "life_os_client"),
    ],
    ids=["coding-tab", "board-issue-start", "life-os-skill"],
)
@pytest.mark.parametrize("window, expected", [(False, [False]), (True, [True])],
                         ids=["toggle-off-no-window", "toggle-on-unchanged"])
def test_launch_path_follows_the_toggle(
    request, monkeypatch, decisions, launch, fixture_name, window, expected,
) -> None:
    client, app, overrides = request.getfixturevalue(fixture_name)
    _set(app, window=window)
    launch(client, app, overrides, monkeypatch)
    assert decisions == expected


def _tail(cfg, *, body, host="100.64.0.5") -> list:
    """Run the shared launch tail once; return the URLs it asked to mirror."""
    opened: list = []
    audit_mod = MagicMock()
    req = SimpleNamespace(client=SimpleNamespace(host=host))

    async def _run() -> None:
        await _helpers.audit_session_start_and_maybe_mirror(
            cfg, req, body, sid="s1", agent="claude", name="n", project="p",
            audit_mod=audit_mod,
            mirror_fn=lambda url, sid: opened.append((url, sid)),
        )
        # The mirror is fire-and-forget on the loop; let it run.
        await asyncio.sleep(0.05)

    asyncio.run(_run())
    return opened


@pytest.mark.parametrize(
    "host, body",
    [
        ("100.64.0.5", {}),                       # a phone
        ("100.64.0.5", {"desktop": True}),        # a desktop browser over the tunnel
        ("127.0.0.1", {}),                        # a script / chief over loopback
        ("127.0.0.1", {"desktop": True}),         # a desktop browser on the PC
    ],
    ids=["phone", "desktop-tunnel", "loopback-api", "desktop-loopback"],
)
def test_toggle_off_opens_no_window_whoever_asks(monkeypatch, host, body) -> None:
    monkeypatch.setattr(_helpers, "mirror_url", lambda *a: "https://127.0.0.1/?terminal=s1")
    on = SimpleNamespace(claude_show_local_window=True, open_sessions_in_window=True)
    off = SimpleNamespace(claude_show_local_window=True, open_sessions_in_window=False)
    assert _tail(on, body=body, host=host) == [("https://127.0.0.1/?terminal=s1", "s1")]
    assert _tail(off, body=body, host=host) == []


def test_explicit_open_in_new_window_ignores_the_toggle(webapp_client, monkeypatch) -> None:
    """The row menu's "Open in new window" posts to /mirror with the toggle off."""
    client, app, _ = webapp_client
    _set(app, window=False)
    from app.webapp.routers import sessions as sessions_router

    mock_open = MagicMock(return_value="opened")
    monkeypatch.setattr(sessions_router.launcher, "open_or_focus_mirror_window", mock_open)
    resp = client.post("/api/claude-code/sessions/abc-123/mirror")
    assert resp.json() == {"mirrored": True, "action": "opened"}
    mock_open.assert_called_once()
