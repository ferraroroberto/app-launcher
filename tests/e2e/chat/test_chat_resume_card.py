"""Chat's latest-turn pill and the resume card: list, search and resume from the picker or the composer."""

from __future__ import annotations

import json as _json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import (
    OVERLAY_OPEN_MS,
    stable_eval,
    stable_read,
)

from tests.e2e.chat._support import (
    _Picker,
    _RESUMABLE,
    _Resume,
    _SID,
    _boot,
    _open_chat,
    _tick,
    _turn,
)


pytestmark = pytest.mark.smoke


@pytest.mark.iphone
def test_latest_pill_brings_a_scrolled_up_reader_back_to_the_newest_turn(
    authed_page: Page, base_url: str
) -> None:
    """#1140: the shared ↓ Latest pill, mounted on the Chat pane. Scrolled up,
    it shows — and stays shown while live turns land (the reader is still
    not looking at them). One tap lands on the newest turn, hides it, and
    hands the pane back to #1050's stick-to-bottom, so the next live turn
    appears on screen by itself."""
    page = authed_page
    tr = _boot(page, base_url)
    tr.append(*[
        _turn("user" if i % 2 == 0 else "assistant",
              f"filler turn {i} " + "with enough words to wrap a line " * 3,
              1000 + i * 10)
        for i in range(30)
    ])
    _open_chat(page)
    listing = page.locator("#transcriptList")
    expect(listing).to_contain_text("filler turn 29", timeout=OVERLAY_OPEN_MS)
    pill = page.locator("#chatLatest")
    expect(pill).to_have_count(1)
    # Opens at the tail: nothing to jump to.
    expect(pill).to_be_hidden()

    page.locator("#transcriptBody").evaluate(
        "el => { el.scrollTop = Math.round(el.scrollHeight * 0.3); }"
    )
    expect(pill).to_be_visible()

    tr.append(_turn("assistant", "a brand new reply", 5000))
    _tick(page, tr)
    expect(listing).to_contain_text("a brand new reply", timeout=OVERLAY_OPEN_MS)
    expect(pill).to_be_visible()

    pill.click()
    expect(pill).to_be_hidden()
    expect(listing.locator(".tr-turn", has_text="a brand new reply")).to_be_in_viewport()

    # Sticking resumed: the next live turn arrives on screen with no scroll.
    tr.append(_turn("user", "and one more after the jump", 6000))
    _tick(page, tr)
    expect(
        listing.locator(".tr-turn", has_text="and one more after the jump")
    ).to_be_in_viewport(timeout=OVERLAY_OPEN_MS)
    expect(pill).to_be_hidden()


@pytest.mark.iphone
def test_a_resume_launch_with_no_transcript_shows_the_card_in_place_of_the_empty_state(
    authed_page: Page, base_url: str
) -> None:
    """#1300, reopened: a session launched with `--resume` has no transcript
    yet, and its screen shows the resume picker from the start. The first
    load's `no_transcript` used to end the view, so the screen was never
    asked about and Chat showed only "No transcript found". The card must
    come up in that line's place, and the line comes back once it is ✕'d."""
    page = authed_page
    picker = _Picker(page)
    picker.resume_up = True
    _Resume(page)
    tr = _boot(page, base_url)
    tr.unavailable = "no_transcript"
    _open_chat(page)

    card = page.locator("#transcriptResumeLive")
    state = page.locator("#transcriptState")
    _tick(page, tr)
    expect(card).to_be_visible(timeout=OVERLAY_OPEN_MS)
    expect(card.locator(".tr-resume-opt")).to_have_count(3)
    expect(state).to_be_hidden()
    box = stable_read(card.bounding_box)
    viewport = page.viewport_size
    assert box and box["x"] >= 0 and box["x"] + box["width"] <= viewport["width"] + 1, box

    card.locator(".tr-resume-close").click()
    expect(card).to_be_hidden()
    expect(state).to_contain_text("No transcript found")

    # The view stayed live: once a transcript appears, its turns land by
    # themselves and the reason line goes with them.
    tr.unavailable = None
    _tick(page, tr)
    expect(page.locator("#transcriptList")).to_contain_text(
        "opening conftest now", timeout=OVERLAY_OPEN_MS
    )
    expect(state).to_be_hidden()


@pytest.mark.iphone
def test_the_resume_card_lists_searches_and_resumes_from_the_picker_or_the_composer(
    authed_page: Page, base_url: str
) -> None:
    """#1300: while the terminal shows Claude Code's /resume picker, Chat
    offers the project's sessions as a searchable card, newest first; a pick
    posts that session with via=picker and the card goes. Typing /resume in
    the composer opens the same card instead of sending anything to the
    terminal, and a pick from it posts via=composer."""
    page = authed_page
    tr = _boot(page, base_url)
    picker = _Picker(page)
    resume = _Resume(page)
    typed: list = []
    page.route(
        re.compile(r".*/api/claude-code/sessions/" + _SID + r"/input$"),
        lambda route: (typed.append(route.request.post_data_json),
                       route.fulfill(status=200, content_type="application/json",
                                     body=_json.dumps({"ok": True, "submit_state": "confirmed"}))),
    )
    _open_chat(page)
    card = page.locator("#transcriptResumeLive")
    expect(card).to_be_hidden()

    picker.resume_up = True
    _tick(page, tr)
    expect(card).to_be_visible(timeout=OVERLAY_OPEN_MS)
    rows = card.locator(".tr-resume-opt")
    expect(rows).to_have_count(3)
    expect(rows.locator(".tr-ask-label")).to_have_text(
        ["Fix the login redirect", "Add a dark theme", "Tidy the flaky test"])
    card.locator(".tr-resume-search").fill("THEME dark")
    shown = card.locator(".tr-resume-opt:not([hidden])")
    expect(shown).to_have_count(1)
    expect(shown).to_contain_text("Add a dark theme")
    card.locator(".tr-resume-search").fill("nothing like this")
    expect(shown).to_have_count(0)
    expect(card.locator(".tr-resume-none")).to_be_visible()
    card.locator(".tr-resume-search").fill("theme")
    box = stable_read(card.locator(".tr-resume-close").bounding_box)
    assert box and box["height"] >= 44 and box["width"] >= 44, box
    # The ✕ is a ghost icon button like the Jobs run list's, not the UA's grey
    # square: same computed fill, border, glyph colour and radius as a probe
    # built from that real rule, and its colour reacts on hover (pointer only).
    ghost = (
        "el => { const s = getComputedStyle(el); return el.isConnected ? "
        "{bg: s.backgroundColor, border: s.borderTopWidth, color: s.color, "
        "radius: s.borderTopLeftRadius} : null; }"
    )
    probe = (
        "() => { const ul = document.createElement('ul'); ul.className = 'jobs-runs-list';"
        "ul.hidden = true; const li = document.createElement('li');"
        "const b = document.createElement('button'); b.className = 'icon-button';"
        "li.appendChild(b); ul.appendChild(li); document.body.appendChild(ul);"
        "const s = getComputedStyle(b); const out = {bg: s.backgroundColor,"
        "border: s.borderTopWidth, color: s.color, radius: s.borderTopLeftRadius};"
        "ul.remove(); return out; }"
    )
    close = card.locator(".tr-resume-close")
    sibling = page.evaluate(probe)
    assert sibling["bg"] == "rgba(0, 0, 0, 0)" and sibling["border"] == "0px", sibling
    assert stable_eval(close, ghost) == sibling, sibling
    # Hover only exists on a pointer device; the touch projection has none.
    if page.evaluate("matchMedia('(hover: hover)').matches"):
        close.hover()
        assert stable_eval(close, ghost)["color"] != sibling["color"]
    shown.first.click()
    expect(page.locator("#toast")).to_contain_text("Resumed: Add a dark theme")
    assert resume.picks == [{"session_id": _RESUMABLE[1]["id"], "via": "picker"}], resume.picks
    expect(card).to_be_hidden()

    # The picker closed on the terminal; /resume typed in Chat opens the card.
    picker.resume_up = False
    composer = page.locator("#chatComposeBar")
    composer.locator(".composer-input").fill("/resume")
    composer.locator(".composer-send").click()
    expect(card).to_be_visible()
    expect(composer.locator(".composer-input")).to_have_value("")
    card.locator(".tr-resume-opt").first.click()
    expect(page.locator("#toast")).to_contain_text("Resumed: Fix the login redirect")
    assert resume.picks[-1] == {"session_id": _RESUMABLE[0]["id"], "via": "composer"}, resume.picks
    assert typed == [], "the bare /resume must never reach the terminal"
