"""Regression pin for the Show-changes overlay (#977).

The feature: a read-only, phone-first view of one project's working tree —
files grouped Changes / Untracked, each a <details> row (status badge · path ·
+N −N) whose unified diff loads lazily on first open and renders with added /
removed line tint. Reachable from the row's ⋯ menu and, as a shortcut, by
tapping a red (dirty) or yellow (off-main) tile name; a clean on-default name
stays inert. ✕ and Escape close it; a clean tree shows the empty state.

Every fetch the overlay makes is mocked before `goto()` (#510) so nothing
here depends on the dev box's real repos, and the diff tint is asserted with
auto-retrying `to_have_css` rather than a raw computed-style read (#680).
"""

from __future__ import annotations

import json
import re

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.smoke

AGENTS = [
    {"id": "claude", "label": "Claude Code", "available": True, "fullscreen": False},
]

CHANGES = {
    "id": "alpha", "name": "alpha", "branch": "feat/977-wip",
    "files": [
        {"path": "app/webapp/static/apps-coding.js", "status": "M", "staged": False,
         "old_path": None, "additions": 12, "deletions": 3, "binary": False},
        {"path": "src/new_thing.py", "status": "A", "staged": True,
         "old_path": None, "additions": 40, "deletions": 0, "binary": False},
        {"path": "notes/todo.md", "status": "U", "staged": False,
         "old_path": None, "additions": 3, "deletions": 0, "binary": False},
    ],
    "counts": {"files": 3, "additions": 55, "deletions": 3},
}

DIFF = (
    "diff --git a/app/webapp/static/apps-coding.js b/app/webapp/static/apps-coding.js\n"
    "index 1111111..2222222 100644\n"
    "--- a/app/webapp/static/apps-coding.js\n"
    "+++ b/app/webapp/static/apps-coding.js\n"
    "@@ -1,3 +1,3 @@\n"
    " const A = 1;\n"
    "-const B = 2;\n"
    "+const B = 3;\n"
    " const C = 4;\n"
)


def _app(app_id: str) -> dict:
    return {
        "id": app_id, "name": app_id, "kind": "claude-code",
        "project_dir": f"E:/automation/{app_id}", "added_at": "",
        "is_favorite": False, "repo_url": None,
    }


def _install_routes(page: Page, *, dirty: bool, files: dict = CHANGES) -> dict:
    fetched: dict = {"changes": [], "diff": []}
    page.route(
        "**/api/apps",
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"scan_root": "E:/automation", "apps": [_app("alpha"), _app("clean")]}),
        ),
    )
    page.route(
        "**/api/agents",
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"agents": AGENTS, "vscode_available": True}),
        ),
    )
    page.route(
        re.compile(r".*/api/claude-code/git-status$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"projects": [
                {"id": "alpha", "is_git": True, "branch": "feat/977-wip", "default_branch": "main",
                 "on_default_branch": False, "dirty": dirty},
                {"id": "clean", "is_git": True, "branch": "main", "default_branch": "main",
                 "on_default_branch": True, "dirty": False},
            ]}),
        ),
    )

    def _diff(route):
        fetched["diff"].append(route.request.url)
        route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"path": "app/webapp/static/apps-coding.js", "diff": DIFF,
                             "binary": False, "truncated": False}),
        )

    def _changes(route):
        fetched["changes"].append(route.request.url)
        route.fulfill(status=200, content_type="application/json", body=json.dumps(files))

    # The diff route is the more specific URL; register it first.
    page.route(re.compile(r".*/api/claude-code/changes/[^/]+/diff\?.*"), _diff)
    page.route(re.compile(r".*/api/claude-code/changes/[^/]+$"), _changes)
    return fetched


def _open_projects(page: Page) -> None:
    page.locator("details.projects-card").evaluate("el => { el.open = true; }")


def _open_via_menu(page: Page, app_id: str = "alpha") -> None:
    page.locator(f'.coding-item[data-id="{app_id}"] .project-menu-anchor').click()
    page.locator(f'.coding-item[data-id="{app_id}"] .project-changes-btn').click()


def test_overlay_lists_files_and_expands_a_tinted_diff(
    authed_page: Page, base_url: str
) -> None:
    fetched = _install_routes(authed_page, dirty=True)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    _open_projects(authed_page)

    expect(authed_page.locator('.coding-item[data-id="alpha"] .project-menu-anchor')).to_be_enabled(timeout=5_000)
    _open_via_menu(authed_page)

    overlay = authed_page.locator("#changesOverlay")
    expect(overlay).to_be_visible()
    expect(authed_page.locator("#changesTitle")).to_contain_text("alpha")
    expect(authed_page.locator("#changesTitle")).to_contain_text("feat/977-wip")
    expect(authed_page.locator("#changesList .chg-summary")).to_contain_text("3 files")

    rows = authed_page.locator("#changesList .chg-file")
    expect(rows).to_have_count(3)
    titles = authed_page.locator("#changesList .chg-group-title")
    expect(titles).to_have_text(["Changes (2)", "Untracked (1)"])
    first = rows.first
    expect(first.locator(".chg-badge")).to_have_text("M")
    expect(first.locator(".chg-base")).to_have_text("apps-coding.js")
    expect(first.locator(".chg-dir")).to_have_text("app/webapp/static/")
    expect(first.locator(".chg-add")).to_have_text("+12")
    # The staged pip only on the staged row.
    expect(rows.nth(1).locator(".chg-staged")).to_have_count(1)
    expect(first.locator(".chg-staged")).to_have_count(0)
    # Nothing fetched until a row opens.
    assert fetched["diff"] == []

    first.locator("summary").click()
    pre = first.locator(".chg-diff")
    expect(pre).to_be_visible(timeout=5_000)
    assert len(fetched["diff"]) == 1
    assert "path=app%2Fwebapp%2Fstatic%2Fapps-coding.js" in fetched["diff"][0]
    # `diff --git` / `index` headers are dropped; the hunk and the lines stay.
    expect(pre.locator(".d-hunk")).to_have_text("@@ -1,3 +1,3 @@")
    expect(pre.locator(".d-del")).to_have_text("-const B = 2;")
    expect(pre.locator(".d-add")).to_have_text("+const B = 3;")
    expect(pre.locator(".d-ctx")).to_have_count(2)
    # Tinted, and block-level so a wrapped line keeps its tint (#680: assert
    # through the auto-retrying matcher, never a raw computed-style read).
    expect(pre.locator(".d-add")).to_have_css("display", "block")
    expect(pre.locator(".d-add")).not_to_have_css("background-color", "rgba(0, 0, 0, 0)")
    expect(pre.locator(".d-del")).not_to_have_css("background-color", "rgba(0, 0, 0, 0)")

    # Re-opening the same row doesn't refetch.
    first.locator("summary").click()
    first.locator("summary").click()
    assert len(fetched["diff"]) == 1

    authed_page.locator("#changesClose").click()
    expect(overlay).to_be_hidden()
    expect(authed_page.locator("#changesList .chg-file")).to_have_count(0)


def test_red_name_opens_changes_and_clean_name_is_inert(
    authed_page: Page, base_url: str
) -> None:
    fetched = _install_routes(authed_page, dirty=True)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    _open_projects(authed_page)

    name = authed_page.locator('.coding-item[data-id="alpha"] .coding-name')
    expect(name).to_have_class(re.compile(r"\bgit-dirty\b"), timeout=5_000)
    expect(name).to_have_attribute("role", "button")
    name.click()
    expect(authed_page.locator("#changesOverlay")).to_be_visible()
    assert len(fetched["changes"]) == 1
    authed_page.keyboard.press("Escape")
    expect(authed_page.locator("#changesOverlay")).to_be_hidden()

    clean = authed_page.locator('.coding-item[data-id="clean"] .coding-name')
    expect(clean).not_to_have_attribute("role", "button")
    clean.click()
    expect(authed_page.locator("#changesOverlay")).to_be_hidden()
    assert len(fetched["changes"]) == 1


def test_clean_tree_shows_the_empty_state(authed_page: Page, base_url: str) -> None:
    empty = {"id": "alpha", "name": "alpha", "branch": "feat/977-wip", "files": [],
             "counts": {"files": 0, "additions": 0, "deletions": 0}}
    _install_routes(authed_page, dirty=False, files=empty)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    _open_projects(authed_page)

    # Off-main but clean: yellow name, still a shortcut (the branch is the
    # "what's different here").
    name = authed_page.locator('.coding-item[data-id="alpha"] .coding-name')
    expect(name).to_have_class(re.compile(r"\bgit-off-main\b"), timeout=5_000)
    name.click()
    expect(authed_page.locator("#changesOverlay")).to_be_visible()
    expect(authed_page.locator("#changesState")).to_contain_text("Working tree clean")
    expect(authed_page.locator("#changesList .chg-file")).to_have_count(0)
