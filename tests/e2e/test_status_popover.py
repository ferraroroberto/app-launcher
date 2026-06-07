"""Regression pin for issue #139 (Coding tab off-main status popover).

The feature: tapping the Coding tab's ``⎇ status`` button — besides the
existing tile annotation (#115) — opens a compact popover listing one line
per project parked off its default branch, colour-matched to the list
(red name = dirty, yellow = off-main) with the branch tag. A second tap,
or a tap outside, closes it. All-on-default shows a single short note.

Approach mirrors test_git_status_flags.py: real per-project git state
isn't deterministic, so we intercept /api/claude-code/git-status with a
canned payload keyed to the first real coding tile, then assert the
popover DOM. Runs in both projections — the wiring is browser-agnostic
but the iPhone projection confirms the phone surface too.
"""

from __future__ import annotations

import json

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.smoke

_BRANCH = "fix/regress-139"


def test_status_button_opens_off_main_popover(authed_page: Page, base_url: str) -> None:
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")

    authed_page.wait_for_selector(
        ".coding-item, #claudeEmpty:not([hidden])", timeout=10_000
    )
    tiles = authed_page.locator(".coding-item")
    if tiles.count() == 0:
        pytest.skip("no coding projects in this environment — nothing to summarise")

    tile_id = tiles.first.get_attribute("data-id")
    assert tile_id, "first coding tile is missing its data-id"

    # Canned status: this tile is BOTH dirty and off its default branch, so
    # the summary line's name must be red (dirty wins) with the branch tag.
    payload = {
        "projects": [
            {
                "id": tile_id,
                "is_git": True,
                "branch": _BRANCH,
                "default_branch": "main",
                "on_default_branch": False,
                "dirty": True,
            }
        ]
    }
    authed_page.route(
        "**/api/claude-code/git-status",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(payload),
        ),
    )

    summary = authed_page.locator("#gitStatusSummary")
    expect(summary).to_be_hidden()

    # First tap fetches + opens the popover with exactly one off-main row.
    authed_page.locator("#gitStatusBtn").click()
    expect(summary).to_be_visible()

    rows = summary.locator(".git-summary-row")
    expect(rows).to_have_count(1)
    name = rows.first.locator(".git-summary-name")
    classes = name.evaluate("el => el.className")
    assert "git-dirty" in classes, (
        f"dirty project should be red in the summary — class was {classes!r}"
    )
    assert "git-off-main" not in classes, (
        "red must take precedence over yellow when a project is both dirty "
        f"and off-default — class was {classes!r}"
    )
    expect(rows.first.locator(".git-branch-tag")).to_have_text(_BRANCH)

    # Second tap toggles it closed.
    authed_page.locator("#gitStatusBtn").click()
    expect(summary).to_be_hidden()
