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

# `iphone`: the conftest keeps a WebKit node only for a test carrying it, and timer and
# poll behaviour is engine-dependent: the WebKit rerun of this budget
# (scripts/tools/webkit_budget_check.py) selects nothing without it.
pytestmark = [pytest.mark.smoke, pytest.mark.iphone]

# The one request a hidden page may send: the usage log's `session_end` event,
# flushed once as the page turns hidden so the visit's end is not lost with the
# tab. It is a single beacon on the transition, not a timer, so a hidden minute
# adds no second one.
_HIDDEN_FLUSH = "/api/usage/events"


# The page holds no stream, so this prefix matches nothing. Naming one still makes the
# probe wait out its real-time boot grace before the visible minute: boot() fans out
# agents, status, version, restart and the Life OS reads only after /api/config
# returns, and WebKit announces those requests 10 to 100 ms after the probe saw
# the config settle, so without the grace they land in the minute and count as polls
# (73 to 79 requests against a budget of 70, one run in four).
_BOOT_GRACE_PREFIX = ("/api/-no-held-stream-",)


def test_main_page_keeps_its_request_budget(authed_page: Page, base_url: str) -> None:
    reading = measure_request_budget(authed_page, f"{base_url}/", stream_paths=_BOOT_GRACE_PREFIX)
    assert reading.hidden.pop(_HIDDEN_FLUSH, 0) <= 1, f"the usage flush repeated while hidden: {reading.summary()}"
    assert_request_budget(reading, visible_budget(REPO_ROOT))
