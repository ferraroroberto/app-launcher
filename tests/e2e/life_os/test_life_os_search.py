"""Life OS tab e2e: searching conversations and starting new ones from the results."""

from __future__ import annotations

import json as _json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e._geometry import assert_min_target, assert_no_overlap
from tests.e2e.conftest import (
    flush_requests,
    life_launch_switch,
    stable_eval,
    wait_until,
)

from tests.e2e.life_os._support import (
    _FAKE_CONVERSATIONS,
    _SEARCH_URL,
    _convo_rows,
    _mock_conversations,
    _mock_search,
    _mock_skill_launches,
    _mock_skills,
    _open_conversations,
    _open_life,
    _pick_life_model,
    _skill_menu_item,
)


pytestmark = pytest.mark.smoke


def test_life_os_field_searches_conversations_across_every_skill(
    authed_page: Page, base_url: str
) -> None:
    """#1504, decision 9: the skills field filters skill names at once and,
    from two characters after the debounce, adds a Conversations group with
    ranked hits from every skill, each naming its skill (#1528) — the case
    the per-skill index cannot answer. One letter searches nothing."""
    _mock_skills(authed_page)
    seen: list = []
    _mock_search(authed_page, [
        dict(_FAKE_CONVERSATIONS["conversations"][0]),
        dict(_FAKE_CONVERSATIONS["conversations"][1], skill="sparring-work",
             topic="the ferry conversation at work"),
    ], seen=seen)
    _open_life(authed_page, base_url)
    field = authed_page.locator("#lifeOsFilterInput")
    expect(field).to_have_attribute("placeholder", "Skills and conversations")
    group = authed_page.locator("#lifeOsConvoGroup")

    field.fill("s")
    flush_requests(authed_page)
    authed_page.wait_for_timeout(400)   # past the 250 ms debounce
    flush_requests(authed_page)
    assert seen == [], seen
    expect(group).to_be_hidden()

    field.fill("spar")
    rows = _convo_rows(authed_page)
    expect(rows).to_have_count(2, timeout=5_000)
    # Names first: the skills group filters to the match and is counted.
    expect(authed_page.locator("#lifeOsSkillsHead")).to_have_text("Skills (1)")
    expect(authed_page.locator("#lifeOsList li.lifeos-item:visible")).to_have_count(1)
    expect(authed_page.locator("#lifeOsConvoHead")).to_have_text("Conversations (2)")
    # No skill filter in the request, and every row names its own skill.
    assert len(seen) == 1 and "q=spar" in seen[0] and "skill=" not in seen[0], seen
    expect(rows.first.locator(".action-row-meta")).to_contain_text("journal-daily")
    expect(rows.nth(1).locator(".action-row-meta")).to_contain_text("sparring-work")
    expect(rows.nth(1).locator(".lifeos-monogram")).to_have_text("S")
    # The search is the owner's own words: nothing beyond the skills
    # filter's existing value holds them.
    stored = authed_page.evaluate(
        "() => Object.keys(localStorage).filter(k => k !== 'app-launcher.filter.skills'"
        " && String(localStorage.getItem(k)).includes('spar'))")
    assert stored == [], stored

    # Clearing the field ends the search and the group steps aside.
    field.fill("")
    expect(group).to_be_hidden(timeout=5_000)
    expect(authed_page.locator("#lifeOsSkillsHead")).to_be_hidden()


def test_life_os_restored_field_never_searches_conversations(
    authed_page: Page, base_url: str
) -> None:
    """#1504: the text the field restores from the device filters the skills
    alone. No conversation search runs at boot or on opening the tab — only
    typing in this session starts one."""
    _mock_skills(authed_page)
    seen: list = []
    _mock_search(authed_page, [dict(_FAKE_CONVERSATIONS["conversations"][0])], seen=seen)
    authed_page.add_init_script(
        "localStorage.setItem('app-launcher.filter.skills', 'journal')")
    _open_life(authed_page, base_url)
    expect(authed_page.locator("#lifeOsFilterInput")).to_have_value("journal")
    expect(authed_page.locator("#lifeOsList li.lifeos-item:visible")).to_have_count(1)
    authed_page.locator("#tabLifeOS").click()
    authed_page.wait_for_timeout(400)   # past the 250 ms debounce
    flush_requests(authed_page)
    assert seen == [], seen
    expect(authed_page.locator("#lifeOsConvoGroup")).to_be_hidden()


def test_life_os_search_results_keep_server_relevance_order(
    authed_page: Page, base_url: str
) -> None:
    """#1074: the client used to push search hits through the #886 date sort,
    so the best match sank below rows that merely mention the word. #1504:
    while searching, Relevance is the first segment and the default.

    The fixture makes the top-ranked hit the *oldest* row on both date axes,
    so a list that re-sorted by either date would render it last — and the
    three orderings disagree pairwise, so each state of the control is
    distinguishable from the other two.
    """
    _mock_skills(authed_page)

    def _row(topic, date, touched):
        return dict(
            _FAKE_CONVERSATIONS["conversations"][0],
            file=date + "-1200-" + topic.replace(" ", "-") + ".md",
            path=".claude/skills/journal-daily/conversations/"
                 + date + "-1200-" + topic.replace(" ", "-") + ".md",
            topic=topic, date=date, last_interaction=touched,
        )

    #                             created       last interaction
    best = _row("the ferry deep dive", "2026-06-01", "2026-06-02")   # oldest both
    mention = _row("a ferry mention", "2026-08-01", "2026-08-02")    # newest created
    passing = _row("ferry in passing", "2026-07-01", "2026-09-05")   # newest touched
    _mock_search(authed_page, [best, mention, passing])   # server's rank order
    _mock_conversations(authed_page)
    _open_life(authed_page, base_url)

    authed_page.locator("#lifeOsFilterInput").fill("ferry")
    rows = _convo_rows(authed_page)
    expect(rows).to_have_count(3, timeout=5_000)
    titles = rows.locator(".action-row-title")
    sort = authed_page.locator("#lifeOsConvoSort")
    tab = lambda mode: sort.locator(f".segmented-item[data-sort='{mode}']")  # noqa: E731
    expect(titles).to_have_text(
        ["the ferry deep dive", "a ferry mention", "ferry in passing"]
    )
    expect(sort).to_have_attribute("data-sort", "relevance")
    expect(sort.locator(".segmented-item:visible")).to_have_text(["Relevance", "Recent", "Created"])
    # No skill is called "ferry": the count says so, and the no-match block
    # stays out of the way of the conversations under it.
    expect(authed_page.locator("#lifeOsSkillsHead")).to_have_text("Skills (0)")
    expect(authed_page.locator("#lifeOsFilterEmpty")).to_be_hidden()

    tab("interaction").click()
    expect(sort).to_have_attribute("data-sort", "interaction")
    expect(titles).to_have_text(
        ["ferry in passing", "a ferry mention", "the ferry deep dive"]
    )
    tab("created").click()
    expect(sort).to_have_attribute("data-sort", "created")
    expect(titles).to_have_text(
        ["a ferry mention", "ferry in passing", "the ferry deep dive"]
    )

    # …and relevance survives the round trip, because the re-sort runs off
    # the server's array rather than the rendered one.
    tab("relevance").click()
    expect(sort).to_have_attribute("data-sort", "relevance")
    expect(titles).to_have_text(
        ["the ferry deep dive", "a ferry mention", "ferry in passing"]
    )

    # A search's ordering is transient: a skill's list afterwards opens on
    # the browse default, never on the search's date pick.
    tab("created").click()
    authed_page.locator("#lifeOsFilterInput").fill("")
    expect(authed_page.locator("#lifeOsConvoGroup")).to_be_hidden(timeout=5_000)
    _skill_menu_item(authed_page, "journal-daily", "[data-item=lifeos-convo-btn]").click()
    expect(sort).to_have_attribute("data-sort", "interaction")


def test_life_os_scope_chip_narrows_the_search_and_widens_it(
    authed_page: Page, base_url: str
) -> None:
    """#1504: typed while narrowed to a skill, the search asks for that skill
    only; the chip's ✕ widens the same search to every skill, and the skill
    rows come back above it."""
    _mock_skills(authed_page)
    _mock_conversations(authed_page)
    seen: list = []
    _mock_search(authed_page, [dict(_FAKE_CONVERSATIONS["conversations"][0])], seen=seen)
    _open_conversations(authed_page, base_url)
    expect(_convo_rows(authed_page)).to_have_count(2)

    authed_page.locator("#lifeOsFilterInput").fill("ferry")
    wait_until(authed_page, lambda: len(seen) == 1, "the scoped search")
    assert "skill=journal-daily" in seen[0], seen
    expect(_convo_rows(authed_page)).to_have_count(1)
    # Within one skill, the rows need not repeat its name.
    expect(_convo_rows(authed_page).first.locator(".action-row-meta")).not_to_contain_text(
        "journal-daily")

    scope = authed_page.locator("#lifeOsConvoScope")
    scope.click()
    expect(scope).to_be_hidden()
    wait_until(authed_page, lambda: len(seen) == 2, "the widened search")
    assert "skill=" not in seen[1], seen
    expect(authed_page.locator("#lifeOsFilterInput")).to_be_focused()
    expect(authed_page.locator(".lifeos-list-card")).not_to_have_class(
        re.compile(r"\bis-convo-scoped\b"))
    expect(authed_page.locator("#lifeOsSkillsHead")).to_have_text("Skills (0)")
    expect(_convo_rows(authed_page).first.locator(".action-row-meta")).to_contain_text(
        "journal-daily")


def test_life_os_search_start_new_in_the_scoped_skill(
    authed_page: Page, base_url: str
) -> None:
    """#1588: narrowed to a skill, a search (hits or none) carries a Start new
    that launches a fresh conversation in that skill through the kebab's path
    — never a resume, whatever the Resume switch says, on the toolbar's model,
    and without the search text."""
    page = authed_page
    _mock_skills(page)
    _mock_conversations(page)
    seen: list = []
    _mock_search(page, [], seen=seen)
    launches = _mock_skill_launches(page)
    _open_conversations(page, base_url)
    start = page.locator("#lifeOsConvoStartNew")
    expect(start).to_be_visible()   # the skill's own list: round 3 shows it too

    life_launch_switch(page, "#lifeOsDetached")
    life_launch_switch(page, "#lifeOsResume")
    _pick_life_model(page, "codex:gpt-6-luna")
    page.locator("#lifeOsFilterInput").fill("no such thing")
    wait_until(page, lambda: len(seen) == 1, "the scoped search")
    expect(page.locator("#lifeOsConvoState")).to_contain_text("Nothing matched")
    expect(start).to_be_visible()
    expect(start).to_have_attribute("data-track", "life-search-start-new")
    expect(start).to_have_accessible_name("Start new conversation")
    expect(start).to_have_attribute("title", "Start new conversation")

    start.click()
    wait_until(page, lambda: len(launches) == 1, "the fresh launch")
    skill, payload = launches[0]
    assert skill == "journal-daily"
    assert payload == {"mode": "remote", "model": "codex:gpt-6-luna", "resume": False}, payload
    expect(page.locator("#lifeOsStartPicker")).to_be_hidden()
    assert "no such thing" not in _json.dumps(payload)

    # Clearing the field ends the search; the narrowed list (and the control)
    # stay, since the group is still showing.
    page.locator("#lifeOsFilterInput").fill("")
    expect(page.locator("#lifeOsConvoState")).not_to_contain_text("Nothing matched")
    expect(start).to_be_visible()


def test_life_os_browse_mode_start_new_starts_the_skill(
    authed_page: Page, base_url: str
) -> None:
    """#1588 round 3: the + is there whenever the Conversations group is, so a
    skill's own list (⋮ Conversations; nothing or one letter typed) starts that
    skill fresh, on the toolbar's model, without sending anything typed. With
    the group off (no skill narrowed, no search) there is no +."""
    page = authed_page
    _mock_skills(page)
    _mock_conversations(page)
    seen: list = []
    _mock_search(page, [], seen=seen)
    launches = _mock_skill_launches(page)
    _open_conversations(page, base_url)
    start = page.locator("#lifeOsConvoStartNew")
    expect(page.locator("#lifeOsFilterInput")).to_have_value("")
    expect(start).to_be_visible()
    expect(start).to_have_attribute("data-track", "life-search-start-new")
    expect(start).to_have_accessible_name("Start new conversation")
    expect(start).to_have_attribute("title", "Start new conversation")
    expect(start).not_to_have_attribute("aria-haspopup", re.compile(".*"))

    # One letter is still browse mode: no search fired, the + stays.
    page.locator("#lifeOsFilterInput").fill("f")
    flush_requests(page)
    assert seen == [], seen
    expect(start).to_be_visible()

    life_launch_switch(page, "#lifeOsDetached")
    life_launch_switch(page, "#lifeOsResume")
    _pick_life_model(page, "codex:gpt-6-luna")
    start.click()
    wait_until(page, lambda: len(launches) == 1, "the fresh launch")
    skill, payload = launches[0]
    assert skill == "journal-daily"
    assert payload == {"mode": "remote", "model": "codex:gpt-6-luna", "resume": False}, payload
    expect(page.locator("#lifeOsStartPicker")).to_be_hidden()

    # Widening to every skill with nothing typed steps the group, and the +, aside.
    page.locator("#lifeOsFilterInput").fill("")
    page.locator("#lifeOsConvoScope").click()
    expect(page.locator("#lifeOsConvoGroup")).to_be_hidden()
    expect(start).to_be_hidden()


def test_life_os_search_start_new_across_every_skill_opens_the_picker(
    authed_page: Page, base_url: str
) -> None:
    """#1588: spanning every skill, Start new opens the skill list instead and
    the owner taps one; the sheet closes and that skill starts fresh. It is
    there for a search with no hits, and ✕ leaves everything unlaunched."""
    page = authed_page
    _mock_skills(page)
    seen: list = []
    _mock_search(page, [], seen=seen)
    launches = _mock_skill_launches(page)
    _open_life(page, base_url)
    life_launch_switch(page, "#lifeOsDetached")
    start = page.locator("#lifeOsConvoStartNew")
    expect(start).to_be_hidden()

    page.locator("#lifeOsFilterInput").fill("zzz")
    wait_until(page, lambda: len(seen) == 1, "the search")
    expect(page.locator("#lifeOsConvoState")).to_contain_text("Nothing matched")
    expect(start).to_be_visible()
    expect(start).to_have_accessible_name("Start new conversation")
    expect(start).to_have_attribute("aria-haspopup", "dialog")

    start.click()
    picker = page.locator("#lifeOsStartPicker")
    expect(picker).to_be_visible()
    options = picker.locator(".lifeos-start-pick")
    expect(options).to_have_count(2)
    assert sorted(options.evaluate_all("els => els.map(e => e.dataset.id)")) == [
        "journal-daily", "sparring-work"]
    page.locator("#lifeOsStartPickerClose").click()
    expect(picker).to_be_hidden()
    flush_requests(page)
    assert launches == []

    start.click()
    picker.locator(".lifeos-start-pick[data-id='sparring-work']").click()
    expect(picker).to_be_hidden()
    wait_until(page, lambda: len(launches) == 1, "the picked skill's launch")
    skill, payload = launches[0]
    assert skill == "sparring-work"
    assert payload["resume"] is False and payload["mode"] == "remote", payload
    assert "zzz" not in _json.dumps(payload)


@pytest.mark.iphone
def test_life_os_search_start_new_is_a_plain_plus_and_the_sort_is_full_width(
    authed_page: Page, base_url: str
) -> None:
    """#1588 round 2: Start new is an unpainted vendored icon-button (a `+`
    glyph, no fill or border at rest, a 44px invisible target) at the trailing
    end of the Conversations head, and the sort control spans the content
    width like the search field, in both themes."""
    page = authed_page
    _mock_skills(page)
    _mock_search(page, [])
    _open_life(page, base_url)
    page.locator("#lifeOsFilterInput").fill("zzz")
    start = page.locator("#lifeOsConvoStartNew")
    expect(start).to_be_visible(timeout=5_000)
    expect(start).to_have_class(re.compile(r"(^|\s)icon-button(\s|$)"))
    expect(start).not_to_have_class(re.compile(r"button-tint"))
    expect(start.locator("svg use")).to_have_attribute("href", "#i-plus")
    expect(start).to_have_text("")
    for theme in ("light", "dark"):
        page.evaluate("theme => document.documentElement.dataset.theme = theme", theme)
        assert_min_target(start)
        assert_no_overlap([page.locator("#lifeOsConvoHead"), start])
        # Unpainted at rest: transparent fill, no border, no shadow.
        expect(start).to_have_css("background-color", "rgba(0, 0, 0, 0)")
        expect(start).to_have_css("border-top-width", "0px")
        expect(start).to_have_css("box-shadow", "none")
        head_w = stable_eval(page.locator("#lifeOsConvoHead"), "h => h.getBoundingClientRect().width")
        assert head_w > 80, head_w
        fit = stable_eval(
            page.locator("#lifeOsConvoGroup"),
            "g => { const r = g.getBoundingClientRect(),"
            " b = g.querySelector('#lifeOsConvoStartNew').getBoundingClientRect();"
            " return b.left >= r.left && b.right <= r.right; }")
        assert fit, "Start new spills out of the Conversations group"
        # The sort track runs the head's full width on a phone, three equal
        # segments; a wide card keeps the track at a phone's width (360px).
        for width in (390, 430, 1280):
            page.set_viewport_size({"width": width, "height": 844})
            geo = stable_eval(
                page.locator("#lifeOsConvoGroup"),
                "g => { const q = s => g.querySelector(s).getBoundingClientRect(),"
                " bar = q('.lifeos-group-bar'), sort = q('#lifeOsConvoSort'),"
                " segs = [...g.querySelectorAll('#lifeOsConvoSort .segmented-item')]"
                ".map(e => e.getBoundingClientRect().width);"
                " return {sortL: sort.left, sortR: sort.right, barL: bar.left,"
                " barR: bar.right, segs}; }")
            assert abs(geo["sortL"] - geo["barL"]) <= 1, (width, geo)
            if width <= 430:
                assert abs(geo["sortR"] - geo["barR"]) <= 1, (width, geo)
            else:
                assert geo["sortR"] - geo["sortL"] <= 361, (width, geo)
            assert max(geo["segs"]) - min(geo["segs"]) <= 1, (width, geo)
    page.set_viewport_size({"width": 390, "height": 844})
    start.click()
    picker = page.locator("#lifeOsStartPicker")
    expect(picker).to_be_visible()
    assert_min_target(page.locator("#lifeOsStartPickerClose"))
    assert_min_target(page.locator("#lifeOsStartPickerDone"))
    for i in range(2):
        assert_min_target(picker.locator(".lifeos-start-pick").nth(i))
    assert picker.evaluate("el => el.scrollWidth <= el.clientWidth")


@pytest.mark.iphone
def test_life_os_browse_mode_plus_keeps_its_geometry_in_both_themes(
    authed_page: Page, base_url: str
) -> None:
    """#1588 round 3: in a skill's own list (no search) the + is the same plain
    44px icon-button at the Conversations head's end, inside the group, at
    phone and desktop widths, light and dark."""
    page = authed_page
    _mock_skills(page)
    _mock_conversations(page)
    _open_conversations(page, base_url)
    start = page.locator("#lifeOsConvoStartNew")
    expect(start).to_be_visible()
    for theme in ("light", "dark"):
        page.evaluate("theme => document.documentElement.dataset.theme = theme", theme)
        for width in (390, 1280):
            page.set_viewport_size({"width": width, "height": 844})
            assert_min_target(start)
            assert_no_overlap([page.locator("#lifeOsConvoHead"), start])
            expect(start).to_have_css("background-color", "rgba(0, 0, 0, 0)")
            expect(start).to_have_css("border-top-width", "0px")
            fit = stable_eval(
                page.locator("#lifeOsConvoGroup"),
                "g => { const r = g.getBoundingClientRect(),"
                " b = g.querySelector('#lifeOsConvoStartNew').getBoundingClientRect();"
                " return b.left >= r.left && b.right <= r.right; }")
            assert fit, (theme, width, "the + spills out of the Conversations group")


@pytest.mark.parametrize("failure", ["unavailable", "forbidden"])
def test_life_os_search_failure_stays_in_its_group(
    authed_page: Page, base_url: str, failure: str
) -> None:
    """A missing search CLI or database degrades to a stated 'unavailable',
    and a 403 (not an enrolled device, or off the tailnet) says so in plain
    words — both inside the Conversations group (#1504), never an error
    toast and never a blank list, while the skills group keeps working."""
    _mock_skills(authed_page)
    if failure == "unavailable":
        authed_page.route(_SEARCH_URL, lambda route: route.fulfill(
            status=200, content_type="application/json", body=_json.dumps({
                "available": False,
                "reason": "no conversation index has been built yet",
                "results": [],
            })))
    else:
        authed_page.route(_SEARCH_URL, lambda route: route.fulfill(
            status=403, content_type="application/json",
            body=_json.dumps({"detail": "Tailscale only"})))
    _open_life(authed_page, base_url)
    authed_page.locator("#lifeOsFilterInput").fill("journal")

    state = authed_page.locator("#lifeOsConvoState")
    expect(state).to_be_visible(timeout=5_000)
    expect(state).to_have_attribute("data-state", "error")
    expect(state).to_contain_text(
        "Search unavailable" if failure == "unavailable" else "Tailscale-only")
    # Passive/background status belongs beside the surface it describes; a
    # toast is for user-initiated command results only.
    expect(authed_page.locator("#toast")).to_be_hidden()
    expect(authed_page.locator("#lifeOsList li.lifeos-item:visible")).to_have_count(1)
    expect(authed_page.locator("#lifeOsSkillsHead")).to_have_text("Skills (1)")
