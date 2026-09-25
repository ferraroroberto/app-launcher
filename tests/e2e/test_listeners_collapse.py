"""Regression pin for issue #480 (Port listeners: collapsible child rows).

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
DOM. Runs in both projections — the wiring is browser-agnostic but the
iPhone projection confirms the phone surface too.
"""

from __future__ import annotations

import json
import re

import pytest
from playwright.sync_api import Page, expect

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


@pytest.fixture()
def listeners_page(authed_page: Page, base_url: str) -> Page:
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
    # The Port-listeners panel is collapsed by default (#383) — open it so
    # the rows are visible/clickable.
    authed_page.locator("#paneApps details.listeners-card").evaluate(
        "el => { el.open = true; }"
    )
    authed_page.wait_for_selector("#listenersList .listener-row", timeout=10_000)
    return authed_page


def _parent_row(page: Page):
    return page.locator("#listenersList .listener-row.expandable")


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

    # -- was test_no_visible_kill_and_the_menu_puts_danger_last --
    # #1129: nine stacked red Kill buttons were the loudest thing in the app.
    # A destructive action is the least prominent control on a row: the row
    # menu's last item, after a divider, in the danger text colour, and still
    # confirmed (the kill step below accepts that dialog).
    expect(page.locator("#listenersList .button-tint.danger")).to_have_count(0)

    row = page.locator("#listenersList .listener-row").first
    row.locator(".action-row-kebab").click()
    menu = row.locator(".row-menu")
    expect(menu).to_be_visible()
    last = menu.locator(":scope > *").last
    expect(last).to_have_class(re.compile(r"\brow-menu-danger\b"))
    expect(last).to_have_class(re.compile(r"\blistener-kill\b"))
    expect(menu.locator(":scope > .row-menu-divider")).to_have_count(1)
    assert menu.evaluate(
        "m => m.lastElementChild.previousElementSibling.classList.contains('row-menu-divider')"
    ), "the destructive item is not separated from the rest by the divider"
    # Close the menu so the collapse steps below start from a clean row.
    page.keyboard.press("Escape")
    expect(menu).to_be_hidden()

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
    page.on("dialog", lambda d: d.accept())

    # Stop process sits in the ⋮ menu (#1129): the kebab is the row's sibling
    # control, so using it on the collapsed parent fires the API and must NOT
    # expand the row — the children stay hidden.
    parent = _parent_row(page)
    parent.locator(".action-row-kebab").click()
    parent.locator(".listener-kill").click()
    expect(page.locator("#listenersList .listener-row.child")).to_have_count(0)
    assert killed_ports == ["8000"], f"parent kill hit {killed_ports!r}"

    # Expand, then kill one child individually.
    _parent_row(page).locator(".action-row-main").click()
    child = page.locator("#listenersList .listener-row.child").first
    child.locator(".action-row-kebab").click()
    child.locator(".listener-kill").click()
    assert killed_ports == ["8000", "8081"], f"child kill hit {killed_ports!r}"
