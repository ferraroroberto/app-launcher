"""Every Life OS e2e test starts from an empty recap ledger."""

from __future__ import annotations

import json as _json

import pytest
from playwright.sync_api import Page

from tests.e2e.life_os._support import _ARCHIVED_URL, _SEARCH_URL, _mock_recap


@pytest.fixture(autouse=True)
def _default_recap(authed_page: Page) -> None:
    """Stub /api/life-os/recap-status for every test so opening the Life OS tab
    is hermetic — without this the live endpoint answers (life-os is checked
    out beside the repo), unhiding the recap tile asynchronously and reflowing
    the list mid-measurement, which jitters the #124 tile-geometry assertion.
    Default is ``available:false`` → the recap tile stays hidden, so tests that
    aren't about the recap see the exact pre-feature layout. The two recap
    tests register their own ``_mock_recap`` after this; Playwright matches
    routes last-registered-first, so that one wins."""
    _mock_recap(authed_page, available=False)
    # The skills field searches conversations from two letters (#1504): a
    # default stub, so no test's typing reaches a real conversation index.
    # Tests that search register their own, which wins (last-registered).
    authed_page.route(_SEARCH_URL, lambda route: route.fulfill(
        status=200, content_type="application/json",
        body=_json.dumps({"available": True, "query": "", "skill": "", "results": []})))
    # Nor the zombie cleanup's real backup: nothing archived by default.
    authed_page.route(_ARCHIVED_URL, lambda route: route.fulfill(
        status=200, content_type="application/json", body=_json.dumps({"archived": []})))
