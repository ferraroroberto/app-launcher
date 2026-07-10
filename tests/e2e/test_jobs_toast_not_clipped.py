"""Regression pin for issue #404 (toast clipped behind the bottom nav bar).

``#toast`` must be a body-level sibling of ``<main class="app">``, not
nested inside it — the identical structural fix already applied to the nav
itself in issue #355 (see the comment above ``<nav class="tabs">`` in
index.html). A ``position: fixed`` toast nested inside ``.app`` (the
scroller `.app` becomes on an installed iOS PWA, per nav-tabs.css's
``display-mode: standalone`` block) can get trapped/clipped behind the
equally body-level floating nav bar on real iOS WebKit instead of escaping
to the physical viewport. Also pins that the toast paints above the nav in
the ordinary (non-standalone) layout, which already worked before this fix
and must keep working after it.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.smoke


def test_toast_is_body_level_sibling_of_app(authed_page: Page, base_url: str) -> None:
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    authed_page.wait_for_selector("#sessionsList", state="attached", timeout=5_000)

    is_nested_in_app = authed_page.evaluate(
        "() => !!document.querySelector('main.app #toast')"
    )
    assert not is_nested_in_app, (
        "#toast must not be nested inside <main class=\"app\"> — it needs to "
        "be a body-level sibling like the nav (issue #355), or an installed "
        "iOS PWA can trap/clip it behind the floating nav bar (issue #404)."
    )

    toast_parent_is_body = authed_page.evaluate(
        "() => document.getElementById('toast').parentElement === document.body"
    )
    assert toast_parent_is_body, "#toast's parent should be <body> directly."


def test_toast_renders_above_nav_bar(authed_page: Page, base_url: str) -> None:
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    authed_page.wait_for_selector("#sessionsList", state="attached", timeout=5_000)
    authed_page.locator("#tabJobs").click()

    authed_page.evaluate(
        "() => { const t = document.getElementById('toast'); "
        "t.textContent = 'Regression check'; t.className = 'toast error'; "
        "t.hidden = false; }"
    )
    toast = authed_page.locator("#toast")
    expect(toast).to_be_visible()

    topmost_is_toast = authed_page.evaluate(
        """
        () => {
          const toast = document.getElementById('toast');
          const r = toast.getBoundingClientRect();
          const el = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
          return el === toast;
        }
        """
    )
    assert topmost_is_toast, "toast must be the topmost element at its own center point"
