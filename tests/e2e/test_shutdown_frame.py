"""Regression pin for #181 — the cooperative WS-shutdown close fallback.

The bug: the PC mirror window's "Stop & Close" had two documented close
paths — a primary Win32 ``WM_CLOSE`` (issue #20) and a cooperative
``{"type":"shutdown"}`` WebSocket frame as a fallback — but the client half
of the fallback was *dead code*. ``terminal.js`` ``ws.onmessage`` wrote every
frame to xterm unconditionally, so when the Win32 path missed (no HWND ever
captured) nothing closed the window and the shutdown JSON was printed into
the terminal as garbage.

Fix: ``ws.onmessage`` routes each frame through ``routeFrame`` first — a
mirror window self-closes on a shutdown frame, the phone drops it, and
ordinary terminal output still writes through. This pins that routing
*decision* deterministically (no live PTY, so it runs on CI too, unlike the
``launched_pty_session``-gated ``test_edge_mirror_close.py``); the actual OS
window close is the manual / e2e acceptance step, and the server half (the
frame being sent) is pinned by ``tests/test_session_host_pty_stop.py``.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page

pytestmark = pytest.mark.smoke

# Exercise routeFrame in the page across the frame kinds that share the
# server→client stream. The only variables are the frame text and whether
# the page is a mirror window.
_EVAL = r"""
async () => {
  const m = await import('/static/terminal.js');
  const r = (data, isMirror) => m.routeFrame(data, isMirror);
  return {
    shutdownMirror: r('{"type":"shutdown"}', true),
    shutdownPhone:  r('{"type":"shutdown"}', false),
    // Ordinary terminal output — including a program that prints a
    // brace-leading line or some *other* JSON shape — must always write.
    plainMirror:    r('hello world\r\n', true),
    plainPhone:     r('hello world\r\n', false),
    braceNonJson:   r('{ not valid json', true),
    otherJsonObj:   r('{"type":"resize","rows":40}', true),
    nonString:      r(null, true),
  };
}
"""


def test_route_frame_classifies_shutdown_vs_output(
    authed_page: Page, base_url: str
) -> None:
    authed_page.goto(base_url, wait_until="domcontentloaded")
    res = authed_page.evaluate(_EVAL)

    # A shutdown control frame closes a mirror window and is dropped on the
    # phone — never written to xterm in either case.
    assert res["shutdownMirror"] == "close-mirror"
    assert res["shutdownPhone"] == "swallow"

    # Everything else is ordinary terminal output and must be written, so no
    # real PTY byte is ever swallowed: plain text, a brace-leading non-JSON
    # line, an unrelated JSON object, and a non-string frame.
    assert res["plainMirror"] == "write"
    assert res["plainPhone"] == "write"
    assert res["braceNonJson"] == "write"
    assert res["otherJsonObj"] == "write"
    assert res["nonString"] == "write"
