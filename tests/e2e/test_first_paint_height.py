"""A tab's first card keeps its height when its data arrives (#1641).

The design review's LAYOUT-10 fails a first card that grows after first paint:
rows or values added when late data lands push everything under it down. Each
test holds the card's endpoints, measures the card with nothing read yet,
releases them, waits for the settled card and compares the two heights. A
card may grow by at most ``MAX_GROWTH_PX`` (the rule's own threshold).

The data is route-mocked and held before navigation (#510), so the first-paint
measurement is deterministic and no real endpoint is read. All names are
fictional.
"""

from __future__ import annotations

import json
import re
import time
from typing import List

import pytest
from playwright.sync_api import Page, Route, expect

from tests.e2e.conftest import stable_eval, wait_until

pytestmark = pytest.mark.smoke

# LAYOUT-10's threshold: growth above this fails the rule.
MAX_GROWTH_PX = 8

_NOW = int(time.time())

def _win(pct: int, reset_s: float, minutes: int) -> dict:
    return {"used_percentage": pct, "resets_at": reset_s, "duration_minutes": minutes}


_QUOTA = {"quota_lines": [
    {"harness": "claude", "provider": "anthropic", "label": "Claude Code",
     "state": "available", "reason": "native_observation", "stale": False,
     "five_hour": _win(39, _NOW + 7200, 300), "weekly": _win(64, _NOW + 302400, 10080)},
    {"harness": "codex", "provider": "openai", "label": "Codex",
     "state": "available", "reason": "native_observation", "stale": False,
     "five_hour": _win(0, _NOW + 9000, 300), "weekly": _win(36, _NOW + 302400, 10080)},
]}

_FILTER = {
    "mode": {"available": True, "mode": "shadow"}, "harnesses": [],
    "stats": {"available": True, "totals": {"tokens_saved": 90000, "rows": 300},
              "today": {"tokens_saved": 12300, "rows": 40},
              "last_7_days": {"tokens_saved": 51000, "rows": 180}, "per_agent": {}},
}


def _job(job_id: str, name: str) -> dict:
    return {
        "id": job_id, "name": name, "target_kind": "python",
        "schedule_chip": "daily 03:00", "next_run": "tomorrow 03:00",
        "next_run_epoch": _NOW + 6 * 3600, "running": False, "stuck": False,
        "paused": False, "confirm": False, "params": [],
        "schedule": {"type": "daily", "at": "03:00"},
        "stats": {"last7": []},
    }


_JOBS = {"jobs": [_job("ledger", "Ledger reconciliation"), _job("mailbox", "Mailbox sweep"),
                  _job("planner", "Planner sync")]}

_AGENDA = {"days": 7, "generated_epoch": _NOW, "occurrences": [
    {"job_id": "ledger", "name": "Ledger reconciliation",
     "fire_epoch": _NOW + 6 * 3600, "cadence": "daily 03:00"},
    {"job_id": "mailbox", "name": "Mailbox sweep",
     "fire_epoch": _NOW + 7 * 3600, "cadence": "daily 03:00"},
    {"job_id": "planner", "name": "Planner sync",
     "fire_epoch": _NOW + 8 * 3600, "cadence": "daily 03:00"},
], "frequent": []}

_APPS = {"scan_root": "C:\\stub", "apps": [
    {"id": "beta-web", "name": "Beta Web", "kind": "webapp",
     "bat_path": "C:\\stub\\beta\\webapp.bat",
     "added_at": "2026-01-01T00:00:00", "autostart": False},
]}

_RUNNING = {"running": [
    {"app_id": "beta-web", "name": "Beta Web", "kind": "webapp", "pid": 4242,
     "started_at": 1736179200, "port": 8601,
     "url": "https://pc.example.invalid:8601/", "alive": True},
]}

_PROBE = {"listeners": [
    {"port": 8700, "pid": 5000, "name": "python.exe", "exe": "python.exe",
     "cmdline": "python -m hub", "app": "Hub", "parent_port": None,
     "service": None, "url": None},
]}


class _Held:
    """An endpoint that answers only once released.

    Every request that arrives before ``release()`` is parked, so the page
    stays at its first paint for as long as the test wants; after it, answers
    go straight through (the card's own polls keep working).
    """

    def __init__(self, page: Page, pattern: str, body: dict) -> None:
        self._body = json.dumps(body)
        self._open = False
        self.parked: List[Route] = []
        page.route(re.compile(pattern), self._handle)

    def _answer(self, route: Route) -> None:
        route.fulfill(status=200, content_type="application/json", body=self._body)

    def _handle(self, route: Route) -> None:
        if self._open:
            self._answer(route)
        else:
            self.parked.append(route)

    def release(self) -> None:
        self._open = True
        parked, self.parked = self.parked, []
        for route in parked:
            self._answer(route)


def _answer(page: Page, pattern: str, body: dict) -> None:
    page.route(
        re.compile(pattern),
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(body)),
    )


def _height(page: Page, selector: str) -> float:
    """The card's rendered height; a card rebuilt mid-read is read again."""
    box = stable_eval(
        page.locator(selector),
        "el => el.isConnected ? el.getBoundingClientRect().height : null",
    )
    assert box, f"{selector} is not rendered"
    return box


def _assert_stable(page: Page, selector: str, held: List[_Held], settled: callable) -> None:
    """Measure at first paint, release, measure again, compare."""
    wait_until(page, lambda: all(h.parked for h in held), "the card's data to be requested")
    first = _height(page, selector)
    for h in held:
        h.release()
    settled()
    after = _height(page, selector)
    assert after - first <= MAX_GROWTH_PX, (
        f"{selector} grew {after - first:.0f}px after first paint "
        f"({first:.0f}px -> {after:.0f}px); LAYOUT-10 allows {MAX_GROWTH_PX}px"
    )


@pytest.mark.iphone
def test_usage_card_keeps_its_height_when_the_quota_arrives(
    authed_page: Page, base_url: str
) -> None:
    page = authed_page
    page.set_viewport_size({"width": 390, "height": 844})
    _answer(page, r".*/api/claude-code/git-status$", {"projects": []})
    held = [_Held(page, r".*/api/rate-limits(\?.*)?$", _QUOTA),
            _Held(page, r".*/api/context-filter$", _FILTER)]
    page.goto(f"{base_url}/", wait_until="domcontentloaded")

    def settled() -> None:
        expect(page.locator("#codingUsage .um-row")).to_have_count(2, timeout=10_000)
        expect(page.locator("#codingUsageFooter")).to_be_visible(timeout=10_000)

    _assert_stable(page, "#codingUsageCard", held, settled)


@pytest.mark.iphone
def test_next_up_card_keeps_its_height_when_the_schedule_arrives(
    authed_page: Page, base_url: str
) -> None:
    page = authed_page
    page.set_viewport_size({"width": 390, "height": 844})
    _answer(page, r".*/api/jobs(\?.*)?$", _JOBS)
    held = [_Held(page, r".*/api/jobs/agenda(\?.*)?$", _AGENDA)]
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.locator("#tabJobs").click()

    def settled() -> None:
        expect(page.locator("#jobsNextUpList li")).to_have_count(3, timeout=10_000)

    _assert_stable(page, "#jobsNextUpCard", held, settled)


@pytest.mark.iphone
@pytest.mark.parametrize(
    "running", [_RUNNING, {"running": []}], ids=["one-running", "nothing-running"])
def test_running_card_keeps_its_height_when_the_apps_arrive(
    authed_page: Page, base_url: str, running: dict
) -> None:
    page = authed_page
    page.set_viewport_size({"width": 390, "height": 844})
    _answer(page, r".*/api/apps$", _APPS)
    held = [_Held(page, r".*/api/apps/running$", running),
            _Held(page, r".*/api/ports/probe$", _PROBE)]
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.locator("#tabApps").click()
    rows = len(running["running"])

    def settled() -> None:
        expect(page.locator("#runningAppsList li.action-row")).to_have_count(rows, timeout=10_000)
        expect(page.locator("#otherPortsMeta")).to_have_text("1 listener not started here",
                                                              timeout=10_000)

    _assert_stable(page, ".running-apps-card", held, settled)
