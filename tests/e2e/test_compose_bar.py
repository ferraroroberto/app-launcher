"""Regression pin for issues #37 / #41 (mobile compose bar).

The feature: a ``✏️`` toolbar button toggles a slim ``<textarea>`` compose
bar above the iOS keyboard. xterm.js wipes its helper textarea after
every keystroke, so iOS/Android predictive keyboards can't suggest there
— the compose bar is a normal textarea with default predictive
attributes. ``➤`` Send forwards ``<text>`` to the PTY over the WS
``input`` channel, then a submitting ``\\r`` as a *separate* frame so it
can't be absorbed into bracketed-paste finalization (#166).

Phase 2 (#41): with the bar open, the ``🖼`` image button uploads with
``?inline=1`` so the session-host returns the stored path *without*
pasting it into the PTY, and the browser drops that path into the
textarea at the caret — the review-before-send pattern ``📋`` uses.

The e2e harness connects from loopback, so every terminal open is
detected as the PC mirror (``isMirror`` true). That is itself the case
issue #37 verification step 4 pins: the ``✏️`` button must be hidden in
the mirror. To exercise the Send path we un-hide the toggle button and
drive the real handler — the Send logic is not mirror-gated, only the
button's visibility is.

Predictive suggestions themselves are an OS-keyboard behaviour and can
only be confirmed on a real phone; this test pins the wiring underneath.
"""

from __future__ import annotations

import base64
import re

import pytest
from playwright.sync_api import Page, expect

# 1x1 transparent PNG — smallest valid image the session-host will accept.
_PNG_1x1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mNk"
    "YAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)

# The session-host stores uploads under <project>\.launcher-tmp\ — the
# inline path dropped into the compose bar must point there.
_PATH_RE = re.compile(r"\.launcher-tmp.*\.png$")

pytestmark = pytest.mark.smoke


def _open_terminal(page: Page, base_url: str, sid: str) -> None:
    page.goto(f"{base_url}/?terminal={sid}", wait_until="domcontentloaded")
    page.wait_for_selector("#terminalOverlay:not([hidden])", timeout=10_000)
    page.wait_for_function(
        "() => document.getElementById('terminalStatus') "
        "&& document.getElementById('terminalStatus').hidden === true",
        timeout=10_000,
    )


def test_compose_button_hidden_in_mirror(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    """Loopback open is the PC mirror — the ✏️ button must stay hidden."""
    _open_terminal(authed_page, base_url, launched_pty_session)
    expect(authed_page.locator("#terminalCompose")).to_be_hidden()
    expect(authed_page.locator("#terminalComposeBar")).to_be_hidden()


def test_compose_send_forwards_text_to_pty(
    authed_page: Page,
    base_url: str,
    launched_pty_session: str,
    wait_for_session_log,
) -> None:
    """➤ Send forwards the textarea contents + Enter to the PTY."""
    sid = launched_pty_session
    _open_terminal(authed_page, base_url, sid)

    # The button is hidden under loopback (mirror) — un-hide it so the
    # real toggle handler / setComposeOpen() runs. Send itself is not
    # mirror-gated, so this exercises the genuine production path.
    authed_page.evaluate(
        "document.getElementById('terminalCompose').hidden = false"
    )
    authed_page.locator("#terminalCompose").click()
    expect(authed_page.locator("#terminalComposeBar")).to_be_visible()

    payload = "compose-{regress}"
    authed_page.locator("#terminalComposeInput").fill(payload)
    authed_page.locator("#terminalComposeSend").click()

    # Bar clears and stays open after Send.
    expect(authed_page.locator("#terminalComposeInput")).to_have_value("")
    expect(authed_page.locator("#terminalComposeBar")).to_be_visible()

    assert wait_for_session_log(authed_page, sid, payload), (
        f"➤ Send did not deliver the compose text to webapp/sessions/{sid}.log "
        "— the text never reached the live PTY session"
    )


def test_compose_send_submits_cr_in_its_own_frame(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    r"""➤ Send delivers the submitting CR as a separate, final WS frame (#166).

    The intermittent "Send does nothing" bug was the trailing ``\r`` riding
    in the same WS frame as the ``\x1b[201~`` paste-end marker, where the TUI
    sometimes swallowed it into paste finalization instead of submitting. We
    spy on every outgoing ``input`` frame and pin that the last one is a lone
    ``\r`` while the text rode an earlier frame with no CR glued on — the
    ordering invariant, holding whether or not the live agent has bracketed
    paste enabled.
    """
    sid = launched_pty_session
    _open_terminal(authed_page, base_url, sid)

    # Record the data of every outgoing WS `input` frame, in order. Patching
    # the prototype catches the already-open socket too (send resolves on the
    # prototype at call time); resize frames are type!='input' and skipped.
    authed_page.evaluate(
        """() => {
            window.__sentInput = [];
            const orig = WebSocket.prototype.send;
            WebSocket.prototype.send = function (d) {
                try {
                    const m = JSON.parse(d);
                    if (m && m.type === 'input') window.__sentInput.push(m.data);
                } catch (_) { /* non-JSON frame */ }
                return orig.call(this, d);
            };
        }"""
    )

    # Un-hide + open the compose bar (mirror trick — see module docstring).
    authed_page.evaluate(
        "document.getElementById('terminalCompose').hidden = false"
    )
    authed_page.locator("#terminalCompose").click()
    expect(authed_page.locator("#terminalComposeBar")).to_be_visible()

    payload = "compose-cr-frame"
    authed_page.locator("#terminalComposeInput").fill(payload)
    authed_page.locator("#terminalComposeSend").click()

    frames = authed_page.evaluate("() => window.__sentInput")
    assert len(frames) >= 2, f"➤ Send produced too few input frames: {frames!r}"
    # The submitting CR is its own, final frame …
    assert frames[-1] == "\r", f"submit CR was not its own final frame: {frames!r}"
    # … and the payload rode the frame before it, with no CR glued on.
    assert payload in frames[-2], f"payload not in the pre-CR frame: {frames!r}"
    assert "\r" not in frames[-2], f"CR leaked into the text frame: {frames!r}"


def test_compose_image_inserts_path_into_bar(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    """🖼 with the bar open drops the uploaded path into the textarea (#41)."""
    sid = launched_pty_session
    _open_terminal(authed_page, base_url, sid)

    # Un-hide + open the compose bar (mirror trick — see module docstring).
    authed_page.evaluate(
        "document.getElementById('terminalCompose').hidden = false"
    )
    authed_page.locator("#terminalCompose").click()
    expect(authed_page.locator("#terminalComposeBar")).to_be_visible()

    # The file input is triggered by the 🖼 button click; set it directly.
    authed_page.locator("#terminalImageInput").set_input_files(
        files=[{"name": "regress.png", "mimeType": "image/png", "buffer": _PNG_1x1}]
    )

    # The uploaded image path lands in the textarea, not the PTY.
    compose = authed_page.locator("#terminalComposeInput")
    expect(compose).to_have_value(_PATH_RE, timeout=10_000)
    expect(authed_page.locator("#terminalComposeBar")).to_be_visible()
