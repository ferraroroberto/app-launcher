"""Regression pin for issue #666 (Coding per-agent visibility toggles),
retargeted onto the ⋯ menu by #1070.

The feature: the ⚙️ Coding options card carries a "Visible agents" list —
one vendored switch per registry agent plus the GitHub issues button —
generated from /api/agents, never hand-written per agent. Toggling one off
drops that entry immediately and persists as `coding_hidden_agents` in the
webapp config, so it stays hidden across a reload.

What #1070 changed: the entries those switches govern are rows in a project
row's ⋯ menu, not buttons on the row itself. The row carries exactly three
controls now — the favourite agent's launch button, the ⋯ anchor, the star —
so a non-favourite agent is only ever findable inside the menu. The switches
themselves, their ids, their persistence and their generated-from-the-
registry contract are all unchanged. The `vscode` pseudo-id's switch is gone
(the menu is now the only route to every non-favourite launch, so it cannot
be hideable); a stored `vscode` value is inert rather than migrated.

Approach: /api/apps and /api/agents are mocked for a deterministic row and
agent set, but the config write is **real** — the e2e conftest points the
webapp at a throwaway `webapp_config.json`, so the reload assertion proves
actual server-side persistence rather than a client-side illusion.
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


def _install_routes(page: Page) -> None:
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
            body=json.dumps({"agents": AGENTS}),
        ),
    )


def _open_surfaces(page: Page) -> None:
    # Projects and the options card are both collapsed by default.
    page.locator("details.projects-card").evaluate("el => { el.open = true; }")
    page.locator("#codingOptions").evaluate("el => { el.open = true; }")


def _menu(page: Page):
    """The row's ⋯ menu. Opened once; row-menu.js reopens it on the rebuilt
    row after a re-render, so the handle stays usable across a save."""
    return page.locator('.coding-item[data-id="alpha"] .project-menu')


def _open_menu(page: Page):
    menu = _menu(page)
    if not menu.is_visible():
        page.locator('.coding-item[data-id="alpha"] .project-menu-anchor').click()
    expect(menu).to_be_visible()
    return menu


def _codex_btn(page: Page):
    """Codex's launch row inside the ⋯ menu (#1070 moved it off the rail).

    A hidden row is *detached* from the menu by row-menu.js rather than
    marked [hidden], so a count of 0 means genuinely not offered."""
    return _menu(page).locator('.project-launch-btn[data-agent="codex"]')


def _github_btn(page: Page):
    return _menu(page).locator(".project-github-btn")


def _reset_visibility(page: Page, base_url: str) -> None:
    """Start from a known baseline — every button visible.

    The autoboot fixture boots the disposable webapp from a *copy of the real
    config* (issue #441) so values are realistic, which means whatever the
    developer has hidden in their own launcher is the starting state here.
    Asserting an empty default would fail on any machine with a hidden agent,
    so the baseline is set rather than assumed. Loopback bypasses the bearer
    middleware, so a plain request needs no token.
    """
    page.request.post(
        f"{base_url}/api/config", data={"coding_hidden_agents": []}
    )


def test_hidden_agent_and_github_buttons_disappear_and_persist(
    authed_page: Page, base_url: str
) -> None:
    _install_routes(authed_page)
    _reset_visibility(authed_page, base_url)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    _open_surfaces(authed_page)

    # The list is generated from the registry: one row per agent + GitHub.
    # `vscode` is deliberately not among them any more (#1070).
    codex_toggle = authed_page.locator('[data-visibility-toggle="codex"]')
    github_toggle = authed_page.locator('[data-visibility-toggle="github"]')
    expect(codex_toggle).to_have_attribute("aria-checked", "true", timeout=5_000)
    expect(authed_page.locator('[data-visibility-toggle="claude"]')).to_have_count(1)
    expect(authed_page.locator('[data-visibility-toggle="vscode"]')).to_have_count(0)
    expect(_codex_btn(authed_page)).to_have_count(1)
    expect(_github_btn(authed_page)).to_have_count(1)

    # Toggling off drops the row from the ⋯ menu with no reload.
    codex_toggle.click()
    expect(_codex_btn(authed_page)).to_have_count(0)
    github_toggle.click()
    expect(_github_btn(authed_page)).to_have_count(0)
    # The favourite's launch button stays on the row — it is the row's one
    # launch affordance, so it is not the visibility list's to hide (#1070).
    expect(
        authed_page.locator('.coding-item[data-id="alpha"] .agent-btn[data-agent="claude"]')
    ).to_have_count(1)
    # The favorite star is never hideable.
    expect(authed_page.locator('.coding-item[data-id="alpha"] .star-btn')).to_have_count(1)
    # Nor is the ⋯ anchor: it is the only route to the rows above.
    expect(
        authed_page.locator('.coding-item[data-id="alpha"] .project-menu-anchor')
    ).to_have_count(1)

    # Persisted server-side: a reload keeps them hidden and the switches off.
    authed_page.reload(wait_until="domcontentloaded")
    _open_surfaces(authed_page)
    expect(authed_page.locator('[data-visibility-toggle="codex"]')).to_have_attribute(
        "aria-checked", "false", timeout=5_000
    )
    expect(_codex_btn(authed_page)).to_have_count(0)
    expect(_github_btn(authed_page)).to_have_count(0)

    # Toggling back on restores both menu rows.
    authed_page.locator('[data-visibility-toggle="codex"]').click()
    authed_page.locator('[data-visibility-toggle="github"]').click()
    expect(_codex_btn(authed_page)).to_have_count(1)
    expect(_github_btn(authed_page)).to_have_count(1)


def test_agent_visibility_switch_identity_survives_successful_save(
    authed_page: Page, base_url: str
) -> None:
    """Regression pin for issue #732.

    The flaky assertion in the test above only reproduces under full-suite
    WebKit load: two back-to-back toggle taps race a click against a switch
    element that a successful save's re-render had just detached. That race
    itself isn't reliably forceable from a test, but its root cause is
    deterministic and directly assertable — renderAgentVisibility() used to
    tear down and rebuild every switch element (`host.innerHTML = ''`) after
    *every* successful patchConfig() call, not just a failed one. This pins
    that a switch element's identity survives a successful save, which is
    what closes the window a fast second tap could land in.
    """
    _install_routes(authed_page)
    _reset_visibility(authed_page, base_url)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    _open_surfaces(authed_page)

    codex_toggle = authed_page.locator('[data-visibility-toggle="codex"]')
    expect(codex_toggle).to_have_attribute("aria-checked", "true", timeout=5_000)

    authed_page.evaluate(
        """() => {
            document.querySelector('[data-visibility-toggle="github"]')
                .dataset.probeMarker = 'still-me';
        }"""
    )

    codex_toggle.click()
    expect(codex_toggle).to_have_attribute("aria-checked", "false", timeout=5_000)
    # The save is real (unmocked) and round-trips through GET /api/config;
    # give any rebuild triggered by it time to run.
    expect(_codex_btn(authed_page)).to_have_count(0)

    survived = authed_page.evaluate(
        """() => {
            const el = document.querySelector('[data-visibility-toggle="github"]');
            return !!el && el.dataset.probeMarker === 'still-me';
        }"""
    )
    assert survived, (
        "renderAgentVisibility() rebuilt the switch DOM on a successful "
        "save — the exact window issue #732's click race lands in"
    )


def test_brand_marks_are_sprite_symbols_with_no_chip(
    authed_page: Page, base_url: str
) -> None:
    """#1070 — no brand mark renders on a grey chip, in either theme.

    #361 painted a theme-aware `--card-2` tile behind every mark because a
    brand `<img>` is a separate document whose glyph fill cannot reach page
    CSS vars, so monochrome marks were baked to a fixed mid-tone and needed
    a known surface to clear. The marks are inline sprite symbols now, so a
    monochrome one inherits `currentColor` from its button and the chip has
    nothing left to do.

    Asserts the mechanism (a sprite `<use>`, not an `<img>`) and the result
    (a transparent background) rather than a literal colour, and does it in
    both themes — the dark leg is the one the old baked mid-tone was chosen
    for. Auto-retrying `to_have_css` throughout (#680): the Coding rows are
    rebuilt by the git-status poll.
    """
    _install_routes(authed_page)
    _reset_visibility(authed_page, base_url)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    _open_surfaces(authed_page)

    row = authed_page.locator('.coding-item[data-id="alpha"]')
    mark = row.locator('.agent-btn[data-agent="claude"] .agent-icon')
    expect(mark).to_have_count(1, timeout=5_000)
    # The mechanism: an inline sprite reference, and no <img> left on the row.
    expect(mark.locator('use[href="#b-claude"]')).to_have_count(1)
    expect(row.locator("img")).to_have_count(0)

    # A monochrome mark paints with currentColor, so its resolved colour has
    # to actually differ between the themes — that, not the mid-tone the chip
    # used to guarantee, is what keeps it legible on a bare card.
    mono = _menu(authed_page).locator(
        '.project-launch-btn[data-agent="codex"] .agent-icon'
    )
    expect(mono).to_have_count(1)
    seen = {}
    for theme in ("light", "dark"):
        authed_page.evaluate(
            "t => document.documentElement.setAttribute('data-theme', t)", theme
        )
        # The chip is gone in both themes — this is the acceptance criterion.
        expect(mark).to_have_css("background-color", "rgba(0, 0, 0, 0)")
        # …including the marks inside the ⋯ menu, which is where every
        # non-favourite mark lives since #1070.
        gh = _open_menu(authed_page).locator(".project-github-btn .agent-icon")
        expect(gh).to_have_css("background-color", "rgba(0, 0, 0, 0)")
        seen[theme] = mono.evaluate("el => getComputedStyle(el).color")

    assert seen["light"] and seen["dark"], seen
    assert seen["light"] != seen["dark"], (
        "a currentColor brand mark resolved to the same colour in both "
        f"themes ({seen['light']}) — it is not theme-adaptive after all"
    )
