"""Regression pin for issue #802 (Coding row → Visual Studio Code button).

The feature: every Coding-tab project row carries a VS Code icon button that
POSTs to /api/claude-code/vscode/{id}, which resolves (creating if needed) the
project's sibling `.code-workspace` file and opens the local editor on it. It
is not an agent launch — no PTY, no session — but it lives in the same strip,
sits between the agent buttons and the GitHub one, is greyed with a hover hint
when the `code` CLI isn't on PATH, and is hideable from the same Visible-agents
list as the rest.

/api/apps and /api/agents are mocked so the row, the agent set, and the
`code`-installed flag are deterministic; the POST is mocked too so the suite
never spawns a real editor on the dev box. The visibility half writes for real
(the e2e conftest points the webapp at a throwaway config), so the reload
assertion proves server-side persistence.
"""

from __future__ import annotations

import json

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.smoke

AGENTS = [
    {"id": "claude", "label": "Claude Code", "available": True, "fullscreen": False},
    {"id": "codex", "label": "Codex CLI", "available": True, "fullscreen": True},
]


def _install_routes(page: Page, *, vscode_available: bool) -> list:
    """Mock the boot fetches; return the list the POST body lands in.

    Mocked before `goto()` per this repo's #510 convention — a late
    /api/agents response otherwise re-renders the row out from under a click.
    """
    posted: list = []

    page.route(
        "**/api/apps",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "scan_root": "E:/automation",
                    "apps": [
                        {
                            "id": "alpha",
                            "name": "alpha",
                            "kind": "claude-code",
                            "project_dir": "E:/automation/alpha",
                            "added_at": "",
                            "is_favorite": False,
                            "repo_url": "https://github.com/ferraroroberto/alpha",
                        }
                    ],
                }
            ),
        ),
    )
    page.route(
        "**/api/agents",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {"agents": AGENTS, "vscode_available": vscode_available}
            ),
        ),
    )

    def _vscode(route):
        posted.append(route.request.url)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "ok": True,
                    "workspace": "E:/automation/alpha.code-workspace",
                    "created": True,
                    "pid": 4321,
                }
            ),
        )

    page.route("**/api/claude-code/vscode/**", _vscode)
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


def _vscode_btn(page: Page):
    return page.locator('.coding-item[data-id="alpha"] .agent-btn[data-agent="vscode"]')


def _github_btn(page: Page):
    return page.locator('.coding-item[data-id="alpha"] .agent-btn').filter(
        has=page.locator('img[alt="GitHub"]')
    )


def test_vscode_button_sits_between_the_agents_and_github(
    authed_page: Page, base_url: str
) -> None:
    posted = _install_routes(authed_page, vscode_available=True)
    _reset_visibility(authed_page, base_url)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    _open_projects(authed_page)

    expect(_vscode_btn(authed_page)).to_be_enabled(timeout=5_000)

    # Order contract from the issue: agents, VS Code, GitHub, star last.
    # Read in one page.evaluate() rather than per-button get_attribute() calls
    # — renderCodingList() rebuilds the strip on the ~4 s apps poll, and a
    # sequence of locator reads can straddle a rebuild (#680). One synchronous
    # JS pass can't interleave with the render; #802's own strip is only three
    # buttons long, so nothing here is worth a stale-handle risk.
    marks = authed_page.evaluate(
        """() => Array.from(
            document.querySelectorAll('.coding-item[data-id="alpha"] .row-actions button')
        ).map(el => el.classList.contains('star-btn')
            ? 'star'
            : (el.dataset.agent || 'github'))"""
    )
    assert marks == ["claude", "codex", "vscode", "github", "star"], marks

    _vscode_btn(authed_page).click()
    # The toast names the created workspace file — the one side effect on disk.
    expect(authed_page.locator("#toast")).to_contain_text(
        "alpha.code-workspace", timeout=5_000
    )
    assert posted and posted[0].endswith("/api/claude-code/vscode/alpha"), posted
    # Nothing is tracked afterwards: the button stays a plain launch, with no
    # Stop control and no session row appearing beside it.
    expect(_vscode_btn(authed_page)).to_be_enabled()
    expect(
        authed_page.locator('.coding-item[data-id="alpha"] .row-actions button')
    ).to_have_count(5)


def test_vscode_button_disabled_when_cli_missing(
    authed_page: Page, base_url: str
) -> None:
    posted = _install_routes(authed_page, vscode_available=False)
    _reset_visibility(authed_page, base_url)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    _open_projects(authed_page)

    btn = _vscode_btn(authed_page)
    expect(btn).to_be_disabled(timeout=5_000)
    expect(btn).to_have_attribute("title", "Visual Studio Code is not installed")
    assert posted == []


def test_vscode_button_is_hideable_and_persists(
    authed_page: Page, base_url: str
) -> None:
    _install_routes(authed_page, vscode_available=True)
    _reset_visibility(authed_page, base_url)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    _open_surfaces(authed_page)

    toggle = authed_page.locator('[data-visibility-toggle="vscode"]')
    expect(toggle).to_have_attribute("aria-checked", "true", timeout=5_000)
    expect(_vscode_btn(authed_page)).to_have_count(1)

    toggle.click()
    expect(_vscode_btn(authed_page)).to_have_count(0)
    # Hiding is per button — GitHub and the star are untouched.
    expect(_github_btn(authed_page)).to_have_count(1)
    expect(authed_page.locator('.coding-item[data-id="alpha"] .star-btn')).to_have_count(1)

    # Persisted server-side, not just a client-side illusion.
    authed_page.reload(wait_until="domcontentloaded")
    _open_surfaces(authed_page)
    expect(authed_page.locator('[data-visibility-toggle="vscode"]')).to_have_attribute(
        "aria-checked", "false", timeout=5_000
    )
    expect(_vscode_btn(authed_page)).to_have_count(0)

    authed_page.locator('[data-visibility-toggle="vscode"]').click()
    expect(_vscode_btn(authed_page)).to_have_count(1)
