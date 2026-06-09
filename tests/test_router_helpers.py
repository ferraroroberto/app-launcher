"""Unit tests for cross-router helpers.

Focus: ``should_mirror_to_pc`` — the decision (issue #20 / #159) of whether
a PTY launch opens the PC mirror window. A phone launch (non-loopback, no
``desktop`` flag) opens it; a desktop browser (loopback, or a non-loopback
client that set ``desktop: true``) skips it.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.webapp.routers._helpers import should_mirror_to_pc


def _request(host: str) -> SimpleNamespace:
    """Minimal stand-in for a Starlette Request: only `.client.host` is read."""
    return SimpleNamespace(client=SimpleNamespace(host=host))


def test_phone_launch_opens_mirror() -> None:
    # Non-loopback (a phone over the tunnel/tailnet), no desktop flag → mirror.
    assert should_mirror_to_pc(True, _request("100.64.0.5"), {}) is True


def test_desktop_flag_skips_mirror() -> None:
    # The issue #159 fix: a desktop browser over the tunnel is non-loopback
    # but already shows the terminal in-page, so it suppresses the mirror.
    assert (
        should_mirror_to_pc(True, _request("100.64.0.5"), {"desktop": True})
        is False
    )


def test_loopback_launch_skips_mirror() -> None:
    # The PC itself by IP — the launching browser already shows the terminal.
    for host in ("127.0.0.1", "::1", "localhost"):
        assert should_mirror_to_pc(True, _request(host), {}) is False


def test_disabled_flag_never_mirrors() -> None:
    # claude_show_local_window off → never open the mirror, even for a phone.
    assert should_mirror_to_pc(False, _request("100.64.0.5"), {}) is False
