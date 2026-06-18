"""Regression pin for issue #250 (Coding tab project favorites).

The feature: a per-tile ★ star toggles a project's favorite state (persisted
server-side via POST /api/claude-code/favorites and surfaced as `is_favorite`
on the /api/apps claude-code rows). Favorites sort to the top of the Coding
list (alphabetical within each group), and the "★ Favorites" header toggle
filters the list down to only the starred projects.

Approach: the real on-disk project set + persisted favorites aren't
deterministic across environments, so we intercept /api/apps with a canned,
*mutable* payload and intercept the favorites POST to flip it — exercising the
real client-side partition, the star-click → re-fetch → reorder loop, and the
filter toggle without touching the user's actual config. Runs in both
projections (the wiring is browser-agnostic, but the iPhone projection
confirms the phone surface too).
"""

from __future__ import annotations

import json

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.smoke


def _row(slug: str, fav: bool) -> dict:
    return {
        "id": slug,
        "name": slug,
        "kind": "claude-code",
        "project_dir": f"E:/automation/{slug}",
        "added_at": "",
        "is_favorite": fav,
    }


def _install_routes(page: Page) -> None:
    """Stateful mocks for /api/apps and the favorites toggle.

    `apps_state` is the single source of truth both routes share: the GET
    renders from it, the POST mutates it, so a star click round-trips through
    the same path the real backend would (toggle → persist → re-fetch).
    Rows are listed alphabetically, exactly as the scanner returns them, so
    the favorites-first partition is the only thing reordering the list.
    """
    apps_state = {
        "alpha": False,
        "bravo": True,
        "charlie": False,
        "delta": False,
    }

    def _apps(route):
        rows = [_row(slug, fav) for slug, fav in apps_state.items()]
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"scan_root": "E:/automation", "apps": rows}),
        )

    def _favorites(route, request):
        body = request.post_data_json or {}
        slug = body.get("id")
        if slug in apps_state:
            apps_state[slug] = bool(body.get("favorite"))
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "ok": True,
                    "coding_favorites": [s for s, f in apps_state.items() if f],
                }
            ),
        )

    page.route("**/api/apps", _apps)
    page.route("**/api/claude-code/favorites", _favorites)


def _order(page: Page) -> list:
    return page.locator("#claudeList .coding-item").evaluate_all(
        "els => els.map(e => e.getAttribute('data-id'))"
    )


def test_favorites_pin_to_top_then_filter(authed_page: Page, base_url: str) -> None:
    # Routes must be live before boot's first /api/apps fetch.
    _install_routes(authed_page)
    # Start with the filter OFF regardless of any persisted pref from a
    # previous run on this profile.
    authed_page.add_init_script(
        "window.localStorage.setItem('launcher.codingFavFilter', '0');"
    )
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")

    expect(authed_page.locator("#claudeList .coding-item")).to_have_count(4)

    # Default view: the one favorite (bravo) is pinned above the alphabetical
    # rest (alpha, charlie, delta).
    assert _order(authed_page) == ["bravo", "alpha", "charlie", "delta"]

    # Star state is reflected on the tiles.
    bravo_star = authed_page.locator('.coding-item[data-id="bravo"] .star-btn')
    assert "is-fav" in bravo_star.get_attribute("class")
    alpha_star = authed_page.locator('.coding-item[data-id="alpha"] .star-btn')
    assert "is-fav" not in alpha_star.get_attribute("class")

    # Turn the "★ Favorites" filter ON → only the starred project shows.
    authed_page.locator("#favFilterBtn").click()
    expect(authed_page.locator("#claudeList .coding-item")).to_have_count(1)
    assert _order(authed_page) == ["bravo"]
    expect(authed_page.locator("#favFilterBtn")).to_have_attribute(
        "aria-pressed", "true"
    )

    # Turn it OFF → full favorites-first list returns.
    authed_page.locator("#favFilterBtn").click()
    expect(authed_page.locator("#claudeList .coding-item")).to_have_count(4)
    assert _order(authed_page) == ["bravo", "alpha", "charlie", "delta"]


def test_star_toggle_reorders_and_persists(authed_page: Page, base_url: str) -> None:
    _install_routes(authed_page)
    authed_page.add_init_script(
        "window.localStorage.setItem('launcher.codingFavFilter', '0');"
    )
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")

    expect(authed_page.locator("#claudeList .coding-item")).to_have_count(4)
    assert _order(authed_page) == ["bravo", "alpha", "charlie", "delta"]

    # Star "delta" — the POST persists it and the SPA re-fetches, so delta
    # both gains the filled star and jumps into the favorites group (alpha-
    # sorted: bravo then delta), above the now-shorter rest.
    authed_page.locator('.coding-item[data-id="delta"] .star-btn').click()

    # Wait for the reorder to settle, then assert the new order.
    expect(
        authed_page.locator("#claudeList .coding-item").first
    ).to_have_attribute("data-id", "bravo")
    delta_star = authed_page.locator('.coding-item[data-id="delta"] .star-btn')
    expect(delta_star).to_have_attribute("aria-pressed", "true")
    assert _order(authed_page) == ["bravo", "delta", "alpha", "charlie"]

    # Unstar "bravo" → it drops out of the favorites group, back into the
    # alphabetical rest (alpha, bravo, charlie); delta is the sole favorite.
    authed_page.locator('.coding-item[data-id="bravo"] .star-btn').click()
    expect(
        authed_page.locator("#claudeList .coding-item").first
    ).to_have_attribute("data-id", "delta")
    assert _order(authed_page) == ["delta", "alpha", "bravo", "charlie"]
