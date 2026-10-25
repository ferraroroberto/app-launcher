"""Page-driving helpers shared by the browser tests: session rows, Settings sheets, row menus."""

from __future__ import annotations

import json
import re
import time

from playwright.sync_api import Page, expect

from tests.e2e.fixtures.waits import OVERLAY_OPEN_MS


# --------------------------------------------------- opening a session row
#
# #1025 removed the running-sessions row's actions gear, which used to be the
# one-tap way into a specific mode (its menu held Terminal / Chat / Rename /
# Stop). The row is now a single tappable button, so a test that wants a mode
# or a session action goes in through the overlay the tap opens. These two
# helpers are that path, shared so the ~7 files that used the gear don't each
# grow their own version.


def stub_session_mirror(page: Page) -> None:
    """Answer the PC-mirror POST with ``mirrored: false``.

    On a **desktop** browser (``pointer: fine`` — the Chromium projection,
    never the iPhone one) a tap on a *full-control* row opens a dedicated PC
    Edge window instead of the in-page overlay (#282). ``mirrored: false`` is
    the server's own "mirroring is disabled" answer and ``openSession``'s
    supported fall-through to the in-page overlay, so stubbing it is what lets
    one row-tap test run on both projections rather than skipping Chromium.

    A test whose subject *is* the mirror must not use this — see
    ``test_desktop_session_mirror.py``.
    """
    page.route(
        re.compile(r".*/api/claude-code/sessions/[^/]+/mirror$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json", body='{"mirrored": false}'
        ),
    )


def open_session_row(page: Page, row, mode: str | None = None) -> None:
    """Tap a running-sessions row and leave the overlay open in ``mode``.

    ``mode`` (``"terminal"`` / ``"chat"``) taps the bar's segmented toggle
    after the overlay opens; ``None`` accepts whatever mode the row opened in
    (a full-control row's last-viewed, a detached one's Chat). Call
    ``stub_session_mirror`` first when the row is full-control and the test
    runs on the Chromium projection.

    Uses the pool-wide ``OVERLAY_OPEN_MS`` budget (#887), never a literal.
    """
    row.locator(".session-open").click()
    page.wait_for_selector("#terminalOverlay:not([hidden])", timeout=OVERLAY_OPEN_MS)
    if mode is not None:
        seg = "#sessionModeChat" if mode == "chat" else "#sessionModeTerminal"
        page.locator(seg).click()
        expect(page.locator("#terminalOverlay")).to_have_attribute("data-mode", mode)


def open_settings(page: Page) -> None:
    """Open Settings from the current tab's page-header gear (#1131)."""
    page.locator(".pane:not([hidden]) .settings-open-btn").first.click()
    expect(page.locator("#paneSettings")).to_be_visible()


def open_settings_sheet(page: Page, sheet_id: str) -> None:
    """Open Settings and tap the row that opens ``sheet_id`` (#1435).

    ``sheet_id`` is a top-level sheet (``foldersSheet``, ``chiefSheet``,
    ``launchDefaultsSheet`` …). For one agent's sheet use
    ``open_agent_sheet``. Settings is opened first unless it already shows.
    """
    if not page.locator("#paneSettings").is_visible():
        open_settings(page)
    page.locator(f'#paneSettings [data-sheet="{sheet_id}"]').click()
    expect(page.locator(f"#{sheet_id}")).to_be_visible()


def open_agent_sheet(page: Page, agent_id: str) -> None:
    """Open Settings › Launch defaults › ``agent_id`` (#1435)."""
    open_settings_sheet(page, "launchDefaultsSheet")
    page.locator(f'#launchDefaultsSheet [data-agent-sheet="{agent_id}"]').click()
    expect(page.locator(f'#agentSheet [data-agent-group="{agent_id}"]')).to_be_visible()


def close_settings_sheets(page: Page) -> None:
    """Close whichever Settings sheet is open with its Done (#1435), or its
    header ✕ when the primary has a job of its own (Screen time's Send to
    task-os, #1678).

    An open modal sheet makes the rest of the page inert, the nav included,
    so a test that taps a tab or a row after a sheet must close it first.
    """
    sheet = page.locator("dialog.settings-sheet[open]")
    if sheet.count():
        done = sheet.locator(".detail-actions [data-sheet-close]")
        (done if done.count() else sheet.locator(".detail-header .detail-close")).click()
    expect(page.locator("dialog.settings-sheet[open]")).to_have_count(0)


def life_launch_switch(page: Page, switch_id: str) -> None:
    """Flip one of Life's launch switches (``#lifeOsDetached`` /
    ``#lifeOsResume``), which live in the model combo's menu since #1503:
    open the menu, tap the switch (the menu stays open), close it again with
    the trigger.

    Waits for the boot ``/api/config`` read to have landed first (#1702): it
    rebuilds the model list through ``setOptions``, which closes an open menu,
    so a menu opened before it lands is closed under the click on the switch
    ("element is not stable", then "not visible"). The launch toolbar mounts
    with one placeholder option; the catalog's second one is the signal."""
    menu = page.locator("#lifeOsModelMenu")
    expect(menu.locator("[role=option]").nth(1)).to_be_attached()
    page.locator("#lifeOsModelBtn").click()
    expect(menu).to_be_visible()
    page.locator(switch_id).click()
    expect(menu).to_be_visible()
    page.locator("#lifeOsModelBtn").click()
    expect(menu).to_be_hidden()


def open_row_menu(page: Page):
    """The open row menu (any ⋮ kebab's). The vendored row-menu (#1565)
    mounts a menu only while it is open, on <body> (or the open <dialog>),
    never inside its row, so a row-scoped locator cannot reach it. At most
    one is open at a time, so this page-wide locator is that one."""
    return page.locator(".row-menu")


class HeldUploads:
    """Route-mock an upload endpoint and answer each request when the test says.

    The composer's attach queue (#1354) is a run of sequential requests; a
    stub that answers at once leaves no moment in which "Uploading 2 of 3" is
    on screen. Each request is parked here instead, and the test settles it
    with :meth:`ok` / :meth:`fail` — so the state between two uploads is held
    still for as long as an assertion needs, with no sleep to tune. Requests
    keep their arrival order, and :attr:`count` is how many have arrived.
    """

    def __init__(self, page: Page, pattern: "re.Pattern[str]") -> None:
        self._page = page
        self.routes: list = []
        page.route(pattern, lambda route: self.routes.append(route))

    @property
    def count(self) -> int:
        return len(self.routes)

    def wait_for(self, n: int, timeout_s: float = 15.0) -> None:
        """Block until ``n`` requests have arrived (each poll lets the page run)."""
        deadline = time.monotonic() + timeout_s
        while len(self.routes) < n:
            if time.monotonic() > deadline:
                raise AssertionError(f"only {len(self.routes)} of {n} uploads arrived")
            self._page.wait_for_timeout(50)

    def ok(self, index: int, path: str) -> None:
        self.routes[index].fulfill(
            status=200, content_type="application/json", body=json.dumps({"path": path}),
        )

    def fail(self, index: int, detail: str) -> None:
        self.routes[index].fulfill(
            status=400, content_type="application/json", body=json.dumps({"detail": detail}),
        )
