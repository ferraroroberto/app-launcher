"""Life tab glance (#1439, step 7/7 of #1432; rows reworked in #1503).

The Life tab after the round-2 redesign, as amended by the owner's second
pass on #1439 and by #1503 (step 3/6 of #1500):

* Skills is the first card. There is no Telegram card: a skill a bot is
  linked to says "Telegram" in its meta and its avatar wears the shared green
  alive badge while that bot's session runs; its kebab starts or opens it.
* Each skill row leads with an avatar (its SKILL.md glyph, else a monogram)
  and says when it was last used. Pinned skills come first, then the most
  recently used, then A to Z; skills not used lately fold under one row.
  The kebab resumes the newest conversation or starts a new one, then pins.
* A row built without the newer ``actionRow`` fields stays byte-identical to
  what ``actionRow`` produced before they existed (the golden below was
  captured from ``main`` at 2e0227c).
* The weekly recap is one glance row under Skills, and rises above it while
  it is the exception; that exception reaches the header ("recap overdue"),
  per the header rule.
* The Skills list has a persisted filter; rename goes through a vendored
  dialog, never a native ``prompt()``.

Every fixture is fictional (Life OS holds personal content), and every launch
route is mocked: nothing here starts, stops or messages a real session.
"""

from __future__ import annotations

import json as _json
import re
from datetime import date, timedelta

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import (
    flush_requests,
    life_launch_switch,
    open_row_menu,
    open_settings_sheet,
    stable_eval,
    wait_until,
)

pytestmark = pytest.mark.smoke

_SKILLS = {
    "available": True,
    "life_os_dir": "E:/synthetic/life-os",
    "skills": [
        {"id": "garden-notes", "name": "garden-notes", "command": "garden-notes",
         "description": "A fictional skill.", "skill_md": "x"},
        {"id": "reading-list", "name": "reading-list", "command": "reading-list",
         "description": "A fictional skill.", "skill_md": "x"},
        {"id": "trip-planner", "name": "trip-planner", "command": "trip-planner",
         "description": "A fictional skill.", "skill_md": "x"},
    ],
}

_CHANNELS = {
    "profiles": [
        {"id": "allotment", "label": "Allotment", "skill": "garden-notes",
         "skill_found": True, "env_present": True, "running": False,
         "session_id": ""},
        {"id": "bookclub", "label": "Book club", "skill": "reading-list",
         "skill_found": True, "env_present": True, "running": True,
         "session_id": "s-bookclub"},
        {"id": "pantry", "label": "Pantry", "skill": "recipe-box",
         "skill_found": False, "env_present": True, "running": False,
         "session_id": ""},
    ],
    "problems": ["profile 'cellar': state_dir does not exist"],
    "setup": {
        "file_present": True, "life_os_found": True, "bun": True, "plugin": True,
        "skills": [{"id": s["id"], "name": s["name"]} for s in _SKILLS["skills"]],
    },
}


def _json_route(page: Page, pattern: str, body: dict) -> None:
    page.route(
        re.compile(pattern),
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=_json.dumps(body)
        ),
    )


def _recap(page: Page, **overrides) -> None:
    body = {
        "available": True, "ledger_exists": True, "age_days": 3.0,
        "staleness": "fresh", "proposal_pending": False, "proposal_name": None,
    }
    body.update(overrides)
    _json_route(page, r".*/api/life-os/recap-status$", body)


def _channel_launches(page: Page) -> list:
    """Mock the channel launch route; a remote session opens no terminal."""
    launches: list = []

    def _launch(route):
        launches.append({
            "url": route.request.url,
            "body": _json.loads(route.request.post_data or "{}"),
        })
        route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"session": {"session_id": "s-new", "kind": "remote"}}),
        )

    page.route(re.compile(r".*/api/life-os/channels/[^/]+/launch$"), _launch)
    return launches


def _ago(days: int) -> str:
    return (date.today() - timedelta(days=days)).isoformat()


@pytest.fixture
def life(authed_page: Page, base_url: str):
    """The Life tab over fictional skills, profiles and a fresh recap."""
    _json_route(authed_page, r".*/api/life-os/skills(\?.*)?$", _SKILLS)
    _json_route(authed_page, r".*/api/life-os/channels$", _CHANNELS)
    # The skills field searches conversations from two letters (#1504): a
    # stub, so a typed filter never reaches a real conversation index.
    _json_route(authed_page, r".*/api/life-os/conversations/search.*",
                {"available": True, "query": "", "skill": "", "results": []})
    _recap(authed_page)

    def _open() -> Page:
        authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
        authed_page.locator("#tabLifeOS").click()
        # The rows render twice: once from the skills, again once the
        # profiles land. Wait for the second.
        expect(authed_page.locator(
            "#lifeOsList li[data-id='reading-list'] .avatar-badge[data-badge='alive']"
        )).to_be_visible(timeout=5_000)
        return authed_page

    return _open


def _row(page: Page, skill_id: str):
    return page.locator(f"#lifeOsList li.lifeos-item[data-id='{skill_id}']")


def test_skills_lead_the_tab_and_no_telegram_card_is_left(life) -> None:
    page = life()
    cards = page.locator("#paneLifeOS > .card:not(.home-head)")
    expect(cards.first).to_have_class(re.compile(r"\blifeos-list-card\b"))
    expect(cards.nth(1)).to_have_id("lifeOsRecap")
    expect(page.locator("#lifeOsChannels")).to_have_count(0)
    expect(page.locator("#paneLifeOS .lifeos-channel-item")).to_have_count(0)
    # The broken profile and the problems line are Settings' alone.
    expect(page.locator("#paneLifeOS")).not_to_contain_text("Pantry")
    expect(page.locator("#paneLifeOS")).not_to_contain_text("cellar")
    open_settings_sheet(page, "channelsSheet")
    broken = page.locator("#channelProfileChecks .channel-check", has_text="Pantry")
    expect(broken.locator(".channel-check-chip")).to_have_text("Needs setup")
    expect(broken).to_contain_text("not found")
    expect(page.locator("#channelProfileProblems")).to_contain_text("cellar")


@pytest.mark.iphone
def test_a_linked_skill_says_telegram_and_its_avatar_wears_the_badge(life) -> None:
    """The bot is a plain fact in the meta, never a chip; the avatar's green
    badge only while that bot runs. Every row is a two-line row (rows.lg),
    its avatar on the leading edge and its kebab on the trailing one."""
    page = life()
    garden, reading, trip = (_row(page, i) for i in ("garden-notes", "reading-list", "trip-planner"))
    expect(garden.locator(".action-row-meta")).to_have_text("no conversations yet · Telegram")
    expect(reading.locator(".action-row-meta")).to_have_text("no conversations yet · Telegram")
    expect(trip.locator(".action-row-meta")).to_have_text("no conversations yet")
    expect(garden.locator(".avatar")).to_have_attribute("data-badge", "none")
    expect(reading.locator(".avatar-badge[data-badge='alive']")).to_have_count(1)
    expect(page.locator("#lifeOsList .chip")).to_have_count(0)
    # No star: the avatar is the row's one leading slot (Pin is in the kebab).
    expect(page.locator("#lifeOsList .star-btn")).to_have_count(0)

    geometry = stable_eval(page.locator("#lifeOsList"), """el => {
      if (!el.isConnected) return null;
      const box = (n) => n.getBoundingClientRect();
      const rows = Array.from(el.querySelectorAll(':scope > li')).map(box);
      const row = el.querySelector("li[data-id='garden-notes']");
      return {
        heights: rows.map((r) => Math.round(r.height)),
        row: box(row), avatar: box(row.querySelector('.avatar')),
        title: box(row.querySelector('.action-row-title')),
        kebab: box(row.querySelector('.action-row-kebab')),
      };
    }""")
    assert geometry["heights"] == [60, 60, 60], geometry["heights"]
    avatar, title, kebab = geometry["avatar"], geometry["title"], geometry["kebab"]
    assert round(avatar["width"]) == 36 and round(avatar["height"]) == 36, avatar
    assert avatar["right"] <= title["left"], (avatar, title)
    assert title["right"] <= kebab["left"], (title, kebab)
    assert kebab["width"] >= 44 and kebab["height"] >= 44, kebab


def test_the_kebab_starts_or_opens_the_linked_bot(life) -> None:
    page = life()
    launches = _channel_launches(page)

    # A stopped bot: "Start on Telegram", after the launch items and Pin.
    garden = _row(page, "garden-notes")
    garden.locator(".action-row-kebab").click()
    items = open_row_menu(page).locator(".row-menu-item")
    expect(items).to_have_text([
        "Resume last conversation", "Start new conversation", "Pin to top",
        "Start on Telegram", "Read", "Conversations",
    ])
    expect(items.nth(3)).to_have_attribute("data-item", "lifeos-channel-start-btn")
    items.nth(3).click()
    wait_until(page, lambda: len(launches) == 1, "the channel launch POST")
    assert launches[0]["url"].endswith("/api/life-os/channels/allotment/launch")
    assert launches[0]["body"]["mode"] == "pty"
    assert launches[0]["body"]["resume"] is False
    assert launches[0]["body"]["model"] == "claude:sonnet"

    # Detached on: refused, as before (a Telegram session is a terminal).
    life_launch_switch(page, "#lifeOsDetached")
    garden.locator(".action-row-kebab").click()
    open_row_menu(page).locator("[data-item=lifeos-channel-start-btn]").click()
    expect(page.locator("#toast")).to_contain_text("turn Detached off")
    flush_requests(page)
    assert len(launches) == 1, launches
    life_launch_switch(page, "#lifeOsDetached")

    # Resume on: the toolbar's Resume reaches the bot's launch too.
    life_launch_switch(page, "#lifeOsResume")
    garden.locator(".action-row-kebab").click()
    open_row_menu(page).locator("[data-item=lifeos-channel-start-btn]").click()
    wait_until(page, lambda: len(launches) == 2, "the resumed channel launch")
    assert launches[1]["body"]["resume"] is True

    # A running bot: "Open Telegram session", and no launch.
    reading = _row(page, "reading-list")
    reading.locator(".action-row-kebab").click()
    open_btn = open_row_menu(page).locator(".row-menu-item").nth(3)
    expect(open_btn).to_have_attribute("data-item", "lifeos-channel-open-btn")
    expect(open_btn).to_contain_text("Open Telegram session")
    open_btn.click()
    flush_requests(page)
    assert len(launches) == 2, launches


def test_the_skills_filter_narrows_and_persists(life) -> None:
    page = life()
    flt = page.locator("#lifeOsFilterInput")
    expect(flt).to_have_attribute("placeholder", "Skills and conversations")
    flt.fill("READ")
    expect(page.locator("#lifeOsList li.lifeos-item:not([hidden])")).to_have_count(1)
    expect(_row(page, "reading-list")).to_be_visible()

    # Survives a reload (the Apps filter's persistence, per-list key).
    page.reload(wait_until="domcontentloaded")
    page.locator("#tabLifeOS").click()
    expect(page.locator("#lifeOsFilterInput")).to_have_value("READ")
    expect(_row(page, "reading-list")).to_be_visible()
    expect(_row(page, "garden-notes")).to_be_hidden()

    page.locator("#lifeOsFilterInput").fill("no such skill")
    empty = page.locator("#lifeOsFilterEmpty")
    expect(empty).to_be_visible()
    empty.locator(".empty-state-action").click()
    expect(page.locator("#lifeOsList li.lifeos-item:not([hidden])")).to_have_count(3)


@pytest.mark.parametrize(
    "recap, header, tone, meta, chips",
    [
        ({"staleness": "overdue", "age_days": 16.0, "proposal_pending": True},
         "recap overdue", "attention", "last run 16 days ago",
         [("overdue", "attention"), ("draft ready", "accent")]),
        ({"staleness": "due", "age_days": 1.0}, "recap due", "attention",
         "last run 1 day ago", [("due", "attention")]),
        ({"staleness": "fresh", "age_days": 0.2}, "3 skills", None,
         "last run today", []),
        ({"staleness": "never", "age_days": None}, "3 skills", None, "never run", []),
        ({"available": False, "staleness": "overdue"}, "3 skills", None, None, []),
    ],
)
def test_the_recap_is_one_glance_row_and_its_exception_heads_the_tab(
    authed_page: Page, life, recap, header, tone, meta, chips
) -> None:
    _recap(authed_page, **recap)
    page = life()
    status = page.locator("#lifeHeadStatus")
    expect(status).to_have_text(header)
    if tone:
        expect(status.locator(".head-exception")).to_have_attribute("data-tone", tone)
    card = page.locator("#lifeOsRecap")
    if meta is None:
        expect(card).to_be_hidden()
        return
    # The recap rises above Skills only as the exception (#1503).
    cards = page.locator("#paneLifeOS > .card:not(.home-head)")
    if chips:
        expect(cards.first).to_have_id("lifeOsRecap")
        expect(cards.nth(1)).to_have_class(re.compile(r"\blifeos-list-card\b"))
    else:
        expect(cards.first).to_have_class(re.compile(r"\blifeos-list-card\b"))
        expect(cards.nth(1)).to_have_id("lifeOsRecap")
    row = card.locator("li.lifeos-recap-item")
    expect(row.locator(".action-row-title")).to_have_text("Weekly recap")
    expect(row.locator(".avatar")).to_have_count(1)
    # #167's coloured-dot badge is gone since #1439: only the chips remain.
    expect(page.locator(".lifeos-recap-badge")).to_have_count(0)
    # The meta's text is its own span only when chips follow it.
    text = row.locator(".action-row-meta-text" if chips else ".action-row-meta")
    expect(text).to_have_text(meta)
    found = row.locator(".chip")
    expect(found).to_have_count(len(chips))
    for i, (text, chip_tone) in enumerate(chips):
        expect(found.nth(i)).to_have_text(text)
        expect(found.nth(i)).to_have_attribute("data-tone", chip_tone)
    verb = row.locator("#lifeOsRecapLaunch")
    expect(verb).to_have_class(re.compile(r"\baction-row-verb\b"))
    # The plain verb (#1501), still named; its look is pinned on the
    # Backlog's ▶ in test_board_tab.py.
    expect(verb).to_have_class(re.compile(r"\brow-verb-plain\b"))
    expect(verb).to_have_attribute("aria-label", "Launch weekly recap review")
    expect(row.locator(".action-row-kebab")).to_have_count(0)


def test_the_recap_verb_launches_with_the_toolbar(life) -> None:
    page = life()
    posted: list = []

    def _launch(route):
        posted.append(_json.loads(route.request.post_data or "{}"))
        route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"session": {"session_id": "r", "kind": "remote"}}),
        )

    page.route(re.compile(r".*/api/life-os/recap/launch$"), _launch)
    life_launch_switch(page, "#lifeOsDetached")
    page.locator("#lifeOsRecapLaunch").click()
    wait_until(page, lambda: len(posted) == 1, "the recap launch POST")
    assert posted[0] == {"mode": "remote", "model": "claude:sonnet"}, posted


def test_rename_goes_through_the_dialog(life) -> None:
    """The conversation log rename is the vendored dialog (#1439): prefilled
    with the name part, ✕ cancels without a request, Rename posts the slug.
    A native prompt() would surface as a page dialog event; none fires."""
    page = life()
    native: list = []
    page.on("dialog", lambda d: (native.append(d.message), d.dismiss()))
    log = {
        "path": ".claude/skills/garden-notes/conversations/2026-03-02-0815-seed-order.md",
        "name": "2026-03-02-0815-seed-order.md", "category": "conversations",
    }
    _json_route(page, r".*/api/life-os/skills/garden-notes/files$",
                {"skill": {"id": "garden-notes", "name": "garden-notes"}, "files": [log]})
    _json_route(page, r".*/api/life-os/file\?.*$",
                {"path": log["path"], "name": log["name"], "content": "fictional", "truncated": False})
    renames: list = []

    def _rename(route):
        renames.append(route.request.post_data_json)
        route.fulfill(status=200, content_type="application/json",
                      body=_json.dumps({"name": "2026-03-02-0815-bulb-order.md"}))

    page.route(re.compile(r".*/api/life-os/file/rename$"), _rename)

    garden = _row(page, "garden-notes")
    garden.locator(".action-row-kebab").click()
    open_row_menu(page).locator("[data-item=lifeos-browse-btn]").click()
    page.locator(".lifeos-file-btn").first.click()
    dialog = page.locator("#lifeOsRenameDialog")

    page.locator("#lifeOsDocRename").click()
    expect(dialog).to_be_visible()
    expect(page.locator("#lifeOsRenameInput")).to_have_value("seed-order")
    page.locator("#lifeOsRenameClose").click()
    expect(dialog).to_be_hidden()
    flush_requests(page)
    assert renames == []

    page.locator("#lifeOsDocRename").click()
    page.locator("#lifeOsRenameInput").fill("Bulb Order")
    page.locator("#lifeOsRenameSave").click()
    wait_until(page, lambda: len(renames) == 1, "the rename POST")
    assert renames[0] == {"path": log["path"], "slug": "bulb-order"}
    expect(dialog).to_be_hidden()
    expect(page.locator("#toast")).to_contain_text("Renamed")
    assert native == [], native


def test_every_life_sheet_backs_out_the_same_way(life) -> None:
    """#1504: one back style everywhere. The conversation sheet, the reader,
    Browse and its document are all the vendored modal, each closed by its
    header ✕ (and a Done primary on the reading sheets), never a ‹ back."""
    page = life()
    closes = page.locator(
        "#lifeOsConvoSheetClose, #lifeOsViewerClose, #lifeOsBrowserClose, #lifeOsDocClose")
    expect(closes).to_have_count(4)
    for i in range(4):
        close = closes.nth(i)
        expect(close).to_have_class(re.compile(r"\bdetail-close\b"))
        expect(close.locator("use")).to_have_attribute("href", "#i-x")
        expect(close.locator("xpath=ancestor::dialog")).to_have_class(
            re.compile(r"\bdetail-dialog\b"))
    expect(page.locator(".terminal-overlay[id^='lifeOs']")).to_have_count(0)
    expect(page.locator("[id^='lifeOs'] use[href='#i-chevron-left']")).to_have_count(0)
    for done in ("#lifeOsViewerDone", "#lifeOsBrowserDone", "#lifeOsDocDone"):
        expect(page.locator(done)).to_have_class(re.compile(r"\bbutton-primary\b"))


# Two Code-tab project rows as actionRow built them before the optional
# fields existed, captured on main (2e0227c) by running _ROWS there. A row
# without them must stay byte-identical. (`titleIcon`, #1439, is gone again
# since #1503: its one caller, the Life row, now shows the bot in its meta.)
_ROWS = """async () => {
  const { actionRow } = await import('/static/action-rows.js');
  const { chip } = await import('/static/glance.js');
  const rows = [
    actionRow({
      id: 'demo-project', className: 'coding-item', title: 'demo-project',
      meta: 'main · 3 uncommitted', chips: [chip('uncommitted', 'attention')],
      label: 'Launch Claude in demo-project', onMain: function () {},
      favorite: { on: true, onToggle: function () {} },
      kebabClass: 'project-menu-anchor', kebabLabel: 'Project actions',
    }),
    actionRow({
      id: 'plain', className: 'coding-item', title: 'plain', label: 'Launch',
      disabled: true, hint: 'Claude is not installed', onMain: function () {},
      favorite: { on: false, onToggle: function () {} },
      kebabClass: 'project-menu-anchor', kebabLabel: 'Project actions',
    }),
  ];
  return rows.map(function (r) { return r.li.outerHTML; });
}"""

_GOLDEN = [
    '<li class="action-row coding-item" data-id="demo-project"><button type="button" class="icon-button action-row-fav star-btn" aria-pressed="true" title="Unstar (remove from favorites)" aria-label="Unstar (remove from favorites)"><svg class="icon action-row-fav-off" aria-hidden="true"><use href="#i-star"></use></svg><svg class="icon action-row-fav-on" aria-hidden="true"><use href="#i-star-fill"></use></svg></button><button type="button" class="action-row-main" aria-label="Launch Claude in demo-project"><span class="action-row-title" title="demo-project">demo-project</span><span class="action-row-meta has-chips"><span class="action-row-meta-text">main \u00b7 3 uncommitted</span><span class="chip" data-tone="attention">uncommitted</span></span></button><button type="button" class="icon-button action-row-kebab project-menu-anchor" title="Project actions" aria-label="Project actions"><svg class="icon" aria-hidden="true"><use href="#i-ellipsis-vertical"></use></svg></button></li>',
    '<li class="action-row coding-item" data-id="plain"><button type="button" class="icon-button action-row-fav star-btn" aria-pressed="false" title="Star (add to favorites)" aria-label="Star (add to favorites)"><svg class="icon action-row-fav-off" aria-hidden="true"><use href="#i-star"></use></svg><svg class="icon action-row-fav-on" aria-hidden="true"><use href="#i-star-fill"></use></svg></button><button type="button" class="action-row-main" disabled="" aria-label="Claude is not installed"><span class="action-row-title" title="plain">plain</span></button><button type="button" class="icon-button action-row-kebab project-menu-anchor" title="Project actions" aria-label="Project actions"><svg class="icon" aria-hidden="true"><use href="#i-ellipsis-vertical"></use></svg></button></li>',
]


def test_a_row_without_the_optional_fields_is_byte_identical(
    authed_page: Page, base_url: str
) -> None:
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    assert authed_page.evaluate(_ROWS) == _GOLDEN


# ---------------------------------------------- rows that sort and fold (#1503)
def _skill(skill_id: str, **fields) -> dict:
    row = {"id": skill_id, "name": skill_id, "command": skill_id,
           "description": "A fictional skill.", "skill_md": "x",
           "icon": "", "last_used": "", "has_conversations": False,
           "is_favorite": False}
    row.update(fields)
    return row


_USED = {
    "available": True,
    "life_os_dir": "E:/synthetic/life-os",
    "skills": [
        _skill("bird-count", last_used=_ago(90), has_conversations=True),
        _skill("garden-notes", icon="sprout", last_used=_ago(0), has_conversations=True),
        _skill("pottery", icon="no-such-glyph", last_used=_ago(3), has_conversations=True),
        _skill("reading-list", last_used=_ago(40), has_conversations=True, is_favorite=True),
        _skill("trip-planner", last_used=_ago(1), has_conversations=True),
        _skill("yoga-log"),
    ],
}


def _visible_order(page: Page) -> list:
    return page.evaluate("""() => Array.from(
      document.querySelectorAll('#lifeOsList > li.lifeos-item'))
      .filter((li) => li.offsetParent !== null).map((li) => li.dataset.id)""")


def test_pinned_then_recent_then_the_rest_fold(authed_page: Page, life) -> None:
    _json_route(authed_page, r".*/api/life-os/skills(\?.*)?$", _USED)
    page = life()
    # Pinned (even if old), then newest used, then the fold.
    expect(page.locator("#lifeOsList li.lifeos-item").first).to_have_attribute(
        "data-id", "reading-list")
    assert _visible_order(page) == ["reading-list", "garden-notes", "trip-planner", "pottery"]
    fold = page.locator("#lifeOsList .lifeos-fold-btn")
    expect(fold).to_have_text("2 more skills")
    expect(fold).to_have_attribute("aria-expanded", "false")
    expect(_row(page, "bird-count")).to_be_hidden()
    expect(_row(page, "reading-list")).to_have_attribute("data-pinned", "true")
    expect(_row(page, "reading-list").locator(".action-row-meta")).to_have_text("used last month · Telegram")

    fold.click()
    expect(fold).to_have_attribute("aria-expanded", "true")
    expect(fold).to_have_text("Fewer skills")
    assert _visible_order(page) == [
        "reading-list", "garden-notes", "trip-planner", "pottery", "bird-count", "yoga-log"]
    fold.click()
    expect(_row(page, "bird-count")).to_be_hidden()

    # The filter searches every skill: a match inside the fold shows, and the
    # fold row steps aside while a filter is typed.
    page.locator("#lifeOsFilterInput").fill("bird")
    expect(_row(page, "bird-count")).to_be_visible()
    expect(fold).to_be_hidden()
    page.locator("#lifeOsFilterInput").fill("")
    expect(_row(page, "bird-count")).to_be_hidden()
    expect(fold).to_be_visible()


def test_with_nothing_in_use_every_skill_shows(life) -> None:
    page = life()
    expect(page.locator("#lifeOsList .lifeos-fold")).to_have_count(0)
    assert _visible_order(page) == ["garden-notes", "reading-list", "trip-planner"]


@pytest.mark.parametrize("skill_id, meta", [
    ("garden-notes", "used today · Telegram"), ("trip-planner", "used yesterday"),
    ("pottery", "used 3 days ago"), ("bird-count", "used 3 months ago"),
    ("yoga-log", "no conversations yet"),
])
def test_the_meta_says_when_it_was_last_used(authed_page: Page, life, skill_id, meta) -> None:
    _json_route(authed_page, r".*/api/life-os/skills(\?.*)?$", _USED)
    page = life()
    expect(_row(page, skill_id).locator(".action-row-meta")).to_have_text(meta)


def test_the_avatar_is_the_skill_glyph_or_a_monogram(authed_page: Page, life) -> None:
    _json_route(authed_page, r".*/api/life-os/skills(\?.*)?$", _USED)
    page = life()
    glyph = _row(page, "garden-notes").locator(".avatar use")
    expect(glyph).to_have_attribute("href", "#i-sprout")
    # A glyph the page's sprite lacks falls back to the monogram, as does none.
    expect(_row(page, "pottery").locator(".avatar .lifeos-monogram")).to_have_text("P")
    expect(_row(page, "trip-planner").locator(".avatar .lifeos-monogram")).to_have_text("T")


def _resume_latest_route(page: Page, reply: dict) -> list:
    posted: list = []

    def _handle(route):
        posted.append({"url": route.request.url,
                       "body": _json.loads(route.request.post_data or "{}")})
        route.fulfill(status=200, content_type="application/json", body=_json.dumps(reply))

    page.route(re.compile(r".*/api/life-os/skills/[^/]+/conversations/resume-latest$"), _handle)
    return posted


def test_the_kebab_resumes_the_last_conversation_on_its_own_model(
    authed_page: Page, life
) -> None:
    _json_route(authed_page, r".*/api/life-os/skills(\?.*)?$", _USED)
    page = life()
    posted = _resume_latest_route(page, {
        "conversation_action": "resume", "model": "claude-opus-5-5", "model_kept": True,
        "session": {"session_id": "s-r", "kind": "remote"}})
    # Detached so nothing opens a terminal; the combo shows Sonnet.
    life_launch_switch(page, "#lifeOsDetached")
    garden = _row(page, "garden-notes")
    garden.locator(".action-row-kebab").click()
    open_row_menu(page).locator("[data-item=lifeos-resume-last-btn]").click()
    wait_until(page, lambda: len(posted) == 1, "the resume-latest POST")
    assert posted[0]["url"].endswith("/api/life-os/skills/garden-notes/conversations/resume-latest")
    # No model: the conversation keeps its own.
    assert posted[0]["body"] == {"mode": "remote"}, posted
    expect(page.locator("#toast")).to_contain_text("Resumed garden-notes (Opus) (detached)")


def test_resume_last_is_disabled_with_no_conversation_yet(authed_page: Page, life) -> None:
    _json_route(authed_page, r".*/api/life-os/skills(\?.*)?$", _USED)
    page = life()
    page.locator("#lifeOsList .lifeos-fold-btn").click()
    yoga = _row(page, "yoga-log")
    yoga.locator(".action-row-kebab").click()
    item = open_row_menu(page).locator("[data-item=lifeos-resume-last-btn]")
    expect(item).to_be_disabled()
    expect(open_row_menu(page).locator(".row-menu-head")).to_contain_text("No conversation yet")


def test_start_new_ignores_the_resume_switch(authed_page: Page, life) -> None:
    _json_route(authed_page, r".*/api/life-os/skills(\?.*)?$", _USED)
    page = life()
    posted: list = []

    def _launch(route):
        posted.append(_json.loads(route.request.post_data or "{}"))
        route.fulfill(status=200, content_type="application/json",
                      body=_json.dumps({"session": {"session_id": "s-n", "kind": "remote"}}))

    page.route(re.compile(r".*/api/life-os/skills/garden-notes/launch$"), _launch)
    life_launch_switch(page, "#lifeOsDetached")
    life_launch_switch(page, "#lifeOsResume")
    garden = _row(page, "garden-notes")
    garden.locator(".action-row-kebab").click()
    open_row_menu(page).locator("[data-item=lifeos-start-new-btn]").click()
    wait_until(page, lambda: len(posted) == 1, "the launch POST")
    assert posted[0] == {"mode": "remote", "model": "claude:sonnet", "resume": False}, posted


def test_pin_to_top_posts_the_pin_and_unpin_is_offered_back(authed_page: Page, life) -> None:
    _json_route(authed_page, r".*/api/life-os/skills(\?.*)?$", _USED)
    page = life()
    posted: list = []

    def _pin(route):
        posted.append(route.request.post_data_json)
        route.fulfill(status=200, content_type="application/json",
                      body=_json.dumps({"ok": True, "life_os_favorites": []}))

    page.route(re.compile(r".*/api/life-os/favorites$"), _pin)
    garden = _row(page, "garden-notes")
    garden.locator(".action-row-kebab").click()
    expect(open_row_menu(page).locator("[data-item=lifeos-pin-btn]")).to_have_text("Pin to top")
    open_row_menu(page).locator("[data-item=lifeos-pin-btn]").click()
    wait_until(page, lambda: len(posted) == 1, "the pin POST")
    assert posted == [{"id": "garden-notes", "favorite": True}], posted
    reading = _row(page, "reading-list")
    reading.locator(".action-row-kebab").click()
    expect(open_row_menu(page).locator("[data-item=lifeos-pin-btn]")).to_have_text("Unpin")


# --------------------------------------------- swipe actions (#1503, step D)
# Playwright's touch emulation only taps, so these drive synthetic pointer
# events through the real handlers (swipe-row.js); the thresholds themselves
# are pinned under plain Node (tests/js/swipe_row.test.mjs). Moves are spaced
# in time so a partial swipe is slow, not a flick.
_SWIPE = """async ({ id, dx, dy, startX, type, steps, pause }) => {
  const li = document.querySelector(`#lifeOsList li[data-id="${id}"]`);
  const surface = li.querySelector('.swipe-surface');
  const r = surface.getBoundingClientRect();
  const x0 = startX == null ? r.left + r.width / 2 : startX;
  const y0 = r.top + r.height / 2;
  const fire = (kind, x, y) => surface.dispatchEvent(new PointerEvent(kind, {
    pointerId: 7, pointerType: type, isPrimary: true,
    clientX: x, clientY: y, bubbles: true, cancelable: true,
  }));
  fire('pointerdown', x0, y0);
  for (let i = 1; i <= steps; i++) {
    await new Promise((done) => setTimeout(done, pause));
    fire('pointermove', x0 + dx * i / steps, y0 + dy * i / steps);
  }
  const during = { side: li.dataset.swipe || '', armed: li.classList.contains('is-armed') };
  fire('pointerup', x0 + dx, y0 + dy);
  return { during, after: li.dataset.swipe || '', width: r.width };
}"""


def _phone(page: Page) -> None:
    """A phone-width viewport: the swipe distances below are a 390px row's."""
    page.set_viewport_size({"width": 390, "height": 844})


def _swipe(page: Page, skill_id: str, dx: float, *, dy: float = 0, start_x=None,
           kind: str = "touch", steps: int = 10, pause: int = 40) -> dict:
    return page.evaluate(_SWIPE, {"id": skill_id, "dx": dx, "dy": dy, "startX": start_x,
                                  "type": kind, "steps": steps, "pause": pause})


def _launch_route(page: Page) -> list:
    posted: list = []

    def _launch(route):
        posted.append(_json.loads(route.request.post_data or "{}"))
        route.fulfill(status=200, content_type="application/json",
                      body=_json.dumps({"session": {"session_id": "s-n", "kind": "remote"}}))

    page.route(re.compile(r".*/api/life-os/skills/[^/]+/launch$"), _launch)
    return posted


@pytest.mark.iphone
def test_swipe_right_resumes_and_a_partial_swipe_left_rests_on_new(
    authed_page: Page, life
) -> None:
    _json_route(authed_page, r".*/api/life-os/skills(\\?.*)?$", _USED)
    _phone(authed_page)
    page = life()
    resumed = _resume_latest_route(page, {
        "conversation_action": "resume", "model": "claude-sonnet-5-5", "model_kept": True,
        "session": {"session_id": "s-r", "kind": "remote"}})
    launched = _launch_route(page)
    # Detached, so nothing opens a terminal; Resume on, which swipes ignore.
    life_launch_switch(page, "#lifeOsDetached")
    life_launch_switch(page, "#lifeOsResume")

    row = _row(page, "garden-notes")
    expect(row).to_have_class(re.compile(r"\bhas-swipe\b"))
    # The panes are behind the surface, hidden, and out of reach of AT.
    expect(row.locator(".swipe-pane--leading")).to_have_attribute("aria-hidden", "true")
    expect(row.locator(".swipe-pane-btn").first).to_have_attribute("tabindex", "-1")

    # A full swipe right arms before release, then resumes the newest
    # conversation directly: no model, no picker.
    full = _swipe(page, "garden-notes", 330, steps=10, pause=16)
    assert full["during"] == {"side": "leading", "armed": True}, full
    assert full["after"] == "", full
    wait_until(page, lambda: len(resumed) == 1, "the resume-latest POST")
    assert resumed[0]["url"].endswith("/garden-notes/conversations/resume-latest")
    assert resumed[0]["body"] == {"mode": "remote"}, resumed
    expect(page.locator("#toast")).to_contain_text("Resumed garden-notes (Sonnet)")

    # A slow partial swipe left rests open on New; nothing launches yet.
    partial = _swipe(page, "garden-notes", -100)
    assert partial["during"] == {"side": "trailing", "armed": False}, partial
    assert partial["after"] == "trailing", partial
    pane = row.locator(".swipe-pane--trailing")
    expect(pane).to_be_visible()
    expect(pane).to_contain_text("New")
    flush_requests(page)
    assert launched == [], launched
    # A tap on the pane commits: a new conversation, the Resume switch ignored.
    pane.locator(".swipe-pane-btn").click()
    wait_until(page, lambda: len(launched) == 1, "the launch POST")
    assert launched[0] == {"mode": "remote", "model": "claude:sonnet", "resume": False}, launched
    expect(row).not_to_have_attribute("data-swipe", re.compile(".*"))
    assert len(resumed) == 1


@pytest.mark.iphone
def test_a_swipe_never_starts_from_a_scroll_a_mouse_or_the_edge(
    authed_page: Page, life
) -> None:
    _json_route(authed_page, r".*/api/life-os/skills(\\?.*)?$", _USED)
    _phone(authed_page)
    page = life()
    resumed = _resume_latest_route(page, {"conversation_action": "resume", "session": None})
    launched = _launch_route(page)
    for label, kwargs in (
        ("a vertical scroll", {"dx": 30, "dy": 200}),
        ("a mouse", {"dx": 330, "kind": "mouse", "pause": 16}),
        ("the back-gesture edge", {"dx": 330, "start_x": 10, "pause": 16}),
    ):
        out = _swipe(page, "garden-notes", **kwargs)
        assert out["during"]["side"] == "" and out["after"] == "", (label, out)
    # A skill with no conversation yet: Resume shows why and never commits.
    page.locator("#lifeOsList .lifeos-fold-btn").click()
    blocked = _swipe(page, "yoga-log", 330, pause=16)
    assert blocked["during"] == {"side": "leading", "armed": False}, blocked
    assert blocked["after"] == "leading", blocked
    expect(_row(page, "yoga-log").locator(".swipe-pane--leading")).to_contain_text(
        "No conversation yet")
    flush_requests(page)
    assert resumed == [] and launched == [], (resumed, launched)


@pytest.mark.iphone
def test_skill_rows_run_full_bleed_on_the_phone_only(authed_page: Page, life) -> None:
    """The experiment the owner asked for: on the phone the Skills card runs
    edge to edge, so a row and its panes span the screen, while its header,
    toolbar and the rows' text keep the inset they had. Wide, it stays a
    card."""
    page = life()
    measure = """el => {
      if (!el.isConnected) return null;
      const box = (n) => n.getBoundingClientRect();
      const card = el.closest('.lifeos-list-card');
      const row = el.querySelector("li[data-id='garden-notes']");
      const app = el.closest('.app');
      return {
        // The content edge, without the desktop scrollbar a narrow window keeps.
        edge: box(app).left + app.clientWidth,
        card: box(card), row: box(row),
        radius: getComputedStyle(card).borderTopLeftRadius,
        head: box(card.querySelector('.collapse-main')),
        filter: box(card.querySelector('.action-row-filter')),
        avatar: box(row.querySelector('.avatar')),
      };
    }"""
    page.set_viewport_size({"width": 390, "height": 844})
    phone = stable_eval(page.locator("#lifeOsList"), measure)
    assert round(phone["row"]["left"]) == 0, phone
    assert abs(phone["row"]["right"] - phone["edge"]) <= 1, phone
    assert phone["radius"] == "0px", phone
    # The text keeps its inset (the gutter plus the card's 14px): header,
    # filter and avatar line up, off the edge.
    assert phone["filter"]["left"] >= 24, phone
    for part in ("avatar", "head"):
        assert abs(phone[part]["left"] - phone["filter"]["left"]) <= 2, (part, phone)
    page.set_viewport_size({"width": 1024, "height": 800})
    wide = stable_eval(page.locator("#lifeOsList"), measure)
    assert wide["card"]["left"] > 0 and wide["radius"] != "0px", wide
