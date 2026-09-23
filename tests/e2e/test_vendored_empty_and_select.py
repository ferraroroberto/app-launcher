"""Regression pin for #1133 — the vendored empty-state and select-native.

`design_lint` reported both components NOT_ADOPTED while the app hand-rolled
them: a zero-item list rendered a bare sentence in a dashed box (or, on the
Board, a sentence with no glyph), and every `<select>` wore the *text input*
recipe — `min-height`, which iOS Safari ignores on a select, rendering it at
its stubby intrinsic height with a double border in WebKit.

Both are now the scaffold's components, byte-for-byte (`tests/
test_vendored_manifest.py` pins the manifest entry; `design_lint`'s vendored
check pins the bytes). This pins what they render.

The Jobs empty state also never appeared at all: `patchRowsInPlace()` returns
early when the row count is unchanged, and 0 === 0 takes that branch on every
poll, so the flag stayed at its markup default of `hidden`. Pinned here too.
"""
from __future__ import annotations

import json as _json
import re

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.smoke


def _json_route(page: Page, pattern: str, body: dict) -> None:
    page.route(
        re.compile(pattern),
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=_json.dumps(body)),
    )


def _empty_everything(page: Page) -> None:
    _json_route(page, r".*/api/jobs(\?.*)?$", {"jobs": []})
    _json_route(page, r".*/api/apps$", {"scan_root": "", "apps": []})
    _json_route(page, r".*/api/board(\?.*)?$", {
        "columns": {}, "github": {"fetched_at": "2026-09-22T10:00:00Z"},
        "live_sessions": {"available": True},
    })
    _json_route(page, r".*/api/life-os/skills(\?.*)?$", {
        "available": True, "life_os_dir": "", "skills": []})
    _json_route(page, r".*/api/life-os/recap-status$", {
        "available": False, "ledger_exists": False, "age_days": None,
        "staleness": "fresh", "proposal_pending": False, "proposal_name": None})


def test_zero_item_lists_render_the_canonical_empty_state(
    authed_page: Page, base_url: str
) -> None:
    page = authed_page
    _empty_everything(page)
    # Hold the first jobs answer: until it lands the list is loading, not
    # empty, and says so; the sort button is labelled already (#1176).
    held, released = [], []

    def _hold_jobs(route):
        if released:
            route.fulfill(status=200, content_type="application/json", body='{"jobs": []}')
        else:
            held.append(route)

    page.route(re.compile(r".*/api/jobs(\?.*)?$"), _hold_jobs)
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.locator("#tabJobs").click()
    page.evaluate("document.querySelectorAll('details').forEach((d) => { d.open = true; })")
    expect(page.locator("#jobsLoading")).to_be_visible()
    expect(page.locator("#jobsLoading")).to_contain_text("Loading jobs")
    expect(page.locator("#jobsEmpty")).to_be_hidden()
    expect(page.locator("#jobsSortBtn")).to_have_text(re.compile(r"\S"))
    released.append(True)
    for route in held:
        route.fulfill(status=200, content_type="application/json", body='{"jobs": []}')
    expect(page.locator("#jobsLoading")).to_be_hidden()

    for tab, empty_id in (
        ("#tabClaude", "#claudeEmpty"),
        ("#tabApps", "#appsEmpty"),
        ("#tabJobs", "#jobsEmpty"),
        ("#tabLifeOS", "#lifeOsEmpty"),
    ):
        page.locator(tab).click()
        page.evaluate("document.querySelectorAll('details').forEach((d) => { d.open = true; })")
        block = page.locator(empty_id)
        expect(block).to_be_visible()
        expect(block).to_have_class(re.compile(r"\bempty-state\b"))
        # The canonical shape: a feature-size muted glyph over one line.
        expect(block.locator(".empty-state-icon")).to_have_count(1)
        expect(block.locator(".empty-state-message")).to_have_count(1)
        assert block.locator(".empty-state-icon").bounding_box()["width"] == 24, (
            f"{empty_id}'s glyph is not at the feature size"
        )


def test_every_board_column_renders_one_when_empty(
    authed_page: Page, base_url: str
) -> None:
    page = authed_page
    _empty_everything(page)
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.locator("#tabBoard").click()
    columns = ("backlog", "claude_turn", "your_turn", "other", "done")
    for key in columns:
        block = page.locator(f".board-empty[data-col='{key}'] .empty-state")
        # Why it is empty, and the control that fills it (#1176).
        expect(block.locator(".empty-state-message")).to_contain_text(
            re.compile(r"Refresh|dispatch bar"))
        expect(block.locator(".empty-state-icon")).to_have_count(1)


def test_a_board_column_with_cards_hides_its_empty_state(
    authed_page: Page, base_url: str
) -> None:
    """The other half of `hidden`: the vendored block is `display: flex`, so
    it would out-render the hidden attribute without the app's global
    `[hidden] { display: none !important }`.

    A **guard**, not a regression pin: it passes against pre-#1133 code too
    (the old empty was a plain div). It is here because adopting a
    `display: flex` component is exactly what breaks `hidden` in an app that
    lacks that global rule — the next component adoption should keep it.
    """
    page = authed_page
    _empty_everything(page)
    _json_route(page, r".*/api/board(\?.*)?$", {
        "columns": {"backlog": [{
            "kind": "issue", "key": "app-launcher#1", "repo": "app-launcher",
            "number": 1, "title": "A synthetic issue", "url": "https://example.com/1",
            "labels": [], "updated_at": "2026-09-22T10:00:00Z",
        }]},
        "github": {"fetched_at": "2026-09-22T10:00:00Z"},
        "live_sessions": {"available": True},
    })
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.locator("#tabBoard").click()
    expect(page.locator(".board-list[data-col='backlog'] li")).to_have_count(1)
    expect(page.locator(".board-empty[data-col='backlog']")).to_be_hidden()


def test_selects_wear_the_vendored_control_recipe(
    authed_page: Page, base_url: str
) -> None:
    """A <select> is a control, not a text input: the height has to come from
    `height` (iOS Safari ignores `min-height` on a select).

    Which height depends on where it stands, and #1124 settled that: the
    component's 36px is the inline-toolbar-control contract, while a select
    standing as a stacked form field is a row the user taps and takes the
    44px touch floor.
    """
    page = authed_page
    _empty_everything(page)
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.locator("#tabJobs").click()
    page.evaluate("document.querySelectorAll('details').forEach((d) => { d.open = true; })")
    page.locator("#jobsEditBtn").click()
    page.locator("#jobsAddBtn").click()
    select = page.locator("#jobKindInput")
    expect(select).to_be_visible()
    expect(select).to_have_class(re.compile(r"\bselect-native\b"))
    # A stacked form field is a row the user taps, so it takes the 44px touch
    # floor (#1124) — still from `height`, which is the half iOS honours. The
    # component's own 36px stays the contract for an *inline* toolbar select
    # sharing a row with a toggle and an input.
    expect(select).to_have_css("height", "44px")
    # Every select in the app shares that one recipe.
    assert page.evaluate(
        "() => Array.from(document.querySelectorAll('select'))"
        ".filter((s) => !s.classList.contains('select-native')).length"
    ) == 0, "a <select> is still wearing the text-input recipe"


def test_editor_dialogs_wear_the_vendored_modal_shell(
    authed_page: Page, base_url: str
) -> None:
    """The job editor is the scaffold's modal (#1133), not a hand-rolled one.

    The hand-rolled dialogs stacked each field's label above its control *and*
    drew a divider between fields, so the separation was doubled. The modal
    contract puts the label and value on one row over a single divider, with
    the × close in the header and exactly one full-width primary in the
    footer.
    """
    page = authed_page
    _empty_everything(page)
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.locator("#tabJobs").click()
    page.evaluate("document.querySelectorAll('details').forEach((d) => { d.open = true; })")
    page.locator("#jobsEditBtn").click()
    page.locator("#jobsAddBtn").click()

    dialog = page.locator("#jobDialog")
    expect(dialog).to_be_visible()
    expect(dialog).to_have_class(re.compile(r"\bdetail-dialog\b"))
    expect(dialog.locator(".detail-header .detail-close")).to_have_count(1)

    # Label and value share one line: their boxes overlap vertically.
    label = dialog.locator(".row", has=page.locator("#jobNameInput")).locator("span").first
    name_box, label_box = page.locator("#jobNameInput").bounding_box(), label.bounding_box()
    assert name_box and label_box
    assert label_box["y"] < name_box["y"] + name_box["height"] and name_box["y"] < label_box["y"] + label_box["height"], (
        f"the Name label sits above its field, not beside it: label={label_box}, field={name_box}"
    )
    assert label_box["x"] < name_box["x"], "the label must lead the row, the value follows"

    # One visible footer action, the full-width primary.
    visible = dialog.locator(".detail-actions button:visible")
    expect(visible).to_have_count(1)
    expect(visible).to_have_class(re.compile(r"\bdetail-save-btn\b"))
    save_box = visible.bounding_box()
    card_box = dialog.locator(".detail-card").bounding_box()
    assert save_box and card_box
    assert save_box["width"] >= card_box["width"] - 2 * 18 - 2, (
        f"footer primary is not full-width: {save_box['width']} of card {card_box['width']}"
    )
    # The primary is design.md's button-primary: 48px, not the app's 36px
    # --control-h inline-control height (#1173, project-scaffolding#280).
    assert save_box["height"] >= 47.5, (
        f"footer primary renders {save_box['height']}px, under button-primary's 48px"
    )
