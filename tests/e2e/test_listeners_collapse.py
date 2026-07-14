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


def test_parent_collapsed_by_default_and_toggles(listeners_page: Page) -> None:
    page = listeners_page

    # Only the grouped parent gets the affordance; children start hidden.
    parent = _parent_row(page)
    expect(parent).to_have_count(1)
    expect(parent).to_have_attribute("aria-expanded", "false")
    expect(parent.locator(".listener-chevron")).to_be_visible()
    expect(page.locator("#listenersList .listener-row.child")).to_have_count(0)

    # Tap reveals the two helper children…
    parent.click()
    expect(page.locator("#listenersList .listener-row.child")).to_have_count(2)
    expect(_parent_row(page)).to_have_attribute("aria-expanded", "true")

    # …and a second tap collapses them again.
    _parent_row(page).click()
    expect(page.locator("#listenersList .listener-row.child")).to_have_count(0)
    expect(_parent_row(page)).to_have_attribute("aria-expanded", "false")


def test_childless_listener_keeps_flat_row(listeners_page: Page) -> None:
    page = listeners_page

    flat = page.locator(
        "#listenersList .listener-row:not(.child):not(.expandable)"
    )
    expect(flat).to_have_count(1)
    expect(flat.locator(".listener-chevron")).to_have_count(0)


def test_kill_works_collapsed_parent_and_expanded_child(
    listeners_page: Page,
) -> None:
    page = listeners_page
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

    # Kill on the collapsed parent fires the API and must NOT expand the row
    # (stopPropagation) — the children stay hidden.
    _parent_row(page).locator("button").click()
    expect(page.locator("#listenersList .listener-row.child")).to_have_count(0)
    assert killed_ports == ["8000"], f"parent kill hit {killed_ports!r}"

    # Expand, then kill one child individually.
    _parent_row(page).click()
    child = page.locator("#listenersList .listener-row.child").first
    child.locator("button").click()
    assert killed_ports == ["8000", "8081"], f"child kill hit {killed_ports!r}"
