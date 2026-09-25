"""Regression pin for issues #37 / #41 / #980 (the docked composer).

The feature: a predictive ``<textarea>`` composer docked under the terminal.
xterm.js wipes its helper textarea after every keystroke, so iOS/Android
predictive keyboards can't suggest there — the composer is a normal textarea
with default predictive attributes. ``➤`` Send forwards ``<text>`` to the
PTY over the WS ``input`` channel, then a submitting ``\\r`` as a *separate*
frame so it can't be absorbed into bracketed-paste finalization (#166).

Since #980 the composer is always docked (no ✏️ toggle) and is the one
shared module every session surface mounts (``composer.js``): a tall
textarea plus a 2×2 grid — mic · keys / image · send. Its markup is rendered
by the module, so tests key on class hooks scoped to the terminal's mount
(``#terminalComposeBar .composer-*``), not page-unique ids.

Attach (#41 / #366 / #448): the image button's *Attach image or file* option
uploads with ``?inline=1`` so the session-host returns the stored path
*without* pasting it into the PTY, and the composer appends that path to
the text — review before send.

The e2e harness connects from loopback, so every terminal open is detected
as the PC mirror (``isMirror`` true). That is itself the case issue #37
verification step 4 pins: the composer must be hidden in the mirror. To
exercise the handlers we un-hide the composer and drive them — the logic is
not mirror-gated, only the composer's visibility is.

Predictive suggestions themselves are an OS-keyboard behaviour and can
only be confirmed on a real phone; this test pins the wiring underneath.
"""

from __future__ import annotations

import base64
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import OVERLAY_OPEN_MS, stable_read

# 1x1 transparent PNG — smallest valid image the session-host will accept.
_PNG_1x1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mNk"
    "YAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)

# The session-host stores uploads under <root>\.launcher-tmp\ — the inline
# path dropped into the composer must point there. Under autoboot that
# root is a per-run temp dir rather than this checkout (issue #922), so the
# leaf is what these patterns pin, not the parent.
_PATH_RE = re.compile(r"\.launcher-tmp.*\.png$")

# Every file this module uploads is named `e2e-stub-…` on purpose: it is the
# marker `tests/e2e/conftest.py`'s teardown scans for when asserting that no
# harness upload landed in the checkout's real `.launcher-tmp` (issue #922).
# Renaming one silently weakens that check — keep the prefix.

COMPOSER = "#terminalComposeBar"
INPUT = f"{COMPOSER} .composer-input"
SEND = f"{COMPOSER} .composer-send"
ATTACH_INPUT = f"{COMPOSER} .composer-attach-input"

pytestmark = [pytest.mark.smoke, pytest.mark.iphone]

# Drive the page's OWN setTerminalStatus (#1219) — the live module instance,
# resolved from the resource timeline the way test_markdown_link_rendering.py
# does, so the status goes through the real code path rather than a
# hand-rolled DOM write. Even the unstamped fallback shares the live `els`:
# the server stamps every served module's imports, so it imports the same
# `state.js?v=` instance the page already holds.
_SET_STATUS_JS = r"""
async (message) => {
  const url = performance.getEntriesByType('resource')
    .map((r) => r.name)
    .find((n) => n.includes('/static/terminal-connection.js?v='))
    || '/static/terminal-connection.js';
  const { setTerminalStatus } = await import(url);
  setTerminalStatus(message, message ? { icon: 'lock' } : undefined);
}
"""

# The home-indicator inset of a Face ID iPhone in portrait, in CSS px (the
# ~34px the #1099 note in .fleet.toml measures), and a probe of what
# env(safe-area-inset-bottom) resolves to on the page right now.
_IPHONE_BOTTOM_INSET = 34
_RESOLVED_BOTTOM_INSET_JS = r"""
() => {
  const probe = document.createElement('div');
  probe.style.cssText =
    'position:fixed;visibility:hidden;height:0;padding-bottom:env(safe-area-inset-bottom,0px)';
  document.body.appendChild(probe);
  const px = probe.getBoundingClientRect().height;
  probe.remove();
  return px;
}
"""

# The strip's box, the message's alignment, and where the message's painted
# content (icon + text, via a Range) actually sits inside the strip.
_STATUS_STRIP_JS = r"""
() => {
  const strip = document.getElementById('terminalStatusStrip');
  const msg = document.getElementById('terminalStatus');
  if (!strip || !msg) return null;
  const s = strip.getBoundingClientRect();
  const range = document.createRange();
  range.selectNodeContents(msg);
  const c = range.getBoundingClientRect();
  return {
    top: s.top, height: s.height, left: s.left, right: s.right,
    hidden: msg.hidden, text: msg.textContent,
    textAlign: getComputedStyle(msg).textAlign,
    contentLeft: c.left, contentRight: c.right, contentWidth: c.width,
  };
}
"""


def _open_terminal(page: Page, base_url: str, sid: str) -> None:
    page.goto(f"{base_url}/?terminal={sid}", wait_until="domcontentloaded")
    page.wait_for_selector("#terminalOverlay:not([hidden])", timeout=OVERLAY_OPEN_MS)
    page.wait_for_function(
        "() => document.getElementById('terminalStatus') "
        "&& document.getElementById('terminalStatus').hidden === true",
        timeout=OVERLAY_OPEN_MS,
    )


def _show_composer(page: Page) -> None:
    """Un-hide the composer (mirror trick — see module docstring)."""
    page.evaluate("document.getElementById('terminalComposeBar').hidden = false")
    expect(page.locator(COMPOSER)).to_be_visible()


def test_composer_hidden_in_mirror(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    """Loopback open is the PC mirror — the composer must stay hidden."""
    _open_terminal(authed_page, base_url, launched_pty_session)
    expect(authed_page.locator(COMPOSER)).to_be_hidden()


def test_composer_grid_shape(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    """#980: today's shape on every surface — textarea + 2×2 grid, in the
    order mic · keys / image · send, with no Compose toggle, no OCR button of
    its own, and none of the folded controls left in the bar."""
    _open_terminal(authed_page, base_url, launched_pty_session)
    _show_composer(authed_page)
    order = authed_page.eval_on_selector_all(
        f"{COMPOSER} .compose-tools > button",
        "els => els.map(e => e.className.split(' ').find(c => c.startsWith('composer-')))",
    )
    assert order == ["composer-mic", "composer-keys", "composer-image", "composer-send"], order
    for gone in ("#terminalCompose", "#terminalPaste", "#terminalImage", "#terminalKeys",
                 "#terminalScreenshot", "#terminalComposeAttach"):
        assert authed_page.locator(gone).count() == 0, f"{gone} should be gone (#980)"


def test_compose_send_forwards_text_to_pty(
    authed_page: Page,
    base_url: str,
    launched_pty_session: str,
    wait_for_session_log,
) -> None:
    """➤ Send forwards the textarea contents + Enter to the PTY."""
    sid = launched_pty_session
    _open_terminal(authed_page, base_url, sid)
    _show_composer(authed_page)

    payload = "compose-{regress}"
    authed_page.locator(INPUT).fill(payload)
    authed_page.locator(SEND).click()

    # Bar clears and stays docked after Send.
    expect(authed_page.locator(INPUT)).to_have_value("")
    expect(authed_page.locator(COMPOSER)).to_be_visible()

    assert wait_for_session_log(authed_page, sid, payload), (
        f"➤ Send did not deliver the compose text to webapp/sessions/{sid}.log "
        "— the text never reached the live PTY session"
    )


def test_compose_send_submits_cr_in_its_own_frame(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    r"""➤ Send delivers the submitting CR immediately after the text (#166).

    The intermittent "Send does nothing" bug was the trailing ``\r`` riding
    in the same WS frame as the ``\x1b[201~`` paste-end marker, where the TUI
    sometimes swallowed it into paste finalization instead of submitting. We
    spy on every outgoing ``input`` frame and pin that the payload frame is
    followed immediately by a lone ``\r`` with no CR glued onto the text — the
    ordering invariant, holding whether or not the live agent has bracketed
    paste enabled. Xterm may emit unrelated focus-report input frames before
    or after that pair.
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
    _show_composer(authed_page)

    payload = "compose-cr-frame"
    authed_page.locator(INPUT).fill(payload)
    authed_page.locator(SEND).click()

    frames = authed_page.evaluate("() => window.__sentInput")
    assert len(frames) >= 2, f"➤ Send produced too few input frames: {frames!r}"
    payload_indexes = [i for i, frame in enumerate(frames) if payload in frame]
    assert payload_indexes, f"payload frame was not sent: {frames!r}"
    payload_index = payload_indexes[-1]
    payload_frame = frames[payload_index]
    assert "\r" not in payload_frame, f"CR leaked into the text frame: {frames!r}"
    assert payload_index + 1 < len(frames), (
        f"submit CR did not follow the payload frame: {frames!r}"
    )
    assert frames[payload_index + 1] == "\r", (
        f"submit CR was not its own frame immediately after the payload: {frames!r}"
    )


def test_compose_image_inserts_path_into_bar(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    """Attach drops the uploaded path into the textarea, not the PTY (#41)."""
    sid = launched_pty_session
    _open_terminal(authed_page, base_url, sid)
    _show_composer(authed_page)

    # The file input is triggered by the image button's Attach option; set
    # it directly (the picker itself is native).
    authed_page.locator(ATTACH_INPUT).set_input_files(
        files=[{"name": "e2e-stub-regress.png", "mimeType": "image/png",
                "buffer": _PNG_1x1}]
    )

    # The uploaded image path lands in the textarea, not the PTY.
    compose = authed_page.locator(INPUT)
    expect(compose).to_have_value(_PATH_RE, timeout=10_000)
    expect(authed_page.locator(COMPOSER)).to_be_visible()


def test_compose_attach_appends_at_end_with_blank_line(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    r"""Issue #366: inline uploads always append at the very end as their own
    paragraph — ``<text>\n\n<path1>\n\n<path2>`` — regardless of the caret.
    Also pins the accept-broadening: a non-image file (text/plain) uploads
    fine."""
    sid = launched_pty_session
    _open_terminal(authed_page, base_url, sid)
    _show_composer(authed_page)

    # Type text, then park the caret at position 0 — the append must ignore it.
    compose = authed_page.locator(INPUT)
    compose.fill("look at this file")
    authed_page.evaluate(
        "() => { const ta = document.querySelector('#terminalComposeBar .composer-input');"
        " ta.selectionStart = ta.selectionEnd = 0; }"
    )

    # First attach: a plain-text file through the attach input.
    authed_page.locator(ATTACH_INPUT).set_input_files(
        files=[{"name": "e2e-stub-notes.txt", "mimeType": "text/plain",
                "buffer": b"hello attach"}]
    )
    expect(compose).to_have_value(
        re.compile(r"^look at this file\n\n.*\.launcher-tmp.*notes\.txt$"),
        timeout=10_000,
    )

    # Second attach stacks below the first, blank-line separated.
    authed_page.locator(ATTACH_INPUT).set_input_files(
        files=[{"name": "e2e-stub-shot.png", "mimeType": "image/png",
                "buffer": _PNG_1x1}]
    )
    expect(compose).to_have_value(
        re.compile(
            r"^look at this file\n\n.*notes\.txt\n\n.*\.launcher-tmp.*\.png$"
        ),
        timeout=10_000,
    )

    # The image button exists in the grid and is wired to the input.
    expect(authed_page.locator(f"{COMPOSER} .composer-image")).to_be_attached()


def test_compose_attach_multiple_images_in_one_pick(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    """Issue #448: picking several gallery images in ONE file-picker
    interaction (a single ``set_input_files`` call with 2+ files, mirroring
    a multi-select gallery pick on the phone) uploads all of them and lands
    every path in the composer, in order, blank-line separated — the same
    append shape as two sequential single-file attaches, but from one picker
    action instead of a pick-upload-repeat loop."""
    sid = launched_pty_session
    _open_terminal(authed_page, base_url, sid)
    _show_composer(authed_page)

    # The attach input must accept a multi-select pick.
    expect(authed_page.locator(ATTACH_INPUT)).to_have_attribute(
        "multiple", re.compile(r".*")
    )

    authed_page.locator(ATTACH_INPUT).set_input_files(
        files=[
            {"name": "e2e-stub-shot1.png", "mimeType": "image/png",
             "buffer": _PNG_1x1},
            {"name": "e2e-stub-shot2.png", "mimeType": "image/png",
             "buffer": _PNG_1x1},
        ]
    )

    compose = authed_page.locator(INPUT)
    expect(compose).to_have_value(
        re.compile(r"^.*\.launcher-tmp.*shot1\.png\n\n.*\.launcher-tmp.*shot2\.png$"),
        timeout=10_000,
    )


def test_compose_send_and_attach_stay_put_during_autogrow(
    authed_page: Page, base_url: str, launched_pty_session: str, browser_name: str
) -> None:
    """Issue #447: the ➤ Send button (and the compose-tools grid, e.g. the
    image button) must not move when the textarea auto-grows on a dictation
    transcript landing. `.compose-bar` used to be `align-items: stretch`, so
    a taller textarea stretched every sibling button to match — measured
    46px of top-edge drift on a realistic transcript on the WebKit/iPhone
    projection, a moving-target race against a tap aimed at the pre-grow
    position (a second tap could land on a shifted-away button, or on
    whatever the growing textarea now covers, reading as "Send did nothing"
    or "the tap became a newline"). `align-items: flex-end` anchors every
    button to the row's one stable edge (the bar's bottom never moves; only
    its top climbs), so only the textarea itself grows.

    Issue #1219 extends the same pin downward: a connection status message
    appearing or clearing must not move them either, and it sits centred in
    the reserved bottom strip. Reopened, it pins how tall that strip gets
    under an iPhone's home-indicator inset (emulated on Chromium)."""
    sid = launched_pty_session
    _open_terminal(authed_page, base_url, sid)
    _show_composer(authed_page)

    send = authed_page.locator(SEND)
    attach = authed_page.locator(f"{COMPOSER} .composer-image")
    send_before = send.bounding_box()
    attach_before = attach.bounding_box()
    assert send_before and attach_before

    # A realistic dictated transcript length (~2-3 sentences) — long enough
    # to push the textarea well past its resting min-height.
    long_text = (
        "Hey can you check the login flow again because yesterday I noticed "
        "the button was not responding on the first tap and I had to tap it "
        "twice before it actually submitted the form so please take a look."
    )
    authed_page.locator(INPUT).fill(long_text)
    authed_page.wait_for_timeout(200)

    send_after = send.bounding_box()
    attach_after = attach.bounding_box()
    assert send_after and attach_after

    # Sub-pixel rounding tolerance only — any real drift here is the #447 bug.
    assert abs(send_after["y"] - send_before["y"]) < 1, (
        f"Send button moved during autogrow: {send_before} -> {send_after}"
    )
    assert abs(send_after["height"] - send_before["height"]) < 1, (
        f"Send button resized during autogrow: {send_before} -> {send_after}"
    )
    assert abs(attach_after["y"] - attach_before["y"]) < 1, (
        f"Attach button moved during autogrow: {attach_before} -> {attach_after}"
    )

    # #1219: a status message coming and going must not move them either.
    # The status line used to be a `hidden` flex child under the composer, so
    # every "Connecting…" pushed the whole grid up one line and every connect
    # dropped it back — the same moving-target race as #447, from below. It
    # is now the content of an always-laid-out bottom strip, so only the
    # strip's content changes, and the message is centred in it.
    #
    # env(safe-area-inset-bottom) resolves to 0px in headless Chromium and in
    # the WebKit/iPhone projection alike (the #1099 blind spot, .fleet.toml),
    # so these checks run with no inset; the Chromium-only block at the end
    # emulates one. Whether the corners actually clear is still a device check.
    idle = stable_read(lambda: authed_page.evaluate(_STATUS_STRIP_JS))
    assert idle and idle["hidden"] is True, f"status not idle once connected: {idle}"
    assert idle["height"] >= 30, (
        f"the bottom status strip is not reserving its row while idle: {idle}"
    )

    authed_page.evaluate(_SET_STATUS_JS, "Passkey unlock required")
    shown = stable_read(lambda: authed_page.evaluate(_STATUS_STRIP_JS))
    send_status = stable_read(send.bounding_box)
    attach_status = stable_read(attach.bounding_box)
    assert shown and shown["hidden"] is False and "Passkey unlock required" in shown["text"], shown
    assert abs(shown["height"] - idle["height"]) < 1 and abs(shown["top"] - idle["top"]) < 1, (
        f"the status strip changed size when a message appeared: {idle} -> {shown}"
    )
    assert send_status and abs(send_status["y"] - send_after["y"]) < 1, (
        f"Send button moved when a status message appeared: {send_after} -> {send_status}"
    )
    assert attach_status and abs(attach_status["y"] - attach_after["y"]) < 1, (
        f"Attach button moved when a status message appeared: {attach_after} -> {attach_status}"
    )
    assert shown["textAlign"] == "center", shown
    left_gap = shown["contentLeft"] - shown["left"]
    right_gap = shown["right"] - shown["contentRight"]
    assert shown["contentWidth"] > 0 and abs(left_gap - right_gap) <= 1, (
        f"status message is not centred in the strip: {left_gap:.1f}px left vs "
        f"{right_gap:.1f}px right ({shown})"
    )

    authed_page.evaluate(_SET_STATUS_JS, None)
    cleared = stable_read(lambda: authed_page.evaluate(_STATUS_STRIP_JS))
    send_cleared = stable_read(send.bounding_box)
    assert cleared and cleared["hidden"] is True and cleared["text"] == "", cleared
    assert abs(cleared["height"] - idle["height"]) < 1, (
        f"the status strip changed size when the message cleared: {idle} -> {cleared}"
    )
    assert send_cleared and abs(send_cleared["y"] - send_after["y"]) < 1, (
        f"Send button moved when the status message cleared: {send_after} -> {send_cleared}"
    )

    # #1219 reopened: under the home-indicator inset the strip must be
    # max(row, inset) tall, the inset overlapping the reserved row. The first
    # cut stacked them (row + inset), about 70px of blank card under the
    # composer on the owner's iPhone, twice the gap wanted. Chromium can
    # emulate the inset over CDP, so its leg sees what only the device showed;
    # WebKit has no such override. The probe proves env() really resolved,
    # so a silently ignored override cannot pass as "no stacking".
    if browser_name == "chromium":
        cdp = authed_page.context.new_cdp_session(authed_page)
        cdp.send("Emulation.setSafeAreaInsetsOverride", {"insets": {
            "bottom": _IPHONE_BOTTOM_INSET, "bottomMax": _IPHONE_BOTTOM_INSET}})
        resolved = authed_page.evaluate(_RESOLVED_BOTTOM_INSET_JS)
        assert resolved == _IPHONE_BOTTOM_INSET, (
            f"the emulated safe-area inset did not reach env(): {resolved}px"
        )
        inset = stable_read(lambda: authed_page.evaluate(_STATUS_STRIP_JS))
        expected = max(idle["height"], _IPHONE_BOTTOM_INSET)
        assert inset and abs(inset["height"] - expected) < 1, (
            f"under a {_IPHONE_BOTTOM_INSET}px home-indicator inset the status strip "
            f"is {inset and inset['height']:.1f}px tall, want max(row "
            f"{idle['height']:.1f}px, inset) = {expected:.1f}px: the inset is "
            "stacking under the reserved row instead of overlapping it"
        )


def test_terminal_composer_has_no_mod_enter_send(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    """#1072: Ctrl/Cmd+Enter sends in Chat mode only.

    The binding is opt-in per mount (``sendOnModEnter``) precisely so the
    terminal, which has its own input path, keeps today's behaviour. What this
    pins is that the opt-in stayed opt-in: the shortcut does not deliver here
    (a terminal send is synchronous and clears the box, so an unchanged draft
    is the tell), and the Send title does not advertise a shortcut this mount
    hasn't got — the exact class of lie the old 'Send (with Enter)' title was.
    """
    _open_terminal(authed_page, base_url, launched_pty_session)
    _show_composer(authed_page)
    field = authed_page.locator(INPUT)
    field.fill("not a send here")
    field.press("Control+Enter")
    # Tolerate a browser-default newline from the unhandled chord; the point
    # is that the draft was not delivered and cleared.
    expect(field).to_have_value(re.compile(r"^not a send here\n?$"))
    expect(authed_page.locator(SEND)).to_have_attribute("title", "Send")
