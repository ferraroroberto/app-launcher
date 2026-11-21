"""The PC load sheet's IP Copy puts the address on the real clipboard.

The clipboard is not mocked: the test reads it back after each tap (from a
sentinel, so a stale value cannot pass), because a mocked ``writeText`` proves
only that the handler ran. The sheet is tall (the port-holder rows are
present), as it is on a busy machine, and every tap must leave a toast the
person can see inside the viewport, over the sheet. One open sheet runs every
case: both viewports, a poll rebuild between press and release, a refused
``writeText``, a page with no clipboard API (plain http), and a failed copy.
Chromium only: headless WebKit's clipboard permission is not reliable, and the
iPhone path (``writeText`` in the tap) is pinned by test_header_pc_health.
"""
from __future__ import annotations

import json
import re
import time

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.shell.test_header_cpu_reading import _boot, _reading, _route_load
from tests.e2e.shell.test_header_pc_health import _machine

pytestmark = pytest.mark.smoke

_IP = "192.0.2.44"
_SENTINEL = "not-the-ip"
_BY_HAND = "Could not copy the IP address. Select it and copy by hand."

# Keeps a readable handle, then takes ``navigator.clipboard`` away the way an
# insecure (plain-http) page has none.
_DROP_CLIPBOARD_API = """
(() => {
  const real = navigator.clipboard;
  window.__readClipboard = () => real.readText();
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: undefined });
})()
"""


def _open_sheet(page: Page, base_url: str):
    _route_load(page, {"body": _reading("normal", 20.0)}, [])
    page.route(
        re.compile(r".*/api/system/health$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({
                "verdict": "watch", "why": None, "reason": "Ports are climbing", "fix": "Restart a holder",
                "machine": _machine(55.0, 40.0), "hub_url": None,
            }),
        ),
    )

    def status(route):
        payload = route.fetch().json()
        payload["private_ip"] = _IP
        route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

    page.route(re.compile(r".*/api/status$"), status)
    page.context.grant_permissions(["clipboard-read", "clipboard-write"], origin=base_url)
    _boot(page, base_url)
    page.locator("#paneClaude > .home-head .cpu-reading").click()
    expect(page.locator("#cpuSheet")).to_be_visible()
    row = page.locator("#cpuSheetBody [data-row='ip']")
    expect(row.locator(".usage-sheet-value")).to_have_text(_IP)
    expect(page.locator("#cpuSheetBody [data-row='holder-0']")).to_be_visible()
    return row


def _arm(page: Page, base_url: str, *, write: bool = True) -> None:
    """Put a sentinel on the clipboard, clear the previous case's toast, then
    leave the page with write permission only when ``write``."""
    page.context.grant_permissions(["clipboard-read", "clipboard-write"], origin=base_url)
    page.evaluate(f"navigator.clipboard.writeText({_SENTINEL!r})")
    page.evaluate("document.getElementById('cpuSheetToast').hidden = true")
    if not write:
        page.context.clear_permissions()
        page.context.grant_permissions(["clipboard-read"], origin=base_url)


def _clipboard_becomes_the_ip(page: Page) -> None:
    deadline = time.monotonic() + 5
    value = None
    while time.monotonic() < deadline:
        value = page.evaluate("(window.__readClipboard || (() => navigator.clipboard.readText()))()")
        if value == _IP:
            return
        page.wait_for_timeout(100)
    raise AssertionError(f"the clipboard holds {value!r}, not the address")


def _toast_in_view(page: Page, text: str) -> None:
    toast = page.locator("#cpuSheetToast")
    expect(toast).to_have_text(text)
    expect(toast).to_be_visible()
    viewport = page.viewport_size
    box = toast.bounding_box()
    sheet = page.locator("#cpuSheet .detail-card").bounding_box()
    assert box is not None and sheet is not None
    assert 0 <= box["y"] and box["y"] + box["height"] <= viewport["height"], f"toast off screen: {box}"
    assert 0 <= box["x"] and box["x"] + box["width"] <= viewport["width"], f"toast off screen: {box}"
    assert sheet["y"] <= box["y"] and box["y"] + box["height"] <= sheet["y"] + sheet["height"], (
        f"toast outside the sheet: {box} vs {sheet}"
    )


def _copy_and_expect_the_ip(page: Page, row) -> None:
    row.get_by_role("button", name="Copy").click()
    _toast_in_view(page, "IP address copied")
    _clipboard_becomes_the_ip(page)


def test_copy_puts_the_ip_on_the_clipboard_and_toasts_in_view(
    authed_page: Page, base_url: str, browser_name: str
) -> None:
    if browser_name != "chromium":
        pytest.skip("headless WebKit's clipboard permission is not reliable")
    page = authed_page
    page.set_viewport_size({"width": 1280, "height": 900})
    row = _open_sheet(page, base_url)

    for width, height in ((1280, 900), (412, 915)):
        page.set_viewport_size({"width": width, "height": height})
        _arm(page, base_url)
        _copy_and_expect_the_ip(page, row)

    # The poll rebuilds the sheet's rows between press and release: the button
    # is the same node after, so the release still completes the click.
    _arm(page, base_url)
    copy = row.get_by_role("button", name="Copy")
    box = copy.bounding_box()
    assert box is not None
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.down()
    page.evaluate(
        """() => {
          window.__copyBtn = document.querySelector("#cpuSheetBody [data-row='ip'] button");
          window.__rebuilds = 0;
          new MutationObserver(() => window.__rebuilds++)
            .observe(document.getElementById('cpuSheetBody'), { childList: true });
          document.dispatchEvent(new Event('visibilitychange'));
        }"""
    )
    page.wait_for_function("window.__rebuilds > 0")
    assert page.evaluate(
        "window.__copyBtn === document.querySelector(\"#cpuSheetBody [data-row='ip'] button\")"
    ), "the poll replaced the Copy button"
    page.mouse.up()
    _toast_in_view(page, "IP address copied")
    _clipboard_becomes_the_ip(page)

    # writeText refused (no write permission): the legacy copy still lands it.
    _arm(page, base_url, write=False)
    _copy_and_expect_the_ip(page, row)

    # No clipboard API at all, as on a page served over plain http.
    _arm(page, base_url, write=False)
    page.evaluate(_DROP_CLIPBOARD_API)
    _copy_and_expect_the_ip(page, row)

    # Neither path works: say so, and say to copy it by hand.
    page.evaluate("document.getElementById('cpuSheetToast').hidden = true")
    page.evaluate("document.execCommand = () => false")
    copy.click()
    _toast_in_view(page, _BY_HAND)
    expect(page.locator("#cpuSheetToast")).to_have_class(re.compile(r"\berror\b"))
