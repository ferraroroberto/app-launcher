"""Board Done lane: "Show earlier" reads one more local day per tap, under day headers."""

from __future__ import annotations

import json as _json
import re
from datetime import date, datetime, timedelta, timezone

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.fixtures.waits import flush_requests, wait_until

from tests.e2e.board._support import _board_payload, _iso_utc, _mock_board, _open_board, _unfold

pytestmark = pytest.mark.smoke


def _closed(number: int, title: str, closed: datetime) -> dict:
    return {
        "kind": "issue", "repo": "app-launcher", "number": number, "title": title,
        "url": f"u{number}", "updated_at": _iso_utc(closed), "closed_at": _iso_utc(closed),
        "state": "closed", "labels": [],
    }


def _serve_days(page: Page, calls: list) -> None:
    """GET /api/board/done?days_back=N: two rows for each day asked for."""
    def handle(route) -> None:
        n = int(re.search(r"days_back=(\d+)", route.request.url).group(1))
        calls.append(n)
        noon = datetime.combine(date.today() - timedelta(days=n), datetime.min.time()) \
            .astimezone().replace(hour=12).astimezone(timezone.utc)
        items = [] if n == 3 else [
            _closed(100 * n + 2, f"day {n} second", noon),
            _closed(100 * n + 1, f"day {n} first", noon - timedelta(hours=3)),
        ]
        route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({
                "days_back": n, "items": items,
                "day": (date.today() - timedelta(days=n)).isoformat(),
            }),
        )

    page.route(re.compile(r".*/api/board/done\?.*$"), handle)


@pytest.mark.iphone
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_show_earlier_adds_one_day_per_tap_without_overflowing(
    authed_page: Page, base_url: str, theme: str
) -> None:
    now = datetime.now(timezone.utc)
    payload = _board_payload()
    payload["columns"]["done"] = [_closed(7, "closed today", now - timedelta(minutes=5))]
    _mock_board(authed_page, payload)
    calls: list = []
    _serve_days(authed_page, calls)
    authed_page.set_viewport_size({"width": 390, "height": 844})
    _open_board(authed_page, base_url)
    authed_page.evaluate("t => document.documentElement.setAttribute('data-theme', t)", theme)
    _unfold(authed_page, "boardColDone")

    done = authed_page.locator("#boardColDone")
    rows = done.locator("li.board-item")
    more = done.locator(".board-done-more")
    heads = done.locator(".board-day-head:visible")
    # Today alone: no header to tell it from, nothing read yet, one tap away.
    expect(rows).to_have_count(1)
    expect(heads).to_have_count(0)
    expect(more).to_have_text("Show earlier")
    assert calls == []

    more.click()
    expect(heads).to_have_text(["Today", "Yesterday"])
    expect(rows).to_have_count(3)
    more.click()
    expect(heads).to_have_count(3)
    two_days_ago = date.today() - timedelta(days=2)
    expect(heads.nth(2)).to_have_text(
        re.compile(rf"^[A-Z][a-z]{{2}},? {two_days_ago.day} [A-Z][a-z]{{2}}$"))
    expect(rows).to_have_count(5)
    # A day with nothing closed says so under its own header.
    more.click()
    expect(done.locator('.board-day[data-day="%s"] .board-day-none'
                        % (date.today() - timedelta(days=3)).isoformat())).to_have_text("Nothing closed.")
    # Each tap read exactly one day, in order, and the poll read none.
    flush_requests(authed_page)
    assert calls == [1, 2, 3]

    # The newest close leads within its day.
    expect(done.locator('.board-day[data-day="%s"] .action-row-title'
                        % (date.today() - timedelta(days=1)).isoformat())).to_have_text(
        ["day 1 second", "day 1 first"])
    # Under a day header the stamp is the clock time, not the date again.
    expect(done.locator('.board-day[data-day="%s"] .action-row-meta'
                        % (date.today() - timedelta(days=1)).isoformat()).first).to_have_text(
        "app-launcher #102 · closed · 12:00")
    wait_until(authed_page, lambda: authed_page.evaluate(
        "() => document.documentElement.scrollWidth <= window.innerWidth"),
        "no horizontal page overflow at 390px")
    assert authed_page.evaluate(
        "() => { const el = document.getElementById('boardColDone');"
        " return el.scrollWidth <= el.clientWidth; }")


@pytest.mark.iphone
@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [390, 1600])
def test_show_earlier_button_lines_up_with_the_cards(
    authed_page: Page, base_url: str, theme: str, width: int
) -> None:
    payload = _board_payload()
    payload["columns"]["done"] = [_closed(7, "closed today", datetime.now(timezone.utc))]
    _mock_board(authed_page, payload)
    _serve_days(authed_page, [])
    authed_page.set_viewport_size({"width": width, "height": 900})
    _open_board(authed_page, base_url)
    authed_page.evaluate("t => document.documentElement.setAttribute('data-theme', t)", theme)
    _unfold(authed_page, "boardColDone")

    done = authed_page.locator("#boardColDone")
    more = done.locator(".board-done-more")
    expect(more).to_be_visible()
    # On the desktop grid the lane body has no sides, so the button takes the
    # card inset itself; the phone's body already supplies it.
    edges = authed_page.evaluate(
        """() => {
            const b = document.querySelector('#boardColDone .board-done-more').getBoundingClientRect();
            const r = document.querySelector('#boardColDone li.board-item').getBoundingClientRect();
            return {left: b.left - r.left, right: r.right - b.right};
        }""")
    # The grid layout is what drops the body's sides; the iPhone projection
    # keeps the phone layout whatever its viewport width.
    grid = authed_page.evaluate(
        "() => getComputedStyle(document.querySelector('#boardColDone > .collapse-body')).paddingLeft === '0px'")
    inset = 14 if grid else 0
    assert abs(edges["left"] - inset) <= 1 and abs(edges["right"] - inset) <= 1, edges
