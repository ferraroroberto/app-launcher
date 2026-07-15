"""Regression pin for issues #115 + #496 (Coding tab git-status flags).

The feature (#115) coloured each tile from /api/claude-code/git-status —
red for a dirty tree, yellow for a non-default branch (red wins when both,
but the branch tag still shows) — plus a legend. #496 reversed the
on-demand contract: the fetch now happens automatically at boot (and on a
slow poll while the Coding/Board tab is visible), so the annotations and
legend must appear WITHOUT any tap on the status button.

Approach: the real per-project git state isn't deterministic across
environments, so we intercept the endpoint BEFORE first navigation with an
echo-and-override handler (real project ids, canned flags), then assert
the DOM annotations appear with no button interaction. Runs in both
projections — the wiring is browser-agnostic but the iPhone projection
confirms the phone surface too.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.smoke

_BRANCH = "feat/regress-115"


def test_git_status_auto_annotates_tiles_without_tap(
    authed_page: Page, base_url: str
) -> None:
    # Install the intercept BEFORE goto — the boot fetch (#496) must consume
    # it. The handler echoes the real project list (ids must match the real
    # tiles) but stamps every project dirty + off-default, so the assertion
    # is deterministic in any checkout.
    def _all_dirty(route):
        resp = route.fetch()
        body = resp.json()
        for p in body.get("projects", []):
            p.update(
                {
                    "is_git": True,
                    "branch": _BRANCH,
                    "default_branch": "main",
                    "on_default_branch": False,
                    "dirty": True,
                }
            )
        route.fulfill(response=resp, json=body)

    authed_page.route("**/api/claude-code/git-status", _all_dirty)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")

    # Projects is collapsed by default (#383 review round) — expand it so
    # the tiles and legend are visible.
    authed_page.locator("details.projects-card").evaluate(
        "el => { el.open = true; }"
    )

    authed_page.wait_for_selector(
        ".coding-item, #claudeEmpty:not([hidden])", timeout=10_000
    )
    tiles = authed_page.locator(".coding-item")
    if tiles.count() == 0:
        pytest.skip("no coding projects in this environment — nothing to flag")

    tile_id = tiles.first.get_attribute("data-id")
    assert tile_id, "first coding tile is missing its data-id"

    # No tap anywhere: the legend and annotations arrive from the boot fetch
    # alone (expect auto-retries through the fetch + re-render).
    expect(authed_page.locator("#gitStatusLegend")).to_be_visible(
        timeout=10_000
    )

    name = authed_page.locator(f'.coding-item[data-id="{tile_id}"] .coding-name')
    classes = name.evaluate("el => el.className")
    assert "git-dirty" in classes, (
        f"dirty tile should be red without any tap — class was {classes!r}"
    )
    assert "git-off-main" not in classes, (
        "red must take precedence over yellow when a tile is both dirty and "
        f"off-default — class was {classes!r}"
    )

    tag = authed_page.locator(f'.coding-item[data-id="{tile_id}"] .git-branch-tag')
    expect(tag).to_have_text(_BRANCH)

    # The summary head card aggregates the same cache (#496 item 1/3).
    expect(authed_page.locator("#homeHeadStatus")).to_contain_text("dirty")
