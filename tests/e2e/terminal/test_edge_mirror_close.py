"""Regression pin for the JS half of commit b946bc8 / issue #20 (mirror-window close).

The bug: Stop & Close from the phone couldn't dismiss the Edge ``--app``
mirror window on the PC. The fix has two halves:

  * Python (already covered): launcher.py polls EnumWindows for a top-
    level window whose title contains a unique marker, then PostMessage
    WM_CLOSE on Stop. Verified by ``tests/unit/launching/test_launcher_mirror_hwnd.py``
    (16 tests with mocked win32gui).
  * JS (this test): ``terminal.js`` keeps the ``app-launcher-mirror-<sid>``
    marker in ``document.title`` when the page is in mirror mode, so
    EnumWindows has something to match on.

If the JS half regresses (someone refactors the title assignment away),
EnumWindows never finds the HWND, WM_CLOSE is never posted, and the
Edge mirror lingers — but the Python side keeps passing in isolation
because it's testing the polling loop with mocked title strings. This
test closes that gap.

Since #266 the mirror title also **leads with the human session name** (for
the Windows/PTI title bar), e.g. ``"fix the login bug — app-launcher-mirror-
<sid>"``. The launcher's scan matches the marker as a *substring*
(``marker in title``), so the contract this pins is that the marker still
**trails** the title intact — not that it is the whole title.

The loopback case (a plain ``?terminal=<sid>`` mirror) is asserted by
``test_terminal_light_theme.py::test_open_terminal_restyles_live_on_theme_flip``
since #1215, which opens that exact deep link on the same stub PTY. The legs
here are the origins and boot paths that need their own routes; they share one
page, one PTY and two switchable routes, and each leg is a former test kept as
a named helper. The first leg is the desktop row-tap that opens a mirror at all
(#282, #1273), formerly ``test_desktop_session_mirror.py``. Desktop only: the
WebKit projection is an iPhone, whose row-tap opens the terminal in-page
(pinned by ``test_stop_unify_and_terminal_kill.py``).
"""

from __future__ import annotations

import re
from typing import Optional

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.fixtures.waits import OVERLAY_OPEN_MS

pytestmark = pytest.mark.smoke

# Issue #940: the marker must not depend on boot() getting past /api/config.
# These legs never open a real terminal, so a synthetic sid is enough.
_SIGNED_OUT_SID = "s-signed-out-940"


class _Routes:
    """``/api/config`` and ``/api/status`` answers each leg switches between.

    ``config``: ``None`` passes through, ``"401"`` signs the page out, and
    ``"window"`` turns on "Open sessions in a new window" (off by default,
    #1955). ``terminal``: ``None`` passes through, else the terminal
    reachability ``/api/status`` reports.
    """

    def __init__(self, page: Page) -> None:
        self.config: Optional[str] = None
        self.terminal: Optional[dict] = None
        page.route(re.compile(r".*/api/config$"), self._config)
        page.route("**/api/status", self._status)

    def _config(self, route) -> None:
        if self.config == "401":
            route.fulfill(status=401, json={"detail": "auth required"})
        elif self.config == "window":
            resp = route.fetch()
            route.fulfill(response=resp, json={**resp.json(), "open_sessions_in_window": True,
                                               "open_sessions_in_chat": True})
        else:
            route.continue_()

    def _status(self, route) -> None:
        if self.terminal is None:
            route.continue_()
        else:
            # Only the terminal discriminator is under test.  A proxying
            # route.fetch() adds a second request and can lose the boot-time
            # status response under full-suite load, leaving state.status unset.
            route.fulfill(json={"terminal": self.terminal})


def _leg_mirror_marker_applies_on_tailnet_origin(
    page: Page, base_url: str, routes: _Routes, sid: str
) -> None:
    """Regression pin for issue #371: the ts.net-hosted mirror window.

    With a Tailscale LE cert active, ``mirror_url`` spawns the Edge ``--app``
    window on the ts.net host — where ``/api/status`` reports
    ``{reachable: true, reason: 'tailnet'}``, not ``'loopback'``. The mirror
    discriminator (``isMirrorWindowSession``) required ``'loopback'``
    exactly, so every ts.net mirror window skipped ``announceMirrorWindow``:
    no title marker (EnumWindows can't find it on Stop & Close) and no
    shutdown-frame self-close — orphan windows piled up on the desktop.

    The e2e webapp is loopback-bound, so simulate the ts.net origin by
    rewriting ``/api/status``'s terminal reachability to the tailnet reason
    — the exact field the discriminator reads.
    """
    routes.config, routes.terminal = None, {"reachable": True, "reason": "tailnet"}
    page.goto(f"{base_url}/?terminal={sid}", wait_until="domcontentloaded")
    page.wait_for_selector("#terminalOverlay:not([hidden])", timeout=OVERLAY_OPEN_MS)

    # The bare marker alone proves nothing: boot() stamps it from the
    # ?terminal= link before /api/status is read, and nothing drops it on a
    # reachable origin. Only announceMirrorWindow, which runs only when the
    # tailnet reason counts as a mirror origin, leads it with the human title.
    marker = f"app-launcher-mirror-{sid}"
    expect(page).to_have_title(re.compile(rf".+ — {re.escape(marker)}$"))


def _leg_signed_out_mirror_still_carries_close_marker(
    page: Page, base_url: str, routes: _Routes
) -> None:
    """A ?terminal= mirror whose /api/config 401s stops boot() early and never
    reaches announceMirrorWindow. The marker has to be in place before that
    call, or Stop & Close and the orphan sweep cannot find the window."""
    routes.config, routes.terminal = "401", None
    page.goto(f"{base_url}/?terminal={_SIGNED_OUT_SID}", wait_until="domcontentloaded")
    # The sign-in sheet is what a 401 raises, so boot() has already returned.
    expect(page.locator("dialog.sign-in")).to_be_visible()
    expect(page).to_have_title(f"app-launcher-mirror-{_SIGNED_OUT_SID}")


def _leg_signed_out_non_mirror_page_stays_unmarked(
    page: Page, base_url: str, routes: _Routes
) -> None:
    """The human-shareable ?session= link and a plain open are never mirrors
    (#241/#877); the early marker must not land on them either."""
    routes.config, routes.terminal = "401", None
    for query in (f"?session={_SIGNED_OUT_SID}", ""):
        page.goto(f"{base_url}/{query}", wait_until="domcontentloaded")
        expect(page.locator("dialog.sign-in")).to_be_visible()
        expect(page).to_have_title("Launcher")


def _leg_terminal_link_on_unreachable_origin_sheds_early_marker(
    page: Page, base_url: str, routes: _Routes
) -> None:
    """A ?terminal= page on an origin that cannot reach the terminal (the
    public tunnel) is not a mirror. It must drop the boot-time marker, or the
    orphan sweep could close a window that is not ours."""
    routes.config, routes.terminal = None, {"reachable": False, "reason": "tunnel-only"}
    page.goto(f"{base_url}/?terminal={_SIGNED_OUT_SID}", wait_until="domcontentloaded")
    expect(page.locator("#terminalOverlay")).to_be_visible(timeout=OVERLAY_OPEN_MS)
    expect(page).to_have_title("Launcher")


def _leg_desktop_row_tap_opens_chat_then_mirrors_once_terminal_is_chosen(
    page: Page, base_url: str, routes: _Routes, sid: str
) -> None:
    """Desktop row-tap with "Open sessions in a new window" on: Terminal mirrors out.

    #282: on a desktop browser (``pointer: fine``) a session's *terminal* is
    the dedicated PC Edge ``--app`` mirror window a new-session launch opens,
    so it can be closed without fear while the session keeps running, never a
    terminal inside the user's own browser. The row-tap then POSTs
    ``/api/claude-code/sessions/<sid>/mirror`` and ``#terminalOverlay`` stays
    hidden. The in-page default with the switch off is pinned by
    ``tests/e2e/session/test_open_session_settings.py``.

    #1273: a session with a transcript reader opens in **Chat** by default on
    the desktop, in-page, with no mirror request. Terminal is the explicit
    choice, remembered per session (``launcher.sessionModes``); once a session
    was last viewed in Terminal, its row-tap mirrors out as before.

    The disposable autoboot harness is identified by
    ``LAUNCHER_SESSION_HOST_PORT`` (set only by the e2e/verify gate), so the
    route reports ``mirrored: true`` *without* spawning a real Edge window or
    touching the desktop — same isolation rule the orphan-mirror sweep follows
    (issue #278). It runs first: a ``?terminal=`` open remembers Terminal for
    the session, which would turn the first tap below into a mirror.
    """
    routes.config, routes.terminal = "window", None
    mirror_posts: list = []
    page.on(
        "request",
        lambda r: mirror_posts.append(r.url)
        if r.method == "POST" and f"/api/claude-code/sessions/{sid}/mirror" in r.url else None,
    )

    page.goto(base_url, wait_until="domcontentloaded")
    pty_row = page.locator(f'#sessionsList li.session-item[data-session-id="{sid}"]')
    expect(pty_row).to_be_visible(timeout=8_000)

    # First open, nothing remembered: Chat, in-page, no mirror window (#1273).
    pty_row.locator(".session-open").click()
    overlay = page.locator("#terminalOverlay")
    expect(overlay).to_be_visible()
    expect(page.locator("#sessionModeChat")).to_have_attribute("aria-pressed", "true")
    assert mirror_posts == [], f"a Chat open must not mirror: {mirror_posts}"

    # Choosing Terminal is remembered for this session; back to the list.
    page.locator("#sessionModeTerminal").click()
    expect(page.locator("#sessionModeTerminal")).to_have_attribute("aria-pressed", "true")
    page.locator("#terminalBack").click()
    expect(overlay).to_be_hidden()

    # The next tap resolves to Terminal, which on a desktop is the mirror
    # window (#282): the row POSTs /mirror and the in-page overlay stays shut.
    with page.expect_response(
        lambda r: r.request.method == "POST"
        and f"/api/claude-code/sessions/{sid}/mirror" in r.url
    ) as resp_info:
        pty_row.locator(".session-open").click()

    resp = resp_info.value
    assert resp.ok, f"mirror POST failed: HTTP {resp.status}"
    assert resp.json().get("mirrored") is True, (
        "a desktop Terminal open must mirror to a PC window (#282)"
    )
    expect(overlay).to_be_hidden()


def test_mirror_marker_lands_only_on_mirror_windows(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    page = authed_page
    sid = launched_pty_session
    routes = _Routes(page)

    _leg_desktop_row_tap_opens_chat_then_mirrors_once_terminal_is_chosen(
        page, base_url, routes, sid
    )
    _leg_mirror_marker_applies_on_tailnet_origin(page, base_url, routes, sid)
    _leg_signed_out_mirror_still_carries_close_marker(page, base_url, routes)
    _leg_signed_out_non_mirror_page_stays_unmarked(page, base_url, routes)
    _leg_terminal_link_on_unreachable_origin_sheds_early_marker(page, base_url, routes)
