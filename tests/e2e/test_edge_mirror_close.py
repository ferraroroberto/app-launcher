"""Regression pin for the JS half of commit b946bc8 / issue #20 (mirror-window close).

The bug: Stop & Close from the phone couldn't dismiss the Edge ``--app``
mirror window on the PC. The fix has two halves:

  * Python (already covered): launcher.py polls EnumWindows for a top-
    level window whose title contains a unique marker, then PostMessage
    WM_CLOSE on Stop. Verified by ``tests/test_launcher_mirror_hwnd.py``
    (16 tests with mocked win32gui).
  * JS (this test): ``terminal.js`` sets ``document.title`` to
    ``app-launcher-mirror-<sid>`` when the page is in mirror mode, so
    EnumWindows has something to match on.

If the JS half regresses (someone refactors the title assignment away),
EnumWindows never finds the HWND, WM_CLOSE is never posted, and the
Edge mirror lingers — but the Python side keeps passing in isolation
because it's testing the polling loop with mocked title strings. This
test closes that gap.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page

pytestmark = pytest.mark.smoke


def test_mirror_page_sets_unique_document_title(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    sid = launched_pty_session
    # Loopback access auto-enters mirror mode: /api/status returns
    # {reachable: true, reason: 'loopback'}, which terminal.js picks up
    # at line 244-245 to flip isMirror = true.
    authed_page.goto(f"{base_url}/?terminal={sid}", wait_until="domcontentloaded")
    authed_page.wait_for_selector("#terminalOverlay:not([hidden])", timeout=10_000)

    expected = f"app-launcher-mirror-{sid}"
    authed_page.wait_for_function(
        f"() => document.title === {expected!r}",
        timeout=5_000,
    )
