"""The main page keeps the fleet's request budget on a fake clock.

The probe (`_request_budget.py`, the scaffold's) loads the page, runs one minute
visible and one minute hidden, and counts every API request by path. The visible
budget is `.fleet.toml` `[request_budget] visible_requests_per_minute`; a missing
key is unknown (`RequestBudgetUnknown`), never a pass.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from playwright.sync_api import Page

from tests.e2e.budget import _request_budget
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

# The requests a hidden page may send, once each: the usage log's `session_end`
# event, and any buffered timing rows (a loaded box makes a read slow enough to
# leave one). Both are flushed as the page turns hidden so they are not lost with
# the tab: one send on the transition, not a timer, so a second one is a failure.
_HIDDEN_FLUSHES = ("/api/usage/events", "/api/perf/timings")


# The page holds no stream, so this prefix matches nothing. Naming one still makes the
# probe wait out its real-time boot grace before the visible minute: boot() fans out
# agents, status, version, restart and the Life OS reads only after /api/config
# returns, and WebKit announces those requests 10 to 100 ms after the probe saw
# the config settle, so without the grace they land in the minute and count as polls
# (73 to 79 requests against a budget of 70, one run in four).
_BOOT_GRACE_PREFIX = ("/api/-no-held-stream-",)


class _StoppedClockCounter(_request_budget._Counter):
    """Counts each minute on a stopped page clock and ends it on quiet traffic.

    The probe moves its count to "hidden" before it hides the page, and every poll
    and the 60 s ledger throttle come due as the visible minute ends. On a running
    clock a poll can fire in that gap, and under load a read started while visible
    (or one chained on its answer) is counted there too. Stopped, a timer fires
    only inside `advance`.
    """

    def advance(self, total_ms: int, step_ms: int) -> None:
        # The stop must lie ahead of the page's time; 2 s covers the round trips.
        now = self.page.evaluate("Date.now()") / 1000
        self.page.clock.pause_at(datetime.fromtimestamp(now + 2))
        super().advance(total_ms, step_ms)
        self.wait_for_boot()


def test_main_page_keeps_its_request_budget(
    authed_page: Page, base_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_request_budget, "_Counter", _StoppedClockCounter)
    reading = measure_request_budget(authed_page, f"{base_url}/", stream_paths=_BOOT_GRACE_PREFIX)
    for path in _HIDDEN_FLUSHES:
        assert reading.hidden.pop(path, 0) <= 1, f"{path} repeated while hidden: {reading.summary()}"
    assert_request_budget(reading, visible_budget(REPO_ROOT))
