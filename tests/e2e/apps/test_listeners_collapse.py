"""Regression pin for issue #480 (Other ports: collapsible child rows).

The feature: a Port-listeners parent row that groups dependent helper
services under it (#224's parent_port nesting) now renders collapsed by
default — just the parent with a rotating chevron — and the whole row is
the tap target that reveals/hides the indented child rows. A listener
with no children keeps today's flat row exactly (no chevron, no tap
affordance). Kill must work from a collapsed parent and from each
expanded child, and the Kill tap must never toggle the collapse.

Approach: the real listener set isn't deterministic across environments,
so we intercept ``/api/ports/probe`` with a canned payload — one parent
with two helper children plus one standalone listener — then assert the
DOM. Runs at two layouts (#1505): the phone's, where the list is in the
Other ports sheet, and the wide one, where the same list sits inline in the
Apps tab's Other ports card. The rows and the confirmed stop are the same
code in both, so every check below runs against each.
"""

from __future__ import annotations

import json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.fixtures.ui import open_row_menu

pytestmark = pytest.mark.smoke

_PROBE_PAYLOAD = {
    "listeners": [
        {
            "port": 8000,
            "pid": 100,
            "name": "python.exe",
            "exe": "python.exe",
            "cmdline": "python -m src.hub",
            "app": "Local LLM Hub",
            "parent_port": None,
            "service": None,
        },
        {
            "port": 8081,
            "pid": 101,
            "name": "python.exe",
            "exe": "python.exe",
            "cmdline": "python -m src.tts_server",
            "app": "Local LLM Hub",
            "parent_port": 8000,
            "service": "src.tts_server",
        },
        {
            "port": 8090,
            "pid": 102,
            "name": "python.exe",
            "exe": "python.exe",
            "cmdline": "python -m src.whisper_proxy",
            "app": "Local LLM Hub",
            "parent_port": 8000,
            "service": "src.whisper_proxy",
        },
        {
            "port": 8501,
            "pid": 103,
            "name": "python.exe",
            "exe": "python.exe",
            "cmdline": "streamlit run app.py",
            "app": "Photo OCR",
            "parent_port": None,
            "service": None,
        },
    ]
}


# The phone layout (the sheet) and the wide one (inline, 1280px with the
# Chromium projection's fine pointer).
_LAYOUTS = {"phone": {"width": 390, "height": 844}, "wide": {"width": 1280, "height": 900}}


@pytest.fixture(params=sorted(_LAYOUTS))
def listeners_page(request, authed_page: Page, base_url: str, browser_name: str) -> Page:
    if request.param == "wide" and browser_name != "chromium":
        pytest.skip("the wide layout needs a fine pointer: desktop Chromium only")
    authed_page.set_viewport_size(_LAYOUTS[request.param])
    authed_page.route(
        "**/api/ports/probe",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(_PROBE_PAYLOAD),
        ),
    )
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    authed_page.locator("#tabApps").click()
    if request.param == "phone":
        # The listeners live in the Other ports sheet since #1437, opened
        # from the Running card's last row.
        authed_page.locator("#otherPortsRow").click()
        expect(authed_page.locator("#otherPortsSheet")).to_be_visible()
    else:
        # Inline under Running on the wide layout (#1505), no sheet.
        expect(authed_page.locator("#otherPortsRow")).to_be_hidden()
        expect(authed_page.locator("#otherPortsCard #listenersList")).to_be_visible()
    authed_page.wait_for_selector("#listenersList .listener-row", timeout=10_000)
    authed_page.listeners_host = (  # type: ignore[attr-defined]
        "#otherPortsSheet" if request.param == "phone" else "#otherPortsCard")
    return authed_page


def _parent_row(page: Page):
    return page.locator("#listenersList .listener-row.expandable")


def _assert_kill_is_last_menu_item(page: Page) -> None:
    """Check the row menu puts the confirmed kill last, after a divider, in the danger colour."""
    # -- was test_no_visible_kill_and_the_menu_puts_danger_last --
    # #1129: nine stacked red Kill buttons were the loudest thing in the app.
    # A destructive action is the least prominent control on a row: the row
    # menu's last item, after a divider, in the danger text colour, and still
    # confirmed (the kill step below accepts that dialog).
    expect(page.locator("#listenersList .button-tint.danger")).to_have_count(0)

    row = page.locator("#listenersList .listener-row").first
    row.locator(".action-row-kebab").click()
    menu = open_row_menu(page)
    expect(menu).to_be_visible()
    last = menu.locator(":scope > *").last
    expect(last).to_have_class(re.compile(r"\brow-menu-danger\b"))
    expect(last).to_have_attribute("data-item", "listener-kill")
    expect(menu.locator(":scope > .row-menu-divider")).to_have_count(1)
    assert menu.evaluate(
        "m => m.lastElementChild.previousElementSibling.classList.contains('row-menu-divider')"
    ), "the destructive item is not separated from the rest by the divider"
    # Close the menu so the collapse steps below start from a clean row.
    page.keyboard.press("Escape")
    expect(menu).to_be_hidden()


@pytest.mark.iphone
def test_listener_rows_collapse_menu_and_kill(listeners_page: Page) -> None:
    """Every listener-row check on one canned probe render (#1215).

    Read-only steps first; the kill (the only step that fires the API) last.
    """
    page = listeners_page

    # -- was test_childless_listener_keeps_flat_row --
    flat = page.locator(
        "#listenersList .listener-row:not(.child):not(.expandable)"
    )
    expect(flat).to_have_count(1)
    expect(flat.locator(".listener-chevron")).to_have_count(0)

    _assert_kill_is_last_menu_item(page)

    # -- was test_parent_collapsed_by_default_and_toggles --
    # Only the grouped parent gets the affordance; children start hidden.
    parent = _parent_row(page)
    expect(parent).to_have_count(1)
    expect(parent).to_have_attribute("aria-expanded", "false")
    expect(parent.locator(".listener-chevron")).to_be_visible()
    expect(page.locator("#listenersList .listener-row.child")).to_have_count(0)

    # Tap reveals the two helper children…
    parent.locator(".action-row-main").click()
    expect(page.locator("#listenersList .listener-row.child")).to_have_count(2)
    expect(_parent_row(page)).to_have_attribute("aria-expanded", "true")

    # …and a second tap collapses them again.
    _parent_row(page).locator(".action-row-main").click()
    expect(page.locator("#listenersList .listener-row.child")).to_have_count(0)
    expect(_parent_row(page)).to_have_attribute("aria-expanded", "false")

    # -- was test_kill_works_collapsed_parent_and_expanded_child (last: fires
    # the kill API) --
    killed_ports: list = []

    def _capture_kill(route) -> None:
        killed_ports.append(route.request.url.rsplit("/", 2)[-2])
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"port": 0, "killed": [1], "errors": []}),
        )

    page.route("**/api/ports/*/kill", _capture_kill)
    # The vendored confirm sheet asks, never native confirm() (#1437).
    native: list = []
    page.on("dialog", lambda d: (native.append(d.message), d.dismiss()))
    confirm = page.locator("#confirmDialog")

    # Stop process sits in the ⋮ menu (#1129): the kebab is the row's sibling
    # control, so using it on the collapsed parent fires the API and must NOT
    # expand the row — the children stay hidden.
    parent = _parent_row(page)
    parent.locator(".action-row-kebab").click()
    open_row_menu(page).locator("[data-item=listener-kill]").click()
    expect(confirm).to_be_visible()
    # Its ✕ is a "no": nothing is killed.
    confirm.locator("#confirmDialogClose").click()
    expect(confirm).to_be_hidden()
    assert killed_ports == [], f"a dismissed confirm still killed {killed_ports!r}"
    parent = _parent_row(page)
    parent.locator(".action-row-kebab").click()
    open_row_menu(page).locator("[data-item=listener-kill]").click()
    confirm.locator("#confirmDialogOk").click()
    expect(page.locator("#listenersList .listener-row.child")).to_have_count(0)
    assert killed_ports == ["8000"], f"parent kill hit {killed_ports!r}"
    # The sheet (or the inline card) the confirm opened over is still up.
    expect(page.locator(page.listeners_host)).to_be_visible()

    # Expand, then kill one child individually.
    _parent_row(page).locator(".action-row-main").click()
    child = page.locator("#listenersList .listener-row.child").first
    child.locator(".action-row-kebab").click()
    open_row_menu(page).locator("[data-item=listener-kill]").click()
    confirm.locator("#confirmDialogOk").click()
    expect(confirm).to_be_hidden()
    assert killed_ports == ["8000", "8081"], f"child kill hit {killed_ports!r}"
    assert native == [], f"a native dialog fired: {native!r}"
