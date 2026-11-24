"""The main page keeps the fleet's request budget on a fake clock.

The probe (`_request_budget.py`, the scaffold's) loads the page, runs one minute
visible and one minute hidden, and counts every API request by path. The visible
budget is `.fleet.toml` `[request_budget] visible_requests_per_minute`; a missing
key is unknown (`RequestBudgetUnknown`), never a pass.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page

from tests.e2e.budget._request_budget import (
    assert_request_budget,
    measure_request_budget,
    visible_budget,
)
from tests.e2e.fixtures.env import REPO_ROOT

pytestmark = pytest.mark.smoke

# The one request a hidden page may send: the usage log's `session_end` event,
# flushed once as the page turns hidden so the visit's end is not lost with the
# tab. It is a single beacon on the transition, not a timer, so a hidden minute
# adds no second one.
_HIDDEN_FLUSH = "/api/usage/events"


def test_main_page_keeps_its_request_budget(authed_page: Page, base_url: str) -> None:
    reading = measure_request_budget(authed_page, f"{base_url}/")
    assert reading.hidden.pop(_HIDDEN_FLUSH, 0) <= 1, f"the usage flush repeated while hidden: {reading.summary()}"
    assert_request_budget(reading, visible_budget(REPO_ROOT))
