"""WebKit-projection pin for commit 696b723 (iPhone index revalidation).

The bug: iOS Safari (especially PWA-installed) used to serve a stale
``index.html`` and request a ``?v=<old hash>`` script that no longer
existed. The fix added ``Cache-Control: no-cache, must-revalidate`` to
the index response so Safari issues a conditional GET on every load.

This test is a thin WebKit-specific check that the header actually
reaches the browser through the real network stack (i.e. it isn't
stripped by a proxy/middleware ordering bug). The non-browser
``test_cache_busting`` already pins the header at the HTTP level for
both projections; this one runs through the rendering engine so a
WebKit-specific regression surfaces here.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, Response

pytestmark = pytest.mark.smoke


def test_index_cache_control_visible_to_webkit(
    authed_page: Page, base_url: str, browser_name: str
) -> None:
    if browser_name != "webkit":
        pytest.skip("WebKit projection only (iOS Safari is the original regression)")

    captured: dict = {}

    def _on_response(res: Response) -> None:
        # First navigation response, which is the / document. Later
        # /api/* and /static/* responses also fire this handler but
        # we only care about the HTML root.
        if "cache-control" in captured:
            return
        url = res.url.rstrip("/")
        if url == base_url.rstrip("/") or url == base_url.rstrip("/") + "/":
            captured["cache-control"] = res.headers.get("cache-control", "")
            captured["status"] = res.status

    authed_page.on("response", _on_response)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    authed_page.wait_for_selector("#sessionsList", state="attached", timeout=5_000)

    assert captured.get("status") == 200, (
        f"GET / returned {captured.get('status')!r} under WebKit"
    )
    cc = captured.get("cache-control", "")
    assert "no-cache" in cc and "must-revalidate" in cc, (
        f"WebKit saw Cache-Control={cc!r} on /; the iPhone-stale-index fix "
        "(commit 696b723) regressed or was stripped by middleware ordering"
    )
