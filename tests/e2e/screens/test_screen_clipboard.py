"""Regression pin for #1719: Paste to PC, from the Screen view's keys bar.

Two keys send the phone's clipboard text to the PC: Paste to PC sets the PC's
clipboard, Paste + Ctrl+V sets it and then presses Ctrl+V, but only once the
server says the copy landed. If the phone refuses the read, a small sheet takes
the text by hand. Nothing is sent in View, while the picture is not live, for an
empty clipboard or for text over the cap.

Nothing here touches a real clipboard or a real PC: ``navigator.clipboard`` is a
mock (headless WebKit's clipboard permissions are not reliable, the same reason
as ``test_jobs_log_copy.py``), the synthetic text is invented, and the stub
socket plays the server's ``clipboard`` replies. The stub is the one
``test_screen_view`` uses.
"""
from __future__ import annotations

import json
import re
from typing import Dict, List

import pytest
from playwright.sync_api import Page, WebSocketRoute, expect

from tests.e2e.fixtures.waits import wait_until
from tests.e2e.screens.test_screen_view import (
    _frame,
    _open_from_sheet,
    _set_mode,
    _stub_screen,
)

pytestmark = [pytest.mark.smoke, pytest.mark.iphone]

_TEXT = "synthetic clipboard line ñ€ one"

# The phone's clipboard: what readText gives, whether it refuses, how often it
# was asked. defineProperty because navigator.clipboard is non-writable in some
# WebKit contexts.
_CLIPBOARD_MOCK = """
(() => {
  window.__clip = { text: %s, refuse: false, reads: 0 };
  Object.defineProperty(navigator, 'clipboard', {
    configurable: true,
    value: {
      readText: async () => {
        window.__clip.reads += 1;
        if (window.__clip.refuse) throw new DOMException('denied', 'NotAllowedError');
        return window.__clip.text;
      },
    },
  });
})()
""" % json.dumps(_TEXT)

_OK = {"type": "clipboard", "direction": "to_pc", "ok": True}


def _open_bar(authed_page: Page, base_url: str):
    """The live view in Control with the keys bar up; ``(seen, sockets, page)``."""
    sockets: List[WebSocketRoute] = []

    def on_connect(ws: WebSocketRoute) -> None:
        sockets.append(ws)
        ws.send(_frame())

    authed_page.context.add_init_script(_CLIPBOARD_MOCK)
    seen = _stub_screen(authed_page.context, on_connect)
    page = _open_from_sheet(authed_page, base_url)
    expect(page.locator("#screenImage")).to_be_visible()
    wait_until(page, lambda: len(seen["acks"]) >= 1, "the frame's ack")
    _set_mode(page, "control")
    page.locator("#screenKeyboard").click()
    expect(page.locator("#screenKeys")).to_be_visible()
    return seen, sockets, page


def _clipboard_sets(seen: Dict[str, List]) -> List[dict]:
    return [m for m in seen["inputs"] if m.get("type") == "clipboard_set"]


def _reads(page: Page) -> int:
    return page.evaluate("window.__clip.reads")


def _reply(sockets: List[WebSocketRoute], body: dict) -> None:
    sockets[-1].send(json.dumps(body))


def test_paste_to_pc_sends_the_phone_text_once_and_never_presses_a_key(
    authed_page: Page, base_url: str
) -> None:
    seen, sockets, page = _open_bar(authed_page, base_url)
    key = page.locator('#screenKeys [data-keys-page="main"] [data-clip="set"]')
    notice = page.locator("#screenNotice")
    expect(notice).to_be_hidden()

    key.click()
    wait_until(page, lambda: _clipboard_sets(seen), "the clipboard_set")
    assert _clipboard_sets(seen) == [{"type": "clipboard_set", "text": _TEXT}]
    assert _reads(page) == 1

    _reply(sockets, _OK)
    expect(notice).to_have_text("Sent to the PC clipboard")
    expect(notice).not_to_have_class(re.compile(r"error"))
    # It is the main key: it sets the clipboard and presses nothing.
    page.wait_for_timeout(300)
    assert [m for m in seen["inputs"] if m.get("type") == "key"] == []

    # An empty clipboard and one over the cap send nothing.
    seen["inputs"].clear()
    page.evaluate("window.__clip.text = ''")
    key.click()
    expect(notice).to_have_text("The phone's clipboard has no text")

    page.evaluate("window.__clip.text = 'a'.repeat(64 * 1024 + 1)")
    key.click()
    expect(notice).to_have_text("That text is too long to send (64 KB at most).")
    expect(notice).to_have_class("screen-notice error")
    page.wait_for_timeout(300)
    assert _clipboard_sets(seen) == []

    # Exactly at the cap goes.
    page.evaluate("window.__clip.text = 'a'.repeat(64 * 1024)")
    key.click()
    wait_until(page, lambda: _clipboard_sets(seen), "the at-cap clipboard_set")
    assert len(_clipboard_sets(seen)[0]["text"]) == 64 * 1024

    # The second page carries the same key.
    seen["inputs"].clear()
    page.evaluate("window.__clip.text = %s" % json.dumps(_TEXT))
    keys = page.locator("#screenKeys")
    keys.locator('[data-page-to="fn"]').click()
    fn_key = keys.locator('[data-keys-page="fn"] [data-clip="set"]')
    expect(fn_key).to_be_visible()
    fn_key.click()
    wait_until(page, lambda: _clipboard_sets(seen), "the clipboard_set from the second page")
    assert _clipboard_sets(seen)[0]["text"] == _TEXT


def test_paste_and_ctrl_v_waits_for_the_copy_a_refused_read_takes_text_by_hand_and_the_keys_fit(
    authed_page: Page, base_url: str
) -> None:
    """One keys bar, in turn: Paste + Ctrl+V waits for the copy, a refused
    read opens the by-hand sheet, both paste keys fit a narrow phone, and
    nothing is read or sent once the picture is not live."""
    seen, sockets, page = _open_bar(authed_page, base_url)
    _paste_and_ctrl_v_leg(seen, sockets, page)
    seen["inputs"].clear()
    _refused_read_leg(seen, sockets, page)
    _paste_keys_fit_leg(page)
    seen["inputs"].clear()
    _not_live_leg(seen, sockets, page)


def _paste_and_ctrl_v_leg(seen: Dict[str, List], sockets: List[WebSocketRoute], page: Page) -> None:
    """Paste + Ctrl+V presses Ctrl+V only after the copy landed."""
    key = page.locator('#screenKeys [data-keys-page="main"] [data-clip="paste"]')
    notice = page.locator("#screenNotice")

    key.click()
    wait_until(page, lambda: _clipboard_sets(seen), "the clipboard_set")
    # Not yet: the PC has not said the copy landed.
    page.wait_for_timeout(300)
    assert [m for m in seen["inputs"] if m.get("type") == "key"] == []

    _reply(sockets, _OK)
    wait_until(page, lambda: seen["inputs"][-1].get("type") == "key", "Ctrl+V")
    assert seen["inputs"][-1] == {"type": "key", "key": "v", "mods": ["ctrl"]}
    expect(notice).to_have_text("Pasted on the PC")

    # A copy that fails (the PC's clipboard is locked) never pastes whatever
    # the PC's clipboard held before.
    seen["inputs"].clear()
    key.click()
    wait_until(page, lambda: _clipboard_sets(seen), "the second clipboard_set")
    _reply(sockets, {**_OK, "ok": False, "reason": "busy"})
    expect(notice).to_have_text("The PC's clipboard is busy. Try again.")
    expect(notice).to_have_class("screen-notice error")
    page.wait_for_timeout(300)
    assert [m for m in seen["inputs"] if m.get("type") == "key"] == []


def _refused_read_leg(seen: Dict[str, List], sockets: List[WebSocketRoute], page: Page) -> None:
    """A refused read opens a sheet that sends the text by hand."""
    page.evaluate("window.__clip.refuse = true")
    sheet = page.locator("#screenPasteSheet")
    expect(sheet).to_be_hidden()

    # From the Paste-to-PC key, an empty sheet's Send sends nothing.
    page.locator('#screenKeys [data-keys-page="main"] [data-clip="set"]').click()
    expect(sheet).to_be_visible()
    sheet.locator(".detail-actions button").click()
    expect(sheet).to_be_hidden()
    page.wait_for_timeout(300)
    assert _clipboard_sets(seen) == []

    page.locator('#screenKeys [data-keys-page="main"] [data-clip="paste"]').click()
    expect(sheet).to_be_visible()
    # The modal contract: a header close and one full-width primary action.
    expect(sheet.locator(".detail-close")).to_be_visible()
    primary = sheet.locator(".detail-actions button")
    expect(primary).to_have_count(1)
    expect(primary).to_have_text("Send to PC")
    assert _clipboard_sets(seen) == []

    # Close sends nothing.
    sheet.locator(".detail-close").click()
    expect(sheet).to_be_hidden()
    assert _clipboard_sets(seen) == []

    # Open again, paste by hand, send: the same message, and the Ctrl+V the
    # key that opened the sheet asked for follows the copy's reply.
    page.locator('#screenKeys [data-keys-page="main"] [data-clip="paste"]').click()
    expect(sheet).to_be_visible()
    sheet.locator("textarea").fill("typed by hand ñ")
    primary.click()
    expect(sheet).to_be_hidden()
    wait_until(page, lambda: _clipboard_sets(seen), "the hand-pasted clipboard_set")
    assert _clipboard_sets(seen) == [{"type": "clipboard_set", "text": "typed by hand ñ"}]
    _reply(sockets, _OK)
    wait_until(page, lambda: seen["inputs"][-1].get("type") == "key", "Ctrl+V after the sheet")
    assert seen["inputs"][-1] == {"type": "key", "key": "v", "mods": ["ctrl"]}
    page.evaluate("window.__clip.refuse = false")


_LABELS_JS = """() => [...document.querySelectorAll('#screenKeys [data-clip]')]
  .filter(e => e.offsetParent !== null)
  .map(e => { const r = e.getBoundingClientRect();
    return { label: e.textContent, w: r.width, h: r.height, l: r.left, r: r.right,
             clipped: e.scrollWidth > e.clientWidth + 1 }; })"""


def _paste_keys_fit_leg(page: Page) -> None:
    """Both paste keys fit a narrow phone on either key page; ends on the first page."""
    keys = page.locator("#screenKeys")
    for width, height in ((320, 640), (360, 740), (844, 390)):
        page.set_viewport_size({"width": width, "height": height})
        for target in ("main", "fn"):
            # Each page's toggle is on the other page: flip only when needed.
            toggle = keys.locator(f'[data-page-to="{target}"]:visible')
            if toggle.count():
                toggle.click()
            expect(keys.locator(f'[data-keys-page="{target}"]')).not_to_be_hidden()
            boxes: list = []

            def two() -> bool:
                nonlocal boxes
                boxes = page.evaluate(_LABELS_JS)
                return len(boxes) == 2

            wait_until(page, two, "both paste keys")
            assert all(b["h"] >= 44 and b["w"] >= 43.5 for b in boxes), (width, target, boxes)
            assert all(b["l"] >= -0.5 and b["r"] <= width + 0.5 for b in boxes), (width, target, boxes)
            assert not any(b["clipped"] for b in boxes), (width, target, boxes)
    keys.locator('[data-page-to="main"]:visible').click()
    expect(keys.locator('[data-keys-page="main"]')).not_to_be_hidden()


def _not_live_leg(seen: Dict[str, List], sockets: List[WebSocketRoute], page: Page) -> None:
    """Nothing is read or sent while the picture is not live."""
    reads = _reads(page)
    _reply(sockets, {"type": "status", "state": "unavailable"})
    keys = page.locator('#screenKeys [data-keys-page="main"] [data-clip]')
    expect(keys.first).to_be_disabled()
    expect(keys.last).to_be_disabled()
    keys.first.click(force=True)
    keys.last.click(force=True)
    page.wait_for_timeout(300)
    assert _clipboard_sets(seen) == []
    assert _reads(page) == reads, "the phone's clipboard was read for a refused transfer"
