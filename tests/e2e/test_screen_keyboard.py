"""Regression pin for #1723: a hardware keyboard drives the PC from the Screen view.

The hidden input only ever saw ``beforeinput``, so a hardware keyboard's
arrows, Esc, Tab, F-keys and Ctrl shortcuts never left the page. A ``keydown``
handler on the view now maps them to the existing ``key`` message
(``hardwareKey`` in ``screen-gestures.js``, pinned in plain Node by
``tests/js/screen_gestures.test.mjs``): each reaches the PC once, a plain
printable character stays with ``beforeinput`` so typed text is not doubled,
Cmd goes out as Ctrl, and nothing is sent in View, while the picture is not
live, or during an IME composition. The desktop pop-out window is the same
view (``/?screen=1``).

Disposable webapp and the stub socket only: the keys are Playwright's, the PC
is the recorded list of input messages, and nothing touches a real desktop.
"""
from __future__ import annotations

import json
from typing import List

import pytest
from playwright.sync_api import Page, WebSocketRoute, expect

from tests.e2e.conftest import wait_until
from tests.e2e.test_screen_view import (
    _frame,
    _live_view,
    _open_from_sheet,
    _set_mode,
    _stub_screen,
)

pytestmark = [pytest.mark.smoke, pytest.mark.iphone]

# A cancelable keydown aimed at the view, as the browser would deliver one:
# dispatchEvent returns false when the handler cancelled it.
_DISPATCH_KEYDOWN_JS = """([init]) => {
  const ev = new KeyboardEvent('keydown', { bubbles: true, cancelable: true, ...init });
  const target = document.activeElement && document.getElementById('screenView').contains(document.activeElement)
    ? document.activeElement : document.getElementById('screenView');
  return { cancelled: !target.dispatchEvent(ev) };
}"""


def _keys(seen) -> List[str]:
    """The key messages the PC got, as ``ctrl+c`` style strings."""
    return [
        "+".join(m.get("mods", []) + [m["key"]])
        for m in seen["inputs"]
        if m.get("type") == "key"
    ]


def _expect_keys(page: Page, seen, expected: List[str], what: str) -> None:
    """Wait for ``expected`` to be exactly what arrived: a doubled key is a
    different list, and the marker pressed last proves nothing queued behind."""
    wait_until(page, lambda: len(_keys(seen)) >= len(expected), what)
    assert _keys(seen) == expected


def test_hardware_keys_reach_the_pc_once_and_typed_text_is_not_doubled(
    authed_page: Page, base_url: str
) -> None:
    seen, page = _live_view(authed_page, base_url)
    _set_mode(page, "control")
    page.evaluate("window.__stillHere = true")

    for press in ("ArrowUp", "ArrowLeft", "Tab", "Escape", "F5", "Control+c", "Control+v", "Meta+c", "Shift+Tab"):
        page.keyboard.press(press)
    page.keyboard.press("F12")
    _expect_keys(
        page,
        seen,
        ["up", "left", "tab", "esc", "f5", "ctrl+c", "ctrl+v", "ctrl+c", "shift+tab", "f12"],
        "the hardware keys",
    )
    # Cmd is Ctrl, never Win; nothing else rode along.
    assert not any("win" in m.get("mods", []) for m in seen["inputs"])
    # The view cancelled what it sent: Esc did not close it, F5 did not reload.
    expect(page.locator("#screenView")).to_be_visible()
    assert page.evaluate("window.__stillHere") is True

    # With the keys bar up the hidden input has focus: typed text goes out
    # once through beforeinput, and Enter / Backspace once through keydown.
    page.locator("#screenKeyboard").click()
    expect(page.locator("#screenKeyInput")).to_be_focused()
    before = len(seen["inputs"])
    page.keyboard.type("ab")
    page.keyboard.press("Enter")
    page.keyboard.press("Backspace")
    page.keyboard.press("Control+a")
    wait_until(page, lambda: len(seen["inputs"]) >= before + 5, "the typed text and keys")
    tail = seen["inputs"][before:]
    assert "".join(m.get("text", "") for m in tail) == "ab"
    assert [m["key"] for m in tail if m["type"] == "key"] == ["enter", "backspace", "a"]
    assert len(tail) == 5, tail  # two characters, three keys: nothing doubled

    # A sticky bar modifier still combines with a hardware key.
    page.locator('#screenKeys [data-mod="shift"]').click()
    page.keyboard.press("ArrowDown")
    wait_until(page, lambda: seen["inputs"][-1].get("key") == "down", "Shift+Down")
    assert seen["inputs"][-1] == {"type": "key", "key": "down", "mods": ["shift"]}
    expect(page.locator('#screenKeys [data-mod="shift"]')).to_have_attribute("aria-pressed", "false")


def test_no_hardware_key_is_sent_in_view_when_not_live_or_while_composing(
    authed_page: Page, base_url: str
) -> None:
    sockets: List[WebSocketRoute] = []

    def on_connect(ws: WebSocketRoute) -> None:
        sockets.append(ws)
        ws.send(_frame())

    seen = _stub_screen(authed_page.context, on_connect)
    page = _open_from_sheet(authed_page, base_url)
    expect(page.locator("#screenImage")).to_be_visible()
    wait_until(page, lambda: len(seen["acks"]) >= 1, "the frame's ack")

    # View mode: not sent, and not cancelled either, so the browser keeps F5.
    for key in ("ArrowUp", "F5", "Escape"):
        init = {"key": key}
        assert page.evaluate(_DISPATCH_KEYDOWN_JS, [init])["cancelled"] is False, key
    assert page.evaluate(_DISPATCH_KEYDOWN_JS, [{"key": "c", "ctrlKey": True}])["cancelled"] is False

    _set_mode(page, "control")
    # An IME composition is the input's business, not a key press.
    assert page.evaluate(_DISPATCH_KEYDOWN_JS, [{"key": "Enter", "isComposing": True}])["cancelled"] is False
    assert page.evaluate(_DISPATCH_KEYDOWN_JS, [{"key": "Process", "keyCode": 229}])["cancelled"] is False
    # A plain printable character is beforeinput's, not keydown's.
    assert page.evaluate(_DISPATCH_KEYDOWN_JS, [{"key": "a"}])["cancelled"] is False

    # The picture goes stale: the same keys are not sent, and not cancelled.
    sockets[-1].send(json.dumps({"type": "status", "state": "unavailable"}))
    expect(page.locator("#screenState")).to_have_attribute("data-state", "stale")
    assert page.evaluate(_DISPATCH_KEYDOWN_JS, [{"key": "F5"}])["cancelled"] is False
    page.keyboard.press("ArrowUp")

    # Live again, in Control: the first key that arrives is the marker, so
    # nothing above was queued or sent.
    sockets[-1].send(_frame())
    expect(page.locator("#screenState")).not_to_have_attribute("data-state", "stale")
    assert page.evaluate(_DISPATCH_KEYDOWN_JS, [{"key": "F8"}])["cancelled"] is True
    _expect_keys(page, seen, ["f8"], "the marker key")


def test_the_pop_out_window_takes_hardware_keys_too(
    authed_page: Page, base_url: str
) -> None:
    seen = _stub_screen(authed_page.context)
    authed_page.goto(f"{base_url}/?screen=1", wait_until="domcontentloaded")
    expect(authed_page.locator("#screenView")).to_be_visible()
    expect(authed_page.locator("#screenImage")).to_be_visible()
    wait_until(authed_page, lambda: len(seen["acks"]) >= 1, "the frame's ack")
    _set_mode(authed_page, "control")

    for press in ("ArrowRight", "Escape", "F5", "Control+c", "Meta+v", "End"):
        authed_page.keyboard.press(press)
    _expect_keys(
        authed_page,
        seen,
        ["right", "esc", "f5", "ctrl+c", "ctrl+v", "end"],
        "the pop-out window's keys",
    )
    expect(authed_page.locator("#screenView")).to_be_visible()
