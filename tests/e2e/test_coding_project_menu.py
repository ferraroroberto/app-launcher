"""Regression pin for the Coding row's ⋯ project menu (#977, superseding the
#802 VS Code button it replaced).

The feature: every Coding-tab project row carries one ⋯ anchor in the old VS
Code slot — between the agent buttons and the GitHub one — that opens a
floating menu of three actions: Open in VS Code (#802's POST, unchanged),
Show changes (the read-only working-tree overlay, pinned separately in
test_coding_changes_overlay.py) and Open folder (Explorer on the PC). The
VS Code item is greyed with a hover hint when the `code` CLI isn't on PATH;
Show changes is hidden for a folder git-status says isn't a repository; the
whole menu is hideable from the Visible-agents list under the same `vscode`
pseudo-id as before, so an existing hidden list keeps working.

/api/apps, /api/agents and /api/claude-code/git-status are mocked so the
row, the agent set, the `code`-installed flag and the git state are
deterministic; the two POSTs are mocked too so the suite never spawns a real
editor or Explorer on the dev box. The visibility half writes for real (the
e2e conftest points the webapp at a throwaway config), so the reload
assertion proves server-side persistence.
"""

from __future__ import annotations

import json
import re

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.smoke

AGENTS = [
    {"id": "claude", "label": "Claude Code", "available": True, "fullscreen": False},
    {"id": "codex", "label": "Codex CLI", "available": True, "fullscreen": True},
]


def _app(app_id: str) -> dict:
    return {
        "id": app_id,
        "name": app_id,
        "kind": "claude-code",
        "project_dir": f"E:/automation/{app_id}",
        "added_at": "",
        "is_favorite": False,
        "repo_url": f"https://github.com/ferraroroberto/{app_id}",
    }


def _install_routes(page: Page, *, vscode_available: bool) -> dict:
    """Mock the boot fetches; return the POST log.

    Mocked before `goto()` per this repo's #510 convention — a late
    /api/agents or git-status response otherwise re-renders the row out from
    under a click. `alpha` is a git repo, `plain` is not.
    """
    posted: dict = {"vscode": [], "folder": []}

    page.route(
        "**/api/apps",
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"scan_root": "E:/automation", "apps": [_app("alpha"), _app("plain")]}),
        ),
    )
    page.route(
        "**/api/agents",
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"agents": AGENTS, "vscode_available": vscode_available}),
        ),
    )
    page.route(
        re.compile(r".*/api/claude-code/git-status$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"projects": [
                {"id": "alpha", "is_git": True, "branch": "main", "default_branch": "main",
                 "on_default_branch": True, "dirty": False},
                {"id": "plain", "is_git": False, "branch": None, "default_branch": None,
                 "on_default_branch": True, "dirty": False},
            ]}),
        ),
    )

    def _vscode(route):
        posted["vscode"].append(route.request.url)
        route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"ok": True, "workspace": "E:/automation/alpha.code-workspace",
                             "created": True, "pid": 4321}),
        )

    def _folder(route):
        posted["folder"].append(route.request.url)
        route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"ok": True, "path": "E:/automation/alpha", "pid": 99}),
        )

    page.route("**/api/claude-code/vscode/**", _vscode)
    page.route("**/api/claude-code/folder/**", _folder)
    return posted


def _open_projects(page: Page) -> None:
    # Projects is collapsed by default (#383 review round).
    page.locator("details.projects-card").evaluate("el => { el.open = true; }")


def _open_surfaces(page: Page) -> None:
    _open_projects(page)
    page.locator("#codingOptions").evaluate("el => { el.open = true; }")


def _reset_visibility(page: Page, base_url: str) -> None:
    """Baseline: every row button visible.

    The autoboot fixture copies the *real* config (#441), so whatever the
    developer has hidden in their own launcher would otherwise be the starting
    state. Loopback bypasses the bearer middleware, so no token is needed.
    """
    page.request.post(f"{base_url}/api/config", data={"coding_hidden_agents": []})


def _anchor(page: Page, app_id: str = "alpha"):
    return page.locator(f'.coding-item[data-id="{app_id}"] .project-menu-anchor')


def _menu(page: Page, app_id: str = "alpha"):
    return page.locator(f'.coding-item[data-id="{app_id}"] .project-menu')


def _github_btn(page: Page):
    return page.locator('.coding-item[data-id="alpha"] .agent-btn').filter(
        has=page.locator('use[href="#b-github"]')
    )


def test_menu_anchor_sits_between_the_agents_and_github(
    authed_page: Page, base_url: str
) -> None:
    posted = _install_routes(authed_page, vscode_available=True)
    _reset_visibility(authed_page, base_url)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    _open_projects(authed_page)

    expect(_anchor(authed_page)).to_be_enabled(timeout=5_000)

    # Order contract: agents, ⋯ (the old VS Code slot), GitHub, star last;
    # the floating menu itself is appended after the star so the rail's
    # sibling-divider rules still see adjacent buttons. One synchronous
    # evaluate() so the read can't straddle the ~4 s apps re-render (#680).
    marks = authed_page.evaluate(
        """() => Array.from(
            document.querySelectorAll('.coding-item[data-id="alpha"] .row-actions > button')
        ).map(el => el.classList.contains('star-btn')
            ? 'star'
            : (el.dataset.agent || 'github'))"""
    )
    assert marks == ["claude", "codex", "vscode", "github", "star"], marks
    expect(_menu(authed_page)).to_be_hidden()

    _anchor(authed_page).click()
    menu = _menu(authed_page)
    expect(menu).to_be_visible()
    expect(_anchor(authed_page)).to_have_attribute("aria-expanded", "true")
    expect(menu.locator(".row-menu-label")).to_have_text(
        ["Open in VS Code", "Show changes", "Open folder"]
    )

    # Open in VS Code: #802's POST, unchanged, and the menu closes on the tap.
    menu.locator(".project-vscode-btn").click()
    expect(authed_page.locator("#toast")).to_contain_text("alpha.code-workspace", timeout=5_000)
    assert posted["vscode"] and posted["vscode"][0].endswith("/api/claude-code/vscode/alpha")
    expect(menu).to_be_hidden()

    # Open folder: its own POST, toast names Explorer.
    _anchor(authed_page).click()
    menu.locator(".project-folder-btn").click()
    expect(authed_page.locator("#toast")).to_contain_text("Explorer", timeout=5_000)
    assert posted["folder"] and posted["folder"][0].endswith("/api/claude-code/folder/alpha")
    expect(menu).to_be_hidden()


def test_menu_closes_on_escape_outside_tap_and_survives_a_rerender(
    authed_page: Page, base_url: str
) -> None:
    _install_routes(authed_page, vscode_available=True)
    _reset_visibility(authed_page, base_url)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    _open_projects(authed_page)

    anchor = _anchor(authed_page)
    expect(anchor).to_be_enabled(timeout=5_000)
    anchor.click()
    expect(_menu(authed_page)).to_be_visible()
    authed_page.keyboard.press("Escape")
    expect(_menu(authed_page)).to_be_hidden()
    expect(anchor).to_have_attribute("aria-expanded", "false")

    anchor.click()
    expect(_menu(authed_page)).to_be_visible()
    # An outside tap closes it (the page heading is outside the rail).
    authed_page.locator("body").click(position={"x": 5, "y": 5})
    expect(_menu(authed_page)).to_be_hidden()

    # Survives the list rebuild the apps poll does every few seconds: the
    # open row's key is remembered and the menu reopens on the rebuilt row.
    anchor.click()
    expect(_menu(authed_page)).to_be_visible()
    authed_page.wait_for_timeout(5_000)
    expect(_menu(authed_page)).to_be_visible()
    expect(_anchor(authed_page)).to_have_attribute("aria-expanded", "true")


def test_vscode_item_disabled_when_cli_missing_and_changes_hidden_for_non_git(
    authed_page: Page, base_url: str
) -> None:
    posted = _install_routes(authed_page, vscode_available=False)
    _reset_visibility(authed_page, base_url)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    _open_projects(authed_page)

    _anchor(authed_page).click()
    item = _menu(authed_page).locator(".project-vscode-btn")
    expect(item).to_be_disabled(timeout=5_000)
    expect(item).to_have_attribute("title", "Visual Studio Code is not installed")
    expect(_menu(authed_page).locator(".project-changes-btn")).to_have_count(1)
    authed_page.keyboard.press("Escape")
    assert posted["vscode"] == []

    # `plain` isn't a git repo (mocked git-status): no Show changes item,
    # the other two still there.
    _anchor(authed_page, "plain").click()
    menu = _menu(authed_page, "plain")
    expect(menu).to_be_visible()
    expect(menu.locator(".project-changes-btn")).to_have_count(0)
    expect(menu.locator(".project-folder-btn")).to_have_count(1)


def test_menu_is_hideable_and_persists(authed_page: Page, base_url: str) -> None:
    _install_routes(authed_page, vscode_available=True)
    _reset_visibility(authed_page, base_url)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    _open_surfaces(authed_page)

    toggle = authed_page.locator('[data-visibility-toggle="vscode"]')
    expect(toggle).to_have_attribute("aria-checked", "true", timeout=5_000)
    expect(_anchor(authed_page)).to_have_count(1)

    toggle.click()
    expect(_anchor(authed_page)).to_have_count(0)
    expect(_menu(authed_page)).to_have_count(0)
    # Hiding is per button — GitHub and the star are untouched.
    expect(_github_btn(authed_page)).to_have_count(1)
    expect(authed_page.locator('.coding-item[data-id="alpha"] .star-btn')).to_have_count(1)

    # Persisted server-side, not just a client-side illusion.
    authed_page.reload(wait_until="domcontentloaded")
    _open_surfaces(authed_page)
    expect(authed_page.locator('[data-visibility-toggle="vscode"]')).to_have_attribute(
        "aria-checked", "false", timeout=5_000
    )
    expect(_anchor(authed_page)).to_have_count(0)

    authed_page.locator('[data-visibility-toggle="vscode"]').click()
    expect(_anchor(authed_page)).to_have_count(1)
