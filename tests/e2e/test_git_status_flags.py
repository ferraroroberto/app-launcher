"""Regression pin for issue #115 (Coding tab on-demand git-status flags).

The feature: tapping the Coding tab's git-status button fetches
/api/claude-code/git-status and colours each tile — red for a dirty tree,
yellow for a non-default branch (red wins when both, but the branch tag
still shows) — plus reveals a legend. Nothing runs until the tap.

Approach: the real per-project git state isn't deterministic across
environments, so we intercept the endpoint with a canned payload keyed to
the first real coding tile, then assert the DOM annotations. Runs in both
projections — the wiring is browser-agnostic but the iPhone projection
confirms the phone surface too.
"""

from __future__ import annotations

import json

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.smoke

_BRANCH = "feat/regress-115"


def test_git_status_button_annotates_tiles(authed_page: Page, base_url: str) -> None:
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")

    # Wait for the Coding list to settle — tiles, or the empty-state note.
    authed_page.wait_for_selector(
        ".coding-item, #claudeEmpty:not([hidden])", timeout=10_000
    )
    tiles = authed_page.locator(".coding-item")
    if tiles.count() == 0:
        pytest.skip("no coding projects in this environment — nothing to flag")

    tile_id = tiles.first.get_attribute("data-id")
    assert tile_id, "first coding tile is missing its data-id"

    # Canned status: this tile is BOTH dirty and off its default branch.
    # Red must win the name colour, but the branch tag must still render.
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

    # Pre-tap: no annotations, legend hidden.
    expect(authed_page.locator("#gitStatusLegend")).to_be_hidden()

    authed_page.locator("#gitStatusBtn").click()

    # Legend reveals once the result lands (expect auto-retries through the
    # fetch + re-render), after which the tile classes are settled.
    expect(authed_page.locator("#gitStatusLegend")).to_be_visible()

    name = authed_page.locator(f'.coding-item[data-id="{tile_id}"] .coding-name')
    classes = name.evaluate("el => el.className")
    assert "git-dirty" in classes, (
        f"dirty tile should be red — class was {classes!r}"
    )
    assert "git-off-main" not in classes, (
        "red must take precedence over yellow when a tile is both dirty and "
        f"off-default — class was {classes!r}"
    )

    tag = authed_page.locator(f'.coding-item[data-id="{tile_id}"] .git-branch-tag')
    expect(tag).to_have_text(_BRANCH)
