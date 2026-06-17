"""Unit tests for cross-router helpers.

Focus: ``should_mirror_to_pc`` — the decision (issue #20 / #241) of whether
a PTY launch opens the dedicated PC mirror window. Both a phone launch
(non-loopback, no ``desktop`` flag) and a desktop-browser launch
(``desktop: true``, loopback or tunnel) open it; only a non-desktop loopback
launch skips it and renders in-page.
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


def test_desktop_flag_opens_mirror_over_tunnel() -> None:
    # Issue #241: a desktop browser gets a dedicated PC Edge window, not an
    # in-page terminal — over the tunnel (non-loopback) it mirrors.
    assert (
        should_mirror_to_pc(True, _request("100.64.0.5"), {"desktop": True})
        is True
    )


def test_desktop_flag_opens_mirror_over_loopback() -> None:
    # Issue #241, the user's exact scenario: desktop Chrome on the PC itself
    # (loopback) must still get its own Edge window so Stop & Close never
    # tears down the controlling browser. The desktop flag wins over the IP.
    for host in ("127.0.0.1", "::1", "localhost"):
        assert (
            should_mirror_to_pc(True, _request(host), {"desktop": True}) is True
        )


def test_loopback_launch_without_desktop_flag_skips_mirror() -> None:
    # A loopback client that did NOT flag itself a desktop (the rare coarse-
    # pointer PC browser) still renders in-page — harmless now that an in-page
    # loopback terminal is no longer mis-classified as a mirror (issue #241).
    for host in ("127.0.0.1", "::1", "localhost"):
        assert should_mirror_to_pc(True, _request(host), {}) is False


def test_disabled_flag_never_mirrors() -> None:
    # claude_show_local_window off → never open the mirror, even for a phone.
    assert should_mirror_to_pc(False, _request("100.64.0.5"), {}) is False
