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
from playwright.sync_api import Page

pytestmark = pytest.mark.smoke


def test_mirror_page_keeps_close_marker_in_document_title(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    sid = launched_pty_session
    # Loopback access auto-enters mirror mode: /api/status returns
    # {reachable: true, reason: 'loopback'}, which terminal.js picks up
    # at line 244-245 to flip isMirror = true.
    authed_page.goto(f"{base_url}/?terminal={sid}", wait_until="domcontentloaded")
    authed_page.wait_for_selector("#terminalOverlay:not([hidden])", timeout=10_000)

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
        resp = route.fetch()
        body = resp.json()
        term = dict(body.get("terminal") or {})
        term.update({"reachable": True, "reason": "tailnet"})
        body["terminal"] = term
        route.fulfill(json=body)

    authed_page.route("**/api/status", to_tailnet)
    authed_page.goto(f"{base_url}/?terminal={sid}", wait_until="domcontentloaded")
    authed_page.wait_for_selector("#terminalOverlay:not([hidden])", timeout=10_000)

    marker = f"app-launcher-mirror-{sid}"
    authed_page.wait_for_function(
        f"() => document.title.endsWith({marker!r})",
        timeout=5_000,
    )
