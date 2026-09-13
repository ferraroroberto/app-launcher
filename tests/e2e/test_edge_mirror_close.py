"""Regression pin for the JS half of commit b946bc8 / issue #20 (mirror-window close).

The bug: Stop & Close from the phone couldn't dismiss the Edge ``--app``
mirror window on the PC. The fix has two halves:

  * Python (already covered): launcher.py polls EnumWindows for a top-
    level window whose title contains a unique marker, then PostMessage
    WM_CLOSE on Stop. Verified by ``tests/test_launcher_mirror_hwnd.py``
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
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import OVERLAY_OPEN_MS

pytestmark = pytest.mark.smoke


def test_mirror_page_keeps_close_marker_in_document_title(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    sid = launched_pty_session
    # Loopback access auto-enters mirror mode: /api/status returns
    # {reachable: true, reason: 'loopback'}, which terminal.js picks up
    # at line 244-245 to flip isMirror = true.
    authed_page.goto(f"{base_url}/?terminal={sid}", wait_until="domcontentloaded")
    authed_page.wait_for_selector("#terminalOverlay:not([hidden])", timeout=OVERLAY_OPEN_MS)

    # The marker must remain at the tail of the title (a human name may lead,
    # issue #266) so the launcher's substring EnumWindows scan still finds and
    # closes the Edge --app window.
    marker = f"app-launcher-mirror-{sid}"
    authed_page.wait_for_function(
        f"() => document.title.endsWith({marker!r})",
        timeout=5_000,
    )


def test_mirror_marker_applies_on_tailnet_origin(
    authed_page: Page, base_url: str, launched_pty_session: str
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
    sid = launched_pty_session

    def to_tailnet(route):
        # Only the terminal discriminator is under test.  A proxying
        # route.fetch() adds a second request and can lose the boot-time status
        # response under full-suite WebKit load, leaving state.status unset.
        route.fulfill(
            json={"terminal": {"reachable": True, "reason": "tailnet"}}
        )

    authed_page.route("**/api/status", to_tailnet)
    authed_page.goto(f"{base_url}/?terminal={sid}", wait_until="domcontentloaded")
    authed_page.wait_for_selector("#terminalOverlay:not([hidden])", timeout=OVERLAY_OPEN_MS)

    marker = f"app-launcher-mirror-{sid}"
    authed_page.wait_for_function(
        f"() => document.title.endsWith({marker!r})",
        timeout=5_000,
    )


# Issue #940: the marker must not depend on boot() getting past /api/config.
# These pages never open a real terminal, so a synthetic sid is enough.
_SIGNED_OUT_SID = "s-signed-out-940"


def _config_401(route) -> None:
    route.fulfill(status=401, json={"detail": "auth required"})


def test_signed_out_mirror_still_carries_close_marker(
    authed_page: Page, base_url: str
) -> None:
    """A ?terminal= mirror whose /api/config 401s stops boot() early and never
    reaches announceMirrorWindow. The marker has to be in place before that
    call, or Stop & Close and the orphan sweep cannot find the window."""
    authed_page.route("**/api/config", _config_401)
    authed_page.goto(
        f"{base_url}/?terminal={_SIGNED_OUT_SID}", wait_until="domcontentloaded"
    )
    # The login overlay is what a 401 raises, so boot() has already returned.
    expect(authed_page.locator("#loginOverlay")).to_be_visible()
    expect(authed_page).to_have_title(f"app-launcher-mirror-{_SIGNED_OUT_SID}")


@pytest.mark.parametrize(
    "query", [f"?session={_SIGNED_OUT_SID}", ""], ids=["shared-link", "plain"]
)
def test_signed_out_non_mirror_page_stays_unmarked(
    authed_page: Page, base_url: str, query: str
) -> None:
    """The human-shareable ?session= link and a plain open are never mirrors
    (#241/#877); the early marker must not land on them either."""
    authed_page.route("**/api/config", _config_401)
    authed_page.goto(f"{base_url}/{query}", wait_until="domcontentloaded")
    expect(authed_page.locator("#loginOverlay")).to_be_visible()
    expect(authed_page).to_have_title("Launcher")


def test_terminal_link_on_unreachable_origin_sheds_early_marker(
    authed_page: Page, base_url: str
) -> None:
    """A ?terminal= page on an origin that cannot reach the terminal (the
    public tunnel) is not a mirror. It must drop the boot-time marker, or the
    orphan sweep could close a window that is not ours."""

    def via_tunnel(route):
        route.fulfill(
            json={"terminal": {"reachable": False, "reason": "tunnel-only"}}
        )

    authed_page.route("**/api/status", via_tunnel)
    authed_page.goto(
        f"{base_url}/?terminal={_SIGNED_OUT_SID}", wait_until="domcontentloaded"
    )
    expect(authed_page.locator("#terminalOverlay")).to_be_visible(
        timeout=OVERLAY_OPEN_MS
    )
    expect(authed_page).to_have_title("Launcher")
