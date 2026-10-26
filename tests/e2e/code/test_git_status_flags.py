"""Regression pin for issues #115 + #496 + #1434 (Coding tab git-status flags).

The feature (#115) annotated each project row from
/api/claude-code/git-status. #496 reversed the on-demand contract: the fetch
now happens automatically at boot (and on a slow poll while the Coding/Board
tab is visible), so the annotations must appear WITHOUT any tap. #1434 made
them quieter and moved git's counts into the Projects card: the branch is
plain meta text ("on <branch>"), uncommitted work an attention chip (never
red, decision 8 of #1432), the card's summary carries the uncommitted
count, its footer says when git was checked, and the page header carries no
git counts at all.

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
import re

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
    # Every project dirty AND off-default, so the branch and the chip are
    # both exercised whichever tile sorts first.
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
    # the rows and footer are visible.
    authed_page.locator("details.projects-card").evaluate(
        "el => { el.open = true; }"
    )

    authed_page.wait_for_selector(".coding-item", timeout=10_000)
    tile_id = authed_page.locator(".coding-item").first.get_attribute("data-id")
    assert tile_id, "first coding tile is missing its data-id"

    # No tap anywhere: the annotations arrive from the boot fetch alone
    # (expect auto-retries through the fetch + re-render).
    expect(authed_page.locator("#gitCheckedFooter")).to_have_text(
        "Git checked just now", timeout=10_000
    )

    row = authed_page.locator(f'.coding-item[data-id="{tile_id}"]')
    expect(row).to_have_attribute("data-git", "dirty")
    # No red: the title carries no git colour class any more (#1434).
    expect(row.locator(".action-row-title")).not_to_have_class(re.compile(r"git-"))

    # The branch rides the row's context line in the plain meta colour, and
    # uncommitted work is an attention chip after it.
    meta = row.locator(".action-row-meta")
    expect(meta.locator(".action-row-meta-text")).to_have_text("on " + _BRANCH)
    uncommitted = meta.locator(".git-uncommitted")
    expect(uncommitted).to_have_text("uncommitted")
    expect(uncommitted).to_have_attribute("data-tone", "attention")

    # The Projects summary counts them, behind an attention dot.
    summary = authed_page.locator("#projectsSummaryMeta")
    expect(summary).to_have_text(f"{len(canned['projects'])} uncommitted")
    expect(summary.locator('.tone-dot[data-tone="attention"]')).to_have_count(1)

    # The page header carries no git counts any more (#1434).
    head = authed_page.locator("#homeHeadStatus")
    expect(head).not_to_contain_text("dirty")
    expect(head).not_to_contain_text("off-main")
    expect(head).not_to_contain_text("uncommitted")
