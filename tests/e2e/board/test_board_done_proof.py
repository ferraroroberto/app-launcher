"""Board Done lane: Issue / PR / Proof actions; Proof opens a merged UI PR's stored report, a gap says "no proof"."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.fixtures.waits import stable_eval

from tests.e2e.board._support import _board_payload, _iso_utc, _mock_board, _open_board, _unfold

pytestmark = pytest.mark.smoke

REPORT = (
    "<!doctype html><html><body><h1>Walkthrough demo-project PR 21</h1>"
    "<p>Layout lint: clean on every captured leg.</p></body></html>"
)


def _closed(number: int, title: str, closed: datetime, **extra) -> dict:
    return {
        "kind": "issue", "repo": "demo-project", "number": number, "title": title,
        "url": f"u{number}", "updated_at": _iso_utc(closed), "closed_at": _iso_utc(closed),
        "state": "closed", "labels": [], **extra,
    }


@pytest.mark.iphone
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_done_rows_show_proof_no_proof_or_nothing(
    authed_page: Page, base_url: str, theme: str
) -> None:
    now = datetime.now(timezone.utc)
    payload = _board_payload()
    payload["columns"]["done"] = [
        _closed(11, "ui change with a report", now - timedelta(minutes=5), proof="available", proof_pr=21,
                pr=21, pr_url="https://example.invalid/pull/21"),
        _closed(12, "ui change without one", now - timedelta(minutes=10), proof="missing", proof_pr=22,
                pr=22, pr_url="https://example.invalid/pull/22"),
        _closed(13, "docs change", now - timedelta(minutes=15)),
    ]
    _mock_board(authed_page, payload)
    fetched: list = []

    def serve_report(route) -> None:
        fetched.append(route.request.url)
        route.fulfill(status=200, content_type="text/html", body=REPORT)

    authed_page.route(re.compile(r".*/api/board/proof/demo-project/21$"), serve_report)
    authed_page.set_viewport_size({"width": 390, "height": 844})
    _open_board(authed_page, base_url)
    authed_page.evaluate("t => document.documentElement.setAttribute('data-theme', t)", theme)
    _unfold(authed_page, "boardColDone")

    rows = authed_page.locator("#boardColDone li.board-item")
    expect(rows).to_have_count(3)
    with_report, without, plain = rows.nth(0), rows.nth(1), rows.nth(2)
    expect(with_report.locator(".board-proof-chip")).to_have_text("Proof")
    expect(without.locator(".board-proof-none")).to_have_text("no proof")
    expect(without.locator(".board-proof-chip")).to_have_count(0)
    expect(plain.locator(".board-proof-chip, .board-proof-none, .board-done-pr")).to_have_count(0)
    # Issue on every row, PR once a merged PR is known, Proof only with a stored report.
    expect(with_report.locator(".board-done-action .chip")).to_have_text(["Issue", "PR", "Proof"])
    expect(without.locator(".board-done-action .chip")).to_have_text(["Issue", "PR"])
    expect(plain.locator(".board-done-action .chip")).to_have_text(["Issue"])
    # Reading the board fetched no report: it is read on tap only.
    assert fetched == []

    button = with_report.locator(".board-done-action", has=authed_page.locator(".board-proof-chip"))
    # The Board re-renders its rows on every poll: read the box off the live node.
    box = stable_eval(button, "el => { const r = el.getBoundingClientRect();"
                              " return { w: r.width, h: r.height }; }")
    assert box["h"] >= 44 and box["w"] >= 44

    button.click()
    viewer = authed_page.locator("#htmlViewer")
    expect(viewer).to_be_visible()
    expect(authed_page.locator("#htmlViewerName")).to_have_text("demo-project-21.html")
    expect(authed_page.frame_locator("#htmlViewerBody iframe").locator("h1")).to_have_text(
        "Walkthrough demo-project PR 21")
    assert len(fetched) == 1
    assert authed_page.evaluate("() => document.getElementById('htmlViewerBody').firstElementChild"
                                ".getAttribute('sandbox')") == ""
    authed_page.locator("#htmlViewerClose").click()
    expect(viewer).not_to_be_visible()
