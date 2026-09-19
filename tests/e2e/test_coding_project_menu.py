"""Regression pin for the Coding row's ⋯ project menu (#977, superseding the
#802 VS Code button it replaced; widened by #1070).

The feature: every Coding-tab project row carries one ⋯ anchor that opens a
floating menu. Since #1070 the row is exactly three controls wide — the
favourite agent's launch button, this anchor, the star — and the menu holds
everything that used to sit on the rail beside them: a launch row per
visible non-favourite agent, then GitHub issues, then the three project
actions #977 put here (Open in VS Code — #802's POST, unchanged; Show
changes — the read-only working-tree overlay, pinned separately in
test_coding_changes_overlay.py; Open folder — Explorer on the PC).

The VS Code item is greyed with a hover hint when the `code` CLI isn't on
PATH; a launch row is greyed the same way when its agent isn't installed;
Show changes is hidden for a folder git-status says isn't a repository; a
row hidden in the Visible-agents list is *detached* from the menu rather
than marked [hidden], so the counts below only ever see what is offered.

The menu itself is no longer hideable (#1070) — it is the only route to
every non-favourite launch. A `vscode` value left in a config by #666 is
inert rather than migrated; that is pinned below.

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


def test_row_carries_three_controls_and_the_menu_holds_the_rest(
    authed_page: Page, base_url: str
) -> None:
    posted = _install_routes(authed_page, vscode_available=True)
    _reset_visibility(authed_page, base_url)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    _open_projects(authed_page)

    expect(_anchor(authed_page)).to_be_enabled(timeout=5_000)

    # Order contract (#1070): the favourite agent's launch button, the ⋯
    # anchor, the star — three controls, nothing else. The floating menu
    # itself is appended after the star so the rail's sibling-divider rules
    # still see adjacent buttons. One synchronous evaluate() so the read
    # can't straddle the ~4 s apps re-render (#680).
    marks = authed_page.evaluate(
        """() => Array.from(
            document.querySelectorAll('.coding-item[data-id="alpha"] .row-actions > button')
        ).map(el => el.classList.contains('star-btn')
            ? 'star'
            : (el.dataset.agent || 'github'))"""
    )
    assert marks == ["claude", "vscode", "star"], marks
    expect(_menu(authed_page)).to_be_hidden()

    _anchor(authed_page).click()
    menu = _menu(authed_page)
    expect(menu).to_be_visible()
    expect(_anchor(authed_page)).to_have_attribute("aria-expanded", "true")
    # Everything that left the row is here, non-favourite launches first,
    # then GitHub, then the three project actions #977 put here.
    expect(menu.locator(".row-menu-label")).to_have_text(
        [
            "Codex CLI",
            "GitHub issues",
            "Open in VS Code",
            "Show changes",
            "Open folder",
        ]
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


def test_menu_is_not_hideable_and_a_stored_vscode_value_is_inert(
    authed_page: Page, base_url: str
) -> None:
    """#1070 — the ⋯ menu stopped being hideable, deliberately.

    Under #666 the `vscode` pseudo-id hid the whole menu, which was safe
    while the menu held only VS Code / Show changes / Open folder. It now
    also holds a launch row for every non-favourite agent and the GitHub
    row, so hiding it would strand them with no other route. The switch is
    therefore gone from the Visible-agents list, and a `vscode` value left
    in an existing config is honoured as a no-op rather than migrated away
    — that is what this asserts, because "hidden list still contains
    vscode" is the state every launcher that ran #666 is actually in.

    This replaces test_menu_is_hideable_and_persists, which pinned the
    opposite requirement.
    """
    _install_routes(authed_page, vscode_available=True)
    # The state an existing launcher is in: `vscode` sitting in the stored
    # hidden list from #666.
    authed_page.request.post(
        f"{base_url}/api/config", data={"coding_hidden_agents": ["vscode"]}
    )
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    _open_surfaces(authed_page)

    # No switch offers to hide it any more …
    expect(authed_page.locator('[data-visibility-toggle="claude"]')).to_have_count(
        1, timeout=5_000
    )
    expect(authed_page.locator('[data-visibility-toggle="vscode"]')).to_have_count(0)
    # … and the stored value does not hide it either.
    expect(_anchor(authed_page)).to_have_count(1)
    menu = _menu(authed_page)
    expect(menu).to_have_count(1)
    _anchor(authed_page).click()
    expect(menu).to_be_visible()
    expect(menu.locator(".project-vscode-btn")).to_have_count(1)

    # The stored value survives untouched — nothing migrated it away.
    stored = authed_page.request.get(f"{base_url}/api/config").json()
    assert "vscode" in stored["coding_hidden_agents"], stored["coding_hidden_agents"]


def test_favorite_agent_dropdown_moves_the_button_with_no_reload(
    authed_page: Page, base_url: str
) -> None:
    """#1070 — the options card's Favorite agent picker owns the row button.

    The favourite is chosen explicitly, never derived from usage: a button
    that relocates on its own is worse than one in the wrong place. Changing
    it swaps which agent is on the row and drops the previous favourite into
    the ⋯ menu, with no reload — and it persists, because it is a config
    write like the visibility switches it sits beside.

    The picker is generated from the live registry (the same contract as
    renderAgentVisibility, #666): with /api/agents mocked to two agents, it
    has exactly two options and no hand-written third.
    """
    _install_routes(authed_page, vscode_available=True)
    _reset_visibility(authed_page, base_url)
    authed_page.request.post(
        f"{base_url}/api/config", data={"coding_favorite_agent": "claude"}
    )
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    _open_surfaces(authed_page)

    picker = authed_page.locator("#codingFavoriteAgent")
    expect(picker).to_have_value("claude", timeout=5_000)
    # Generated from the registry, not hand-written per agent.
    expect(picker.locator("option")).to_have_text(["Claude Code", "Codex CLI"])

    row_btn = authed_page.locator(
        '.coding-item[data-id="alpha"] .row-actions > .agent-btn[data-agent]'
    ).first
    expect(row_btn).to_have_attribute("data-agent", "claude")

    # Swap the favourite → the row button becomes Codex, no reload.
    picker.select_option("codex")
    expect(
        authed_page.locator(
            '.coding-item[data-id="alpha"] .row-actions > button[data-agent="codex"]'
        )
    ).to_have_count(1, timeout=5_000)
    # The row still carries exactly three controls.
    expect(
        authed_page.locator('.coding-item[data-id="alpha"] .row-actions > button')
    ).to_have_count(3)

    # Claude is now the one in the menu, and Codex is not repeated there.
    _anchor(authed_page).click()
    menu = _menu(authed_page)
    expect(menu).to_be_visible()
    expect(menu.locator('.project-launch-btn[data-agent="claude"]')).to_have_count(1)
    expect(menu.locator('.project-launch-btn[data-agent="codex"]')).to_have_count(0)
    authed_page.keyboard.press("Escape")

    # Persisted server-side, not a client-side illusion.
    stored = authed_page.request.get(f"{base_url}/api/config").json()
    assert stored["coding_favorite_agent"] == "codex", stored["coding_favorite_agent"]


def test_favorite_agent_stays_on_the_row_even_when_hidden(
    authed_page: Page, base_url: str
) -> None:
    """#1070 — "Visible agents" governs the menu, not the row's one button.

    Hiding the favourite would otherwise leave the row with no launch
    affordance at all, which is the failure #1040 had to repair on the
    session row: a green gate over a row whose options had silently
    vanished. The favourite is rendered regardless of the hidden list.
    """
    _install_routes(authed_page, vscode_available=True)
    authed_page.request.post(
        f"{base_url}/api/config",
        data={"coding_hidden_agents": ["claude", "codex", "github"]},
    )
    authed_page.request.post(
        f"{base_url}/api/config", data={"coding_favorite_agent": "claude"}
    )
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    _open_projects(authed_page)

    # Hidden everywhere else, still on the row.
    expect(
        authed_page.locator(
            '.coding-item[data-id="alpha"] .row-actions > button[data-agent="claude"]'
        )
    ).to_have_count(1, timeout=5_000)
    _anchor(authed_page).click()
    menu = _menu(authed_page)
    expect(menu).to_be_visible()
    # …and never repeated inside the menu, hidden or not.
    expect(menu.locator(".project-launch-btn")).to_have_count(0)
    expect(menu.locator(".project-github-btn")).to_have_count(0)
    # The three project actions are not agents and stay put.
    expect(menu.locator(".project-vscode-btn")).to_have_count(1)
