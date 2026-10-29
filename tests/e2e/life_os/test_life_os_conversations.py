"""Life OS tab e2e: the conversation list, its sheet and the phone fit."""

from __future__ import annotations

import json as _json
import os
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e._geometry import assert_min_target, assert_no_overlap
from tests.e2e._surface import surface
from tests.e2e.fixtures.waits import flush_requests, stable_eval, wait_until
from tests.e2e.jobs.test_jobs_log_copy import _CLIPBOARD_MOCK

from tests.e2e.life_os._support import (
    _FAKE_CONVERSATIONS,
    _close_sheet,
    _convo_rows,
    _mock_conversations,
    _mock_skills,
    _mock_transcript,
    _month_day,
    _open_conversations,
    _open_sheet,
)


pytestmark = pytest.mark.smoke


def test_life_os_skill_conversations_list_under_a_scope_chip(
    authed_page: Page, base_url: str
) -> None:
    """#1504: a skill's ⋮ Conversations empties and focuses the field, narrows
    it to the skill with a chip, and lists that skill's index newest first in
    the Conversations group, where the skill rows step aside. #1528's row:
    the title, then one context line with the date and the summary as plain
    sentences (the digest's labels gone, its literal "none" dropped); a tap
    opens the sheet, and nothing destructive shows on the row."""
    _mock_skills(authed_page)
    _mock_conversations(authed_page)
    authed_page.add_init_script(
        "localStorage.setItem('app-launcher.filter.skills', 'journal')")
    _open_conversations(authed_page, base_url)

    field = authed_page.locator("#lifeOsFilterInput")
    expect(field).to_have_value("")
    expect(field).to_be_focused()
    scope = authed_page.locator("#lifeOsConvoScope")
    expect(scope).to_be_visible()
    expect(scope).to_have_text("in journal-daily")
    expect(authed_page.locator("#lifeOsList")).to_be_hidden()
    expect(authed_page.locator("#lifeOsSkillsHead")).to_be_hidden()

    rows = _convo_rows(authed_page)
    expect(rows).to_have_count(2)
    expect(authed_page.locator("#lifeOsConvoCount")).to_have_text("(2)")
    expect(rows.locator(".action-row-title")).to_have_text(
        ["booking the ferry", "an early trial run"]
    )
    expect(rows.first.locator(".action-row-meta")).to_have_text(re.compile(
        "^" + _month_day("Aug", 1) + r" · took the 07:40\. confirm the return leg\.$"
    ))
    expect(rows.nth(1).locator(".action-row-meta")).to_have_text(
        re.compile("^" + _month_day("Jun", 1) + "$")
    )
    # The skill's avatar leads, and a chevron says the row opens a sheet.
    expect(rows.first.locator(".lifeos-avatar")).to_have_count(1)
    expect(rows.first.locator(".lifeos-convo-chevron")).to_have_count(1)
    expect(rows.locator(".action-row-kebab")).to_have_count(0)
    listing = authed_page.locator("#lifeOsConvoList")
    for label in ("Decisions", "Open loops", "Source", "none"):
        expect(listing).not_to_contain_text(label)


@pytest.mark.iphone
def test_life_os_conversation_sort_is_a_segmented_control(
    authed_page: Page, base_url: str
) -> None:
    """#886/#1504: a skill's list orders by last interaction (Recent) by
    default, flips to creation order (Created) on the labelled segmented
    control, and remembers the choice on the device — all without a second
    fetch. Relevance is a search-only order, so browsing hides it, and the
    row's date is the active order's.

    The fixture is deliberately built so the two orderings disagree: the
    older-*created* "trial" capture is the more recently *touched* one, so a
    list that ignored `last_interaction` would render in the opposite order.
    """
    fetches = {"n": 0}

    def _count(route):
        fetches["n"] += 1
        route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({
                "skill": "journal-daily", "available": True,
                "conversations": [
                    dict(_FAKE_CONVERSATIONS["conversations"][0],
                         last_interaction="2026-08-01"),   # never resumed
                    dict(_FAKE_CONVERSATIONS["conversations"][1],
                         last_interaction="2026-09-05"),   # resumed since
                ],
            }),
        )

    _mock_skills(authed_page)
    authed_page.route(
        re.compile(r".*/api/life-os/skills/journal-daily/conversations$"), _count
    )
    _open_conversations(authed_page, base_url)

    rows = _convo_rows(authed_page)
    titles = rows.locator(".action-row-title")
    expect(rows).to_have_count(2)
    sort = authed_page.locator("#lifeOsConvoSort")
    tab = lambda mode: sort.locator(f".segmented-item[data-sort='{mode}']")  # noqa: E731
    expect(sort).to_have_attribute("data-sort", "interaction")
    expect(tab("relevance")).to_be_hidden()
    expect(tab("interaction")).to_have_text("Recent")
    expect(tab("interaction")).to_have_attribute("aria-pressed", "true")
    expect(tab("created")).to_have_text("Created")

    # Default: most recently interacted with first — the resumed trial run,
    # even though it was created two months before the ferry booking, and
    # its row shows that last interaction.
    expect(titles).to_have_text(["an early trial run", "booking the ferry"])
    expect(rows.first.locator(".action-row-meta")).to_have_text(
        re.compile("^" + _month_day("Sep", 5))
    )

    before = fetches["n"]
    tab("created").click()
    expect(sort).to_have_attribute("data-sort", "created")
    expect(tab("created")).to_have_attribute("aria-pressed", "true")
    expect(titles).to_have_text(["booking the ferry", "an early trial run"])
    expect(rows.nth(1).locator(".action-row-meta")).to_have_text(
        re.compile("^" + _month_day("Jun", 1))   # creation now leads the trial
    )
    flush_requests(authed_page)
    assert fetches["n"] == before, "re-sorting refetched the list"
    # Three peers on one track, each a real target, none overlapping.
    segments = [tab("interaction"), tab("created")]
    for segment in segments:
        assert_min_target(segment)
    assert_no_overlap(segments)

    # The choice sticks across a reload of the whole tab.
    _open_conversations(authed_page, base_url)
    expect(sort).to_have_attribute("data-sort", "created")
    expect(titles.first).to_have_text("booking the ferry")


def test_life_os_conversations_empty_state_when_no_index(
    authed_page: Page, base_url: str
) -> None:
    """A skill the indexer hasn't digested yet gets an honest empty state in
    the group — not a blank pane and not an error toast."""
    _mock_skills(authed_page)
    _mock_conversations(
        authed_page,
        {"skill": "journal-daily", "available": False, "conversations": []},
    )
    _open_conversations(authed_page, base_url)

    state = authed_page.locator("#lifeOsConvoState")
    expect(state).to_be_visible()
    expect(state).to_contain_text("No conversation index yet")
    expect(state).to_have_attribute("data-state", "empty")
    expect(_convo_rows(authed_page)).to_have_count(0)
    expect(authed_page.locator("#toast")).to_be_hidden()


@pytest.mark.iphone
def test_life_os_conversation_controls_are_full_targets(
    authed_page: Page, base_url: str
) -> None:
    """#1528/#1504: the field's scope chip, each conversation row and the
    sheet's ✕, link rows and Resume are 44px targets, and the chip never
    shares a pixel with the box it sits in."""
    _mock_skills(authed_page)
    _mock_conversations(authed_page)
    _open_conversations(authed_page, base_url)

    scope = authed_page.locator("#lifeOsConvoScope")
    assert_min_target(scope)
    assert_min_target(_convo_rows(authed_page).first.locator(".action-row-main"))
    assert_no_overlap([authed_page.locator("#lifeOsFilterInput"), scope])
    sheet = _open_sheet(authed_page, 0)
    assert_min_target(authed_page.locator("#lifeOsConvoSheetClose"))
    links = sheet.locator(".settings-row:visible")
    expect(links).to_have_count(5)   # Read, Rename, Copy link, Open raw, Delete
    for i in range(5):
        assert_min_target(links.nth(i))
    assert_min_target(authed_page.locator("#lifeOsConvoSheetResume"))


# Every text size in a conversation row (its avatar's monogram aside), in one
# read, and the sheet's width against the viewport's.
_MEASURE_ROWS = """root => {
  const sizes = new Set();
  root.querySelectorAll('.lifeos-convo-row *').forEach(el => {
    if (el.closest('.avatar')) return;
    if ([...el.childNodes].some(n => n.nodeType === 3 && n.textContent.trim())) {
      sizes.add(getComputedStyle(el).fontSize);
    }
  });
  return [...sizes].sort();
}"""

_MEASURE_SHEET = """d => {
  const card = d.querySelector('.detail-card').getBoundingClientRect();
  const resume = d.querySelector('#lifeOsConvoSheetResume').getBoundingClientRect();
  return { vw: window.innerWidth, left: card.left, right: card.right,
           cardWidth: card.width, resumeWidth: resume.width,
           overflow: d.scrollWidth - d.clientWidth };
}"""


@pytest.mark.iphone
def test_life_os_conversation_rows_and_sheet_fit_the_phone(
    authed_page: Page, base_url: str
) -> None:
    """#1528: a row uses two type sizes only, 16px for its title and 14px for
    everything else. #1504: at 390px the conversation sheet sits inside the
    screen with nothing to scroll sideways, and Resume runs the width of its
    footer, in light and dark."""
    authed_page.set_viewport_size({"width": 390, "height": 844})
    _mock_skills(authed_page)
    _mock_conversations(authed_page)
    _open_conversations(authed_page, base_url)
    expect(_convo_rows(authed_page)).to_have_count(2)

    sizes = stable_eval(authed_page.locator("#lifeOsConvoList"), _MEASURE_ROWS)
    assert sizes == ["14px", "16px"], f"a row mixes other type sizes: {sizes}"

    sheet = _open_sheet(authed_page, 0)
    # A long name ellipsizes: the header h2 is a flex row, where
    # text-overflow does nothing and the title was cut mid-letter.
    title = authed_page.locator("#lifeOsConvoSheetTitle")
    expect(title).to_have_css("display", "block")
    expect(title).to_have_css("text-overflow", "ellipsis")
    for theme in ("light", "dark"):
        authed_page.evaluate("t => document.documentElement.dataset.theme = t", theme)
        m = sheet.evaluate(_MEASURE_SHEET)
        assert m["left"] >= 0 and m["right"] <= m["vw"] + 0.5, (theme, m)
        assert m["overflow"] <= 0, (theme, m)
        assert m["resumeWidth"] >= m["cardWidth"] - 2 * 18 - 1, (theme, m)


def test_life_os_sheet_summary_is_one_paragraph_for_both_index_shapes(
    authed_page: Page, base_url: str
) -> None:
    """#1528, in the sheet since #1504: the meta line says the skill, when,
    how many messages and which harness, then the summary as one unlabelled
    paragraph — a newer index entry's ``summary`` as written
    (fleet-config#1373), an older entry's decisions and open loops as
    consecutive sentences, and nothing at all for an entry whose digest was
    "none" on both counts."""
    newer = dict(
        _FAKE_CONVERSATIONS["conversations"][0],
        file="2026-08-02-1000-ferry-plan.md",
        path=".claude/skills/journal-daily/conversations/2026-08-02-1000-ferry-plan.md",
        date="2026-08-02", topic="planning the ferry trip", decisions="", open_loops="",
        summary="Booked the early sailing; the return leg is still open.",
    )
    _mock_skills(authed_page)
    _mock_conversations(authed_page, dict(
        _FAKE_CONVERSATIONS, conversations=[newer] + _FAKE_CONVERSATIONS["conversations"]))
    _open_conversations(authed_page, base_url)
    rows = _convo_rows(authed_page)
    expect(rows).to_have_count(3)
    expect(rows.first.locator(".action-row-meta")).to_contain_text(
        "Booked the early sailing; the return leg is still open.")

    summary = authed_page.locator("#lifeOsConvoSheetSummary")
    meta = authed_page.locator("#lifeOsConvoSheetMeta")
    for index, text in ((0, "Booked the early sailing; the return leg is still open."),
                        (1, "took the 07:40. confirm the return leg.")):
        sheet = _open_sheet(authed_page, index)
        expect(summary).to_have_text(text)
        expect(meta).to_have_text(re.compile(
            r"^journal-daily · " + _month_day("Aug", index and 1 or 2)
            + r" · 12 messages · from Claude$"))
        for label in ("Decisions", "Open loops", "Source"):
            expect(sheet).not_to_contain_text(label)
        _close_sheet(authed_page)
    _open_sheet(authed_page, 2)
    expect(summary).to_be_hidden()
    expect(meta).to_contain_text("4 messages")


def test_life_os_unresumable_conversation_says_so(
    authed_page: Page, base_url: str
) -> None:
    """#727, in the sheet since #1504: a capture with no stored session id is
    readable but cannot be reopened. A phone has no hover, so the reason is a
    visible note and Resume stays, disabled — and roughly a quarter of the
    archive is in this state, so it must not read as breakage."""
    _mock_skills(authed_page)
    _mock_conversations(authed_page)
    _open_conversations(authed_page, base_url)

    resume = authed_page.locator("#lifeOsConvoSheetResume")
    note = authed_page.locator("#lifeOsConvoSheetNote")
    _open_sheet(authed_page, 1)
    expect(note).to_be_visible()
    expect(note).to_contain_text("readable only")
    expect(resume).to_be_disabled()
    expect(authed_page.locator("#lifeOsConvoSheet .lifeos-convo-read")).to_be_visible()
    _close_sheet(authed_page)

    # The resumable row is the contrast: it offers the action, and says
    # nothing about why it could not.
    _open_sheet(authed_page, 0)
    expect(note).to_be_hidden()
    expect(resume).to_be_enabled()


def test_life_os_sheet_has_resume_primary_and_link_rows(
    authed_page: Page, base_url: str
) -> None:
    """#1504: the sheet's one primary is Resume, and every other action of the
    old row menu and reader menu is a link row — Read, Rename, Copy link,
    Open raw — with Delete alone last in the danger tone. With the
    toolbar on the conversation's own harness there is no handoff to offer.
    Copy link writes the ?convo= link (#1170)."""
    _mock_skills(authed_page)
    _mock_conversations(authed_page)
    authed_page.add_init_script(_CLIPBOARD_MOCK)
    _open_conversations(authed_page, base_url)

    sheet = _open_sheet(authed_page, 0)
    expect(authed_page.locator("#lifeOsConvoSheetTitle")).to_have_text("booking the ferry")
    resume = authed_page.locator("#lifeOsConvoSheetResume")
    expect(resume).to_have_text("Resume")
    expect(resume).to_have_class(re.compile(r"\bbutton-primary\b"))
    expect(sheet.locator(".detail-actions button")).to_have_count(1)
    expect(sheet.locator(".settings-row:visible .settings-row-label")).to_have_text(
        ["Read the conversation", "Rename", "Copy link", "Open raw", "Delete"])
    expect(sheet.locator(".lifeos-convo-handoff")).to_be_hidden()
    delete = sheet.locator(".lifeos-convo-delete")
    expect(delete).to_have_class(re.compile(r"\blifeos-sheet-danger\b"))
    # Delete is alone in the last box, after every other row.
    expect(delete.locator("xpath=..")).to_have_class(re.compile(r"\blifeos-sheet-links\b"))
    expect(delete.locator("xpath=../button")).to_have_count(1)

    sheet.locator(".lifeos-convo-copy-link").click()
    authed_page.wait_for_function(
        "() => Array.isArray(window.__copied) && window.__copied.length > 0")
    assert authed_page.evaluate("() => window.__copied[0]") == (
        f"{base_url}/?convo=journal-daily/2026-08-01-0900-ferry-booking.md")


def test_life_os_convo_link_opens_the_sheet_and_the_reader(
    authed_page: Page, base_url: str
) -> None:
    """#1170, through the sheet since #1504: a copied
    ``?convo=<skill>/<file>`` link lands on the Life OS tab with that capture
    in the reader, stacked over its conversation sheet, and strips the param.
    A link that no longer resolves says so in the Conversations group — never
    a blank pane.

    #1222: the link must not wait on the rest of boot. The git-status fan-out
    that ran ahead of it took 1–7 s on an idle box and timed the link out
    under load, so it is held pending here for the whole test — the reader
    has to open with it still in flight."""
    _mock_skills(authed_page)
    _mock_conversations(authed_page)
    _mock_transcript(authed_page)
    authed_page.route(re.compile(r".*/api/claude-code/git-status$"),
                      lambda route: None)
    authed_page.goto(
        f"{base_url}/?convo=journal-daily/2026-08-01-0900-ferry-booking.md",
        wait_until="domcontentloaded")
    viewer = authed_page.locator("#lifeOsConvoViewer")
    expect(viewer).to_be_visible(timeout=10_000)
    expect(authed_page.locator("#lifeOsViewerTitle")).to_have_text("booking the ferry")
    expect(authed_page.locator("#tabLifeOS")).to_have_attribute("aria-selected", "true")
    assert "convo=" not in authed_page.url
    surface(authed_page, "lifeos.convo-viewer")
    # ✕ comes back to the sheet the link opened under it.
    authed_page.locator("#lifeOsViewerClose").click()
    expect(viewer).to_be_hidden()
    expect(authed_page.locator("#lifeOsConvoSheetTitle")).to_have_text("booking the ferry")
    expect(authed_page.locator("#lifeOsConvoSheet")).to_be_visible()
    surface(authed_page, "lifeos.convo-sheet")

    authed_page.goto(f"{base_url}/?convo=journal-daily/2026-01-01-gone.md",
                     wait_until="domcontentloaded")
    state = authed_page.locator("#lifeOsConvoState")
    expect(state).to_contain_text("no longer matches", timeout=10_000)
    expect(viewer).to_be_hidden()
    expect(authed_page.locator("#lifeOsConvoSheet")).to_be_hidden()

    authed_page.goto(f"{base_url}/?convo=no-such-skill/x.md",
                     wait_until="domcontentloaded")
    expect(state).to_contain_text("no-such-skill", timeout=10_000)


def test_life_os_sheet_delete_confirms_then_returns_to_the_refreshed_list(
    authed_page: Page, base_url: str
) -> None:
    """#1410, from the sheet since #1504: Delete asks first (naming the
    conversation and what goes), Cancel leaves everything alone, a refusal
    keeps the sheet and says why, and confirming sends one DELETE for that
    capture, closes the sheet and the reader and lands back on the refreshed
    list without the row, with a toast."""
    _mock_skills(authed_page)
    _mock_transcript(authed_page)
    remaining = list(_FAKE_CONVERSATIONS["conversations"])
    deletes: list = []
    refuse = {"on": True}

    def _list(route):
        route.fulfill(status=200, content_type="application/json", body=_json.dumps(
            {**_FAKE_CONVERSATIONS, "conversations": remaining}))

    def _delete(route):
        deletes.append(route.request.url)
        if refuse["on"]:
            route.fulfill(status=409, content_type="application/json", body=_json.dumps(
                {"detail": "Nothing was deleted: this conversation is open in a running session."}))
            return
        remaining.pop(0)
        route.fulfill(status=200, content_type="application/json", body=_json.dumps(
            {"deleted": "x", "removed": {"capture": True, "transcript": "removed"}}))

    authed_page.route(re.compile(r".*/api/life-os/skills/journal-daily/conversations$"), _list)
    authed_page.route(re.compile(r".*/api/life-os/skills/journal-daily/conversations\?path=.*"),
                      lambda route: _delete(route) if route.request.method == "DELETE"
                      else route.fallback())
    _open_conversations(authed_page, base_url)
    rows = _convo_rows(authed_page)
    expect(rows).to_have_count(2)
    sheet = _open_sheet(authed_page, 0)
    delete = sheet.locator(".lifeos-convo-delete")

    # Cancel: the confirm sheet (#1439, never a native confirm()) stacks over
    # this one, names the conversation and what goes; its ✕ sends nothing.
    native: list = []
    authed_page.on("dialog", lambda d: (native.append(d.message), d.dismiss()))
    confirm = authed_page.locator("#confirmDialog")
    delete.click()
    expect(confirm).to_be_visible()
    expect(authed_page.locator("#confirmDialogTitle")).to_contain_text("booking the ferry")
    expect(authed_page.locator("#confirmDialogMessage")).to_contain_text("transcript")
    expect(authed_page.locator("#confirmDialogMessage")).to_contain_text("cannot be undone")
    authed_page.locator("#confirmDialogClose").click()
    expect(confirm).to_be_hidden()
    expect(sheet).to_be_visible()
    flush_requests(authed_page)
    assert deletes == []

    # A refusal (the conversation is open in a running session): all stays.
    delete.click()
    authed_page.locator("#confirmDialogOk").click()
    wait_until(authed_page, lambda: len(deletes) == 1, "the refused DELETE")
    expect(authed_page.locator("#toast")).to_contain_text("running session")
    expect(sheet).to_be_visible()
    expect(rows).to_have_count(2)

    # Confirm, from under the reader: one DELETE for that capture, then the
    # refreshed list with every layer closed.
    refuse["on"] = False
    sheet.locator(".lifeos-convo-read").click()
    expect(authed_page.locator("#lifeOsConvoViewer")).to_be_visible()
    authed_page.locator("#lifeOsViewerClose").click()
    delete.click()
    authed_page.locator("#confirmDialogOk").click()
    wait_until(authed_page, lambda: len(deletes) == 2, "the confirmed DELETE")
    assert "path=.claude%2Fskills%2Fjournal-daily%2Fconversations%2F" \
        "2026-08-01-0900-ferry-booking.md" in deletes[1]
    expect(rows).to_have_count(1)
    expect(rows.first.locator(".action-row-title")).to_have_text("an early trial run")
    expect(authed_page.locator("#toast")).to_contain_text("Deleted booking the ferry")
    expect(sheet).to_be_hidden()
    expect(authed_page.locator("#lifeOsConvoViewer")).to_be_hidden()
    assert native == [], native


@pytest.mark.iphone
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_life_os_bot_chat_row_shows_its_fixed_title_beside_a_normal_row(
    authed_page: Page, base_url: str, theme: str
) -> None:
    """#1952: the server titles a bot chat ``Telegram bot - <skill>``; the list
    shows that title where a normal row shows its digest topic, whole inside
    the viewport at 390 px in both themes. Set E2E_SHOT_DIR to keep a
    screenshot of each (local review only; stubbed data)."""
    bot, normal = _FAKE_CONVERSATIONS["conversations"]
    body = {**_FAKE_CONVERSATIONS, "conversations": [
        {**bot, "title": "Telegram bot - journal-daily"}, normal]}
    authed_page.set_viewport_size({"width": 390, "height": 844})
    authed_page.emulate_media(color_scheme=theme)
    _mock_skills(authed_page)
    _mock_conversations(authed_page, body)
    _open_conversations(authed_page, base_url)
    authed_page.evaluate("t => { document.documentElement.dataset.theme = t; }", theme)
    rows = _convo_rows(authed_page)
    expect(rows).to_have_count(2)
    titles = rows.locator(".action-row-title")
    expect(titles.nth(0)).to_have_text("Telegram bot - journal-daily")
    expect(titles.nth(1)).to_have_text("an early trial run")
    assert stable_eval(titles.nth(0), "el => el.scrollWidth <= el.clientWidth + 1") is True
    shot_dir = os.environ.get("E2E_SHOT_DIR")
    if shot_dir:
        authed_page.screenshot(path=os.path.join(shot_dir, f"bot-chat-row-{theme}.png"))
