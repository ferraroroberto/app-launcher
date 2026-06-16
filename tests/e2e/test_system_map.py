"""Regression pin for issue #173 (fleet system map as a launcher surface).

The feature: the Code tab carries a foldable **🗺️ System map** section —
a ``<details>`` mirroring the other Coding-tab panels — that lazy-loads the
claude-config ``architecture/system-map.png`` on first expand and opens it
full-screen (lightbox) on tap. The section hides unless
``/api/system-map/status`` reports the PNG exists.

Both endpoints are stubbed via ``page.route`` so the test is deterministic
regardless of whether a claude-config checkout (and a rendered map) is
present on the host — on CI there is none.

Runs in both projections — the wiring is browser-agnostic but the iPhone
projection confirms the phone surface too.
"""

from __future__ import annotations

import base64

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.smoke

# A real 1x1 PNG so the served <img> decodes and gains intrinsic size
# (Playwright visibility needs a laid-out box, not just a present element).
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def _route_map(page: Page, *, available: bool = True) -> None:
    page.route(
        "**/api/system-map/status",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body='{"available": %s, "claude_config_dir": "X"}'
            % ("true" if available else "false"),
        ),
    )
    page.route(
        "**/api/system-map/image",
        lambda route: route.fulfill(
            status=200, content_type="image/png", body=_PNG
        ),
    )


def test_section_sits_between_projects_and_settings(
    authed_page: Page, base_url: str
) -> None:
    _route_map(authed_page, available=True)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")

    card = authed_page.locator("details.system-map-card#systemMapCard")
    expect(card).to_be_visible()

    # Document order: Projects → System map → Settings. compareDocumentPosition
    # returns DOCUMENT_POSITION_FOLLOWING (4) when the argument follows `this`.
    order_ok = authed_page.evaluate(
        """() => {
            const projects = document.querySelector('details.projects-card');
            const map = document.querySelector('#systemMapCard');
            const settings = document.querySelector('#settingsPanel');
            const after = (a, b) =>
                !!(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
            return after(projects, map) && after(map, settings);
        }"""
    )
    assert order_ok, "System map must sit after Projects and before Settings"


def test_section_hidden_when_unavailable(
    authed_page: Page, base_url: str
) -> None:
    _route_map(authed_page, available=False)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    expect(authed_page.locator("#systemMapCard")).to_be_hidden()


def test_map_loads_on_expand_and_zooms(
    authed_page: Page, base_url: str
) -> None:
    _route_map(authed_page, available=True)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")

    card = authed_page.locator("#systemMapCard")
    expect(card).to_be_visible()
    img = authed_page.locator("#systemMapImage")

    # Lazy-load: the image only fetches once the panel is expanded.
    expect(img).to_be_hidden()
    authed_page.locator("#systemMapCard .coding-summary-title").click()
    expect(img).to_be_visible()

    # Tap the inline map → full-screen lightbox; tap it again → dismiss.
    lightbox = authed_page.locator("#systemMapLightbox")
    expect(lightbox).to_be_hidden()
    img.click()
    expect(lightbox).to_be_visible()
    lightbox.click()
    expect(lightbox).to_be_hidden()
