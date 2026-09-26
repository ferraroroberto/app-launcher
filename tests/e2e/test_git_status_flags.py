"""Regression pin for issues #115 + #496 (Coding tab git-status flags).

The feature (#115) coloured each tile from /api/claude-code/git-status —
red for a dirty tree, yellow for a non-default branch (red wins when both,
but the branch tag still shows) — plus a legend. #496 reversed the
on-demand contract: the fetch now happens automatically at boot (and on a
slow poll while the Coding/Board tab is visible), so the annotations and
legend must appear WITHOUT any tap on the status button.

Approach: the real per-project git state isn't deterministic across
environments, so we intercept the endpoint BEFORE first navigation with a
pure-fulfill handler serving canned flags for the real project ids (read
from /api/apps, the same list the tiles render from), then assert the DOM
annotations appear with no button interaction. Runs in both projections —
the wiring is browser-agnostic but the iPhone projection confirms the phone
surface too.
"""

from __future__ import annotations

import json

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.smoke

_BRANCH = "feat/regress-115"


def _canned_git_status(authed_page: Page, base_url: str) -> dict:
    """Canned git-status body stamped onto the real coding project ids.

    The ids have to match the real tiles, but they are read from /api/apps —
    the same list the tiles render from, and no `git` runs to produce it —
    rather than by `route.fetch()`-ing the real /api/claude-code/git-status
    (#908). That endpoint runs `git` once per directory under `projects_dir`
    and cost ~9 s on this box once the fleet had worktrees checked out, so
    the legend wait below sat right on its own budget; worse, a timeout left
    the route handler blocked inside `route.fetch()`, and the
    `TargetClosedError` it then raised at teardown surfaced on the *next*
    test's context fixture. A pure fulfill is instant and does not scale with
    the number of checkouts.
    """
    resp = authed_page.request.get(f"{base_url}/api/apps")
    assert resp.ok, f"/api/apps returned {resp.status} — cannot build the canned body"
    ids = [
        a["id"]
        for a in (resp.json().get("apps") or [])
        if a.get("kind") == "claude-code"
    ]
    if not ids:
        pytest.skip("no coding projects in this environment — nothing to flag")
    # Every project dirty AND off-default, so the red-wins precedence and the
    # branch tag are both exercised whichever tile sorts first.
    return {
        "projects": [
            {
                "id": pid,
                "is_git": True,
                "branch": _BRANCH,
                "default_branch": "main",
                "on_default_branch": False,
                "dirty": True,
            }
            for pid in ids
        ]
    }


def test_git_status_auto_annotates_tiles_without_tap(
    authed_page: Page, base_url: str
) -> None:
    canned = _canned_git_status(authed_page, base_url)

    # Install the intercept BEFORE goto — the boot fetch (#496) must consume
    # it.
    authed_page.route(
        "**/api/claude-code/git-status",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(canned),
        ),
    )
    # Boot's panel fetches run concurrently (#1258): one that never answers
    # (the ports probe, held open for the whole test) must not hold back the
    # git flags, which used to wait behind every fetch ahead of them.
    authed_page.route("**/api/ports/probe", lambda route: None)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")

    # Projects is collapsed by default (#383 review round) — expand it so
    # the tiles and legend are visible.
    authed_page.locator("details.projects-card").evaluate(
        "el => { el.open = true; }"
    )

    authed_page.wait_for_selector(".coding-item", timeout=10_000)
    tile_id = authed_page.locator(".coding-item").first.get_attribute("data-id")
    assert tile_id, "first coding tile is missing its data-id"

    # No tap anywhere: the legend and annotations arrive from the boot fetch
    # alone (expect auto-retries through the fetch + re-render).
    expect(authed_page.locator("#gitStatusLegend")).to_be_visible(
        timeout=10_000
    )

    name = authed_page.locator(f'.coding-item[data-id="{tile_id}"] .action-row-title')
    classes = name.evaluate("el => el.className")
    assert "git-dirty" in classes, (
        f"dirty tile should be red without any tap — class was {classes!r}"
    )
    assert "git-off-main" not in classes, (
        "red must take precedence over yellow when a tile is both dirty and "
        f"off-default — class was {classes!r}"
    )

    # The branch rides the row's context line (#1128), spelled out beside
    # the colour so hue is never the only channel.
    meta = authed_page.locator(f'.coding-item[data-id="{tile_id}"] .action-row-meta')
    expect(meta).to_contain_text(_BRANCH)
    expect(meta).to_contain_text("Uncommitted changes")

    # The summary head card aggregates the same cache (#496 item 1/3).
    expect(authed_page.locator("#homeHeadStatus")).to_contain_text("dirty")
