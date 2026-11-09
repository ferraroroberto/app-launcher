"""Shared driver for the read-aloud rows in the session bar's ⋮ menu.

The rows are built on every open, so a test that wants the Summarize row first
waits for the hub probe to settle: the app sets ``data-hub`` on ``#terminalMenu``
when it has (``up`` / ``down``), and nothing else tells a test the probe is over.
"""

from __future__ import annotations

from playwright.sync_api import Locator, Page

MENU = "#terminalMenu"
READ_ITEM = '.terminal-menu [data-item="overlay-read-aloud-btn"]'
SUMMARIZE_ITEM = '.terminal-menu [data-item="overlay-summarize-read-btn"]'


def stub_hub_health(page: Page, *, available: bool = True) -> None:
    """Answer the hub probe, so the ⋮ menu's rows don't depend on the host's
    real hub: register this before the session overlay opens."""
    page.route(
        "**/api/tts/health",
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body='{"available": %s}' % ("true" if available else "false"),
        ),
    )


def wait_for_hub_probe(page: Page, state: str = "up") -> None:
    """Block until the hub probe for the open session settled as ``state``."""
    page.wait_for_selector(f'{MENU}[data-hub="{state}"]', state="attached", timeout=10_000)


def open_menu(page: Page) -> None:
    """Open the ⋮ menu (it re-evaluates every row on open)."""
    page.locator(MENU).click()


def read_aloud_item(page: Page) -> Locator:
    open_menu(page)
    return page.locator(READ_ITEM)

