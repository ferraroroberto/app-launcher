"""Life OS tab e2e: renaming and resuming a conversation."""

from __future__ import annotations

import json as _json
import os
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.fixtures.ui import life_launch_switch
from tests.e2e.fixtures.waits import flush_requests, wait_until

from tests.e2e.life_os._support import (
    _FAKE_CONVERSATIONS,
    _RESUMABLE_SID,
    _close_sheet,
    _convo_rows,
    _mock_conversations,
    _mock_skills,
    _open_conversations,
    _open_life,
    _open_sheet,
    _pick_life_model,
    _skill_menu_item,
)


pytestmark = pytest.mark.smoke


def _selection(page: Page) -> tuple:
    """The rename input's selected range, as (start, end)."""
    return tuple(page.locator("#confirmDialogInput").evaluate(
        "el => [el.selectionStart, el.selectionEnd]"))


def _mock_renamable_conversations(page: Page):
    """A stateful stand-in for the list + PATCH: what the server would keep.

    Returns ``(patches, titles)``: every PATCH ``(url, body)`` received, and
    the titles now stored. The list GET answers from ``titles`` the way
    index.json does, so a reload shows exactly what the last PATCH left behind.
    """
    patches: list = []
    titles: dict = {}

    def _list(route):
        rows = [{**row, "title": titles.get(row["file"], "")}
                for row in _FAKE_CONVERSATIONS["conversations"]]
        route.fulfill(status=200, content_type="application/json",
                      body=_json.dumps({**_FAKE_CONVERSATIONS, "conversations": rows}))

    def _patch(route):
        if route.request.method != "PATCH":
            route.fallback()
            return
        body = route.request.post_data_json
        patches.append((route.request.url, body))
        file = _FAKE_CONVERSATIONS["conversations"][0]["file"]
        if body["title"]:
            titles[file] = body["title"]
        else:
            titles.pop(file, None)
        route.fulfill(status=200, content_type="application/json",
                      body=_json.dumps({"file": file, "title": body["title"]}))

    page.route(re.compile(r".*/api/life-os/skills/journal-daily/conversations$"), _list)
    page.route(re.compile(r".*/api/life-os/skills/journal-daily/conversations\?path=.*"), _patch)
    return patches, titles


def test_life_os_sheet_rename_survives_a_reload_and_clears_back_to_the_topic(
    authed_page: Page, base_url: str
) -> None:
    """#1495, from the sheet since #1504: Rename opens the rename dialog over
    the sheet (never a native prompt()), Save sends one PATCH with the typed
    name, and the sheet and the list show it — and only it: #1528 dropped
    the digest topic's muted second line. Reopened, the list still shows it,
    and saving the field empty clears it back to the topic."""
    _mock_skills(authed_page)
    patches, titles = _mock_renamable_conversations(authed_page)
    native: list = []
    authed_page.on("dialog", lambda d: (native.append(d.message), d.dismiss()))
    _open_conversations(authed_page, base_url)
    rows = _convo_rows(authed_page)
    first = rows.first
    expect(first.locator(".action-row-title")).to_have_text("booking the ferry")

    dialog = authed_page.locator("#confirmDialog")
    rename = _open_sheet(authed_page, 0).locator(".lifeos-convo-rename")
    rename.click()
    expect(dialog).to_be_visible()
    expect(authed_page.locator("#confirmDialogTitle")).to_have_text("Rename conversation")
    # An untitled conversation opens on its topic, selected (#1953).
    expect(authed_page.locator("#confirmDialogInput")).to_have_value("booking the ferry")
    expect(authed_page.locator("#confirmDialogInput")).to_be_focused()
    assert _selection(authed_page) == (0, len("booking the ferry"))
    # The header ✕ cancels without a request, back on the sheet.
    authed_page.locator("#confirmDialogClose").click()
    expect(dialog).to_be_hidden()
    expect(authed_page.locator("#lifeOsConvoSheet")).to_be_visible()
    flush_requests(authed_page)
    assert patches == []

    # Saving the untouched topic writes nothing: it is already the shown name.
    rename.click()
    authed_page.locator("#confirmDialogOk").click()
    expect(dialog).to_be_hidden()
    flush_requests(authed_page)
    assert patches == []

    rename.click()
    authed_page.locator("#confirmDialogInput").fill("  Ferry  to the island ")
    authed_page.locator("#confirmDialogOk").click()
    wait_until(authed_page, lambda: len(patches) == 1, "the rename PATCH")
    assert patches[0][1] == {"title": "Ferry to the island"}
    assert "path=.claude%2Fskills%2Fjournal-daily%2Fconversations%2F" \
        "2026-08-01-0900-ferry-booking.md" in patches[0][0]
    expect(authed_page.locator("#toast")).to_contain_text("Renamed to Ferry to the island")
    expect(authed_page.locator("#lifeOsConvoSheetTitle")).to_have_text("Ferry to the island")
    expect(first.locator(".action-row-title")).to_have_text("Ferry to the island")
    expect(first).not_to_contain_text("booking the ferry")
    _close_sheet(authed_page)

    # Widen and narrow again: the list is fetched afresh, so the stored name
    # is what it loads. (Not a second page.goto: a full document load is the
    # step that times out on a loaded gate box, and this test is about the
    # list, not the boot. The server leg is pinned in the API tests.)
    authed_page.locator("#lifeOsConvoScope").click()
    expect(authed_page.locator("#lifeOsConvoGroup")).to_be_hidden()
    _skill_menu_item(authed_page, "journal-daily", "[data-item=lifeos-convo-btn]").click()
    expect(first.locator(".action-row-title")).to_have_text("Ferry to the island")
    expect(first).not_to_contain_text("booking the ferry")

    # The dialog opens on the current title; saving it empty clears it.
    _open_sheet(authed_page, 0).locator(".lifeos-convo-rename").click()
    expect(authed_page.locator("#confirmDialogInput")).to_have_value("Ferry to the island")
    authed_page.locator("#confirmDialogInput").fill("")
    authed_page.locator("#confirmDialogOk").click()
    wait_until(authed_page, lambda: len(patches) == 2, "the clearing PATCH")
    assert patches[1][1] == {"title": ""}
    assert titles == {}
    expect(authed_page.locator("#lifeOsConvoSheetTitle")).to_have_text("booking the ferry")
    expect(first.locator(".action-row-title")).to_have_text("booking the ferry")
    assert native == [], native


@pytest.mark.iphone
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_life_os_rename_sheet_prefill_fits_at_phone_width(
    authed_page: Page, base_url: str, theme: str
) -> None:
    """#1953: at 390 px the pre-filled topic sits whole inside the input and
    the input inside the sheet's card, in both themes. Set E2E_SHOT_DIR to
    keep a screenshot of each (local review only; stubbed data)."""
    authed_page.set_viewport_size({"width": 390, "height": 844})
    authed_page.emulate_media(color_scheme=theme)
    _mock_skills(authed_page)
    _mock_renamable_conversations(authed_page)
    _open_conversations(authed_page, base_url)
    authed_page.evaluate("t => { document.documentElement.dataset.theme = t; }", theme)
    _open_sheet(authed_page, 0).locator(".lifeos-convo-rename").click()
    field = authed_page.locator("#confirmDialogInput")
    expect(field).to_have_value("booking the ferry")
    assert _selection(authed_page) == (0, len("booking the ferry"))
    fit = field.evaluate("""el => {
        const box = el.getBoundingClientRect();
        const card = el.closest('.detail-card').getBoundingClientRect();
        return {
            textFits: el.scrollWidth <= el.clientWidth,
            insideCard: box.left >= card.left && box.right <= card.right,
            insideViewport: box.left >= 0 && box.right <= window.innerWidth,
        };
    }""")
    assert fit == {"textFits": True, "insideCard": True, "insideViewport": True}, fit
    shot_dir = os.environ.get("E2E_SHOT_DIR")
    if shot_dir:
        authed_page.screenshot(
            path=os.path.join(shot_dir, f"rename-prefill-{theme}.png"))


@pytest.mark.iphone
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_life_os_rename_prefill_of_a_long_topic_is_cut_to_the_title_limit(
    authed_page: Page, base_url: str, theme: str
) -> None:
    """#1972: a topic over the 120-character title limit pre-fills cut at a
    word, so the field is valid before anything is typed (no "Use no more than
    120 characters") and Save closes the sheet, writing nothing. Set
    E2E_SHOT_DIR to keep a screenshot of each theme (local review only)."""
    long_topic = " ".join(["plan the autumn trip around the lakes and book the slow ferry"] * 5)
    first, *rest = _FAKE_CONVERSATIONS["conversations"]
    authed_page.set_viewport_size({"width": 390, "height": 844})
    authed_page.emulate_media(color_scheme=theme)
    _mock_skills(authed_page)
    _mock_conversations(authed_page, {
        **_FAKE_CONVERSATIONS, "conversations": [{**first, "topic": long_topic}, *rest]})
    patches: list = []
    authed_page.route(
        re.compile(r".*/api/life-os/skills/journal-daily/conversations\?path=.*"),
        lambda route: (patches.append(route.request.url), route.fallback())
        if route.request.method == "PATCH" else route.fallback())
    _open_conversations(authed_page, base_url)
    authed_page.evaluate("t => { document.documentElement.dataset.theme = t; }", theme)
    _open_sheet(authed_page, 0).locator(".lifeos-convo-rename").click()
    field = authed_page.locator("#confirmDialogInput")
    expect(field).to_be_focused()
    value = field.input_value()
    assert 0 < len(value) <= 120 and long_topic.startswith(value), value
    assert long_topic[len(value)] == " ", "cut on a word boundary"
    assert field.evaluate("el => el.validity.valid && el.validationMessage === ''")
    shot_dir = os.environ.get("E2E_SHOT_DIR")
    if shot_dir:
        authed_page.screenshot(
            path=os.path.join(shot_dir, f"rename-long-topic-{theme}.png"))
    authed_page.locator("#confirmDialogOk").click()
    expect(authed_page.locator("#confirmDialog")).to_be_hidden()
    flush_requests(authed_page)
    assert patches == []


def test_life_os_rename_refusal_keeps_the_row_and_says_why(
    authed_page: Page, base_url: str
) -> None:
    """#1495: a refused rename (the title writer says no) toasts the server's
    reason and leaves the name unchanged."""
    _mock_skills(authed_page)
    _mock_conversations(authed_page)
    authed_page.route(
        re.compile(r".*/api/life-os/skills/journal-daily/conversations\?path=.*"),
        lambda route: route.fulfill(
            status=404, content_type="application/json",
            body=_json.dumps({"detail": "That conversation is no longer in the index; refresh the list."}))
        if route.request.method == "PATCH" else route.fallback())
    _open_conversations(authed_page, base_url)
    first = _convo_rows(authed_page).first
    _open_sheet(authed_page, 0).locator(".lifeos-convo-rename").click()
    authed_page.locator("#confirmDialogInput").fill("Nope")
    authed_page.locator("#confirmDialogOk").click()
    expect(authed_page.locator("#toast")).to_contain_text("no longer in the index")
    expect(authed_page.locator("#lifeOsConvoSheetTitle")).to_have_text("booking the ferry")
    expect(first.locator(".action-row-title")).to_have_text("booking the ferry")


@pytest.mark.iphone
def test_life_os_conversation_resume_posts_the_session_id(
    authed_page: Page, base_url: str
) -> None:
    """#727, the point of the whole feature: the sheet's Resume posts that
    exact capture — no native picker — honouring the Skills toolbar's
    Detached switch like every other Life OS launch, and keeping the
    conversation's own model (#1503) whatever the toolbar's model says. The
    sheet closes as the launch lands."""
    _mock_skills(authed_page)
    _mock_conversations(authed_page)
    launches: list = []

    def _fulfill_launch(route):
        launches.append(_json.loads(route.request.post_data or ""))
        route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({
                "launched": "journal-daily", "name": "journal-daily",
                "agent": "claude", "mode": "remote", "model": "claude-opus-5-5",
                "model_kept": True, "resume": True, "resume_sid": _RESUMABLE_SID,
                "session": {"session_id": "x", "kind": "remote"},
            }),
        )

    authed_page.route(
        re.compile(r".*/api/life-os/skills/journal-daily/conversations/launch$"),
        _fulfill_launch,
    )
    _open_life(authed_page, base_url)
    # Detached on, so the resume lands in a console instead of opening the
    # terminal overlay (nothing to tear down in the assertion).
    life_launch_switch(authed_page, "#lifeOsDetached")
    _pick_life_model(authed_page, "claude:opus")
    _skill_menu_item(authed_page, "journal-daily", "[data-item=lifeos-convo-btn]").click()

    _open_sheet(authed_page, 0)
    authed_page.locator("#lifeOsConvoSheetResume").click()
    wait_until(authed_page, lambda: len(launches) == 1, "the resume POST")
    # The resume keeps the conversation's own model (#1503): the server
    # leaves --model out on `keep_model`, whatever the toolbar says.
    assert launches[0] == {
        "mode": "remote", "model": "claude:opus", "action": "resume",
        "capture": {key: _FAKE_CONVERSATIONS["conversations"][0][key]
                    for key in ("path", "revision", "agent", "sid")},
        "keep_model": True,
    }, launches
    expect(authed_page.locator("#lifeOsConvoSheet")).to_be_hidden()
    expect(authed_page.locator("#toast")).to_contain_text("Resumed booking the ferry (Opus)")
