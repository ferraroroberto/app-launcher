"""Life tab glance (#1439, step 7/7 of #1432).

The Life tab after the round-2 redesign, as amended by the owner's second
pass on #1439:

* Skills is the first card. There is no Telegram card: a skill a bot is
  linked to wears a muted send glyph after its name (one optional
  ``actionRow`` field, ``titleIcon``), carrying the shared green alive dot
  while that bot's session runs, and its kebab leads with the bot.
* The skill rows are Code's project rows exactly, so the new field must leave
  a row built without it byte-identical to what ``actionRow`` produced before
  it existed (the golden below was captured from ``main`` at 2e0227c).
* The weekly recap is one glance row under Skills; its exception reaches the
  header ("recap overdue"), per the header rule.
* The Skills list has a persisted filter; rename goes through a vendored
  dialog, never a native ``prompt()``.

Every fixture is fictional (Life OS holds personal content), and every launch
route is mocked: nothing here starts, stops or messages a real session.
"""

from __future__ import annotations

import json as _json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import flush_requests, open_settings_sheet, stable_eval, wait_until

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


@pytest.fixture
def life(authed_page: Page, base_url: str):
    """The Life tab over fictional skills, profiles and a fresh recap."""
    _json_route(authed_page, r".*/api/life-os/skills(\?.*)?$", _SKILLS)
    _json_route(authed_page, r".*/api/life-os/channels$", _CHANNELS)
    _recap(authed_page)

    def _open() -> Page:
        authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
        authed_page.locator("#tabLifeOS").click()
        # The rows render twice: once from the skills, again once the
        # profiles land. Wait for the second.
        expect(
            authed_page.locator("#lifeOsList li[data-id='reading-list'] .action-row-title-icon")
        ).to_be_visible(timeout=5_000)
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
def test_a_linked_skill_wears_the_send_glyph_on_an_unchanged_row(life) -> None:
    """The glyph is a muted fact after the name, never a chip, green-dotted
    only while the bot runs, and the row keeps its one-line 52px height."""
    page = life()
    stopped = _row(page, "garden-notes").locator(".action-row-title-icon")
    running = _row(page, "reading-list").locator(".action-row-title-icon")
    expect(stopped).to_have_attribute("data-alive", "false")
    expect(stopped.locator(".avatar-badge")).to_have_count(0)
    expect(running).to_have_attribute("data-alive", "true")
    expect(running.locator(".avatar-badge[data-badge='alive']")).to_have_count(1)
    expect(_row(page, "trip-planner").locator(".action-row-title-icon")).to_have_count(0)
    expect(page.locator("#lifeOsList .chip")).to_have_count(0)
    expect(page.locator("#lifeOsList .action-row-meta")).to_have_count(0)

    muted = page.evaluate(
        "() => { const p = document.createElement('span');"
        "p.style.color = 'var(--muted)'; document.body.appendChild(p);"
        "const c = getComputedStyle(p).color; p.remove(); return c; }"
    )
    expect(stopped).to_have_css("color", muted)

    geometry = stable_eval(page.locator("#lifeOsList"), """el => {
      if (!el.isConnected) return null;
      const box = (n) => n.getBoundingClientRect();
      const rows = Array.from(el.querySelectorAll(':scope > li')).map(box);
      const row = el.querySelector("li[data-id='garden-notes']");
      return {
        heights: rows.map((r) => Math.round(r.height)),
        title: box(row.querySelector('.action-row-title')),
        glyph: box(row.querySelector('.action-row-title-icon')),
        kebab: box(row.querySelector('.action-row-kebab')),
      };
    }""")
    assert geometry["heights"] == [52, 52, 52], geometry["heights"]
    title, glyph, kebab = geometry["title"], geometry["glyph"], geometry["kebab"]
    assert glyph["left"] >= title["right"] - 1, (title, glyph)
    assert glyph["right"] <= kebab["left"], (glyph, kebab)
    assert glyph["top"] < title["bottom"] and title["top"] < glyph["bottom"], (title, glyph)


def test_the_kebab_starts_or_opens_the_linked_bot(life) -> None:
    page = life()
    launches = _channel_launches(page)

    # A stopped bot: "Start on Telegram", first in the menu.
    garden = _row(page, "garden-notes")
    garden.locator(".action-row-kebab").click()
    items = garden.locator(".row-menu-btn")
    expect(items.first).to_have_class(re.compile(r"\blifeos-channel-start-btn\b"))
    expect(items.first).to_contain_text("Start on Telegram")
    expect(items).to_have_count(3)   # the bot, Read, Conversations
    items.first.click()
    wait_until(page, lambda: len(launches) == 1, "the channel launch POST")
    assert launches[0]["url"].endswith("/api/life-os/channels/allotment/launch")
    assert launches[0]["body"]["mode"] == "pty"
    assert launches[0]["body"]["resume"] is False
    assert launches[0]["body"]["model"] == "claude:sonnet"

    # Detached on: refused, as before (a Telegram session is a terminal).
    page.locator("#lifeOsDetached").click()
    garden.locator(".action-row-kebab").click()
    garden.locator(".lifeos-channel-start-btn").click()
    expect(page.locator("#toast")).to_contain_text("turn Detached off")
    flush_requests(page)
    assert len(launches) == 1, launches
    page.locator("#lifeOsDetached").click()

    # Resume on: the toolbar's Resume reaches the bot's launch too.
    page.locator("#lifeOsResume").click()
    garden.locator(".action-row-kebab").click()
    garden.locator(".lifeos-channel-start-btn").click()
    wait_until(page, lambda: len(launches) == 2, "the resumed channel launch")
    assert launches[1]["body"]["resume"] is True

    # A running bot: "Open Telegram session", and no launch.
    reading = _row(page, "reading-list")
    reading.locator(".action-row-kebab").click()
    open_btn = reading.locator(".row-menu-btn").first
    expect(open_btn).to_have_class(re.compile(r"\blifeos-channel-open-btn\b"))
    expect(open_btn).to_contain_text("Open Telegram session")
    open_btn.click()
    flush_requests(page)
    assert len(launches) == 2, launches


def test_the_skills_filter_narrows_and_persists(life) -> None:
    page = life()
    flt = page.locator("#lifeOsFilterInput")
    expect(flt).to_have_attribute("placeholder", "Filter skills")
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
    row = card.locator("li.lifeos-recap-item")
    expect(row.locator(".avatar")).to_have_count(1)
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
    page.locator("#lifeOsDetached").click()
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
    garden.locator(".lifeos-browse-btn").click()
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


def test_every_life_overlay_backs_out_the_same_way(life) -> None:
    page = life()
    backs = page.locator("#lifeOsBrowserBack, #lifeOsConvosBack, #lifeOsViewerBack")
    expect(backs).to_have_count(3)
    for i in range(3):
        back = backs.nth(i)
        expect(back).to_have_class(re.compile(r"\bicon-button\b"))
        expect(back).to_have_text("")
        expect(back.locator("use")).to_have_attribute("href", "#i-chevron-left")
    sort = page.locator("#lifeOsConvosSort")
    expect(sort).to_have_class(re.compile(r"\bicon-button\b"))


# Two Code-tab project rows as actionRow built them before `titleIcon`
# existed, captured on main (2e0227c) by running _ROWS there. A row without
# the field must stay byte-identical.
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
    '<li class="action-row coding-item" data-id="demo-project"><button type="button" class="action-row-fav star-btn" aria-pressed="true" title="Unstar (remove from favorites)" aria-label="Unstar (remove from favorites)"><svg class="icon action-row-fav-off" aria-hidden="true"><use href="#i-star"></use></svg><svg class="icon action-row-fav-on" aria-hidden="true"><use href="#i-star-fill"></use></svg></button><button type="button" class="action-row-main" aria-label="Launch Claude in demo-project"><span class="action-row-title" title="demo-project">demo-project</span><span class="action-row-meta has-chips"><span class="action-row-meta-text">main \u00b7 3 uncommitted</span><span class="chip" data-tone="attention">uncommitted</span></span></button><button type="button" class="action-row-kebab project-menu-anchor" title="Project actions" aria-label="Project actions"><svg class="icon" aria-hidden="true"><use href="#i-ellipsis-vertical"></use></svg></button></li>',
    '<li class="action-row coding-item" data-id="plain"><button type="button" class="action-row-fav star-btn" aria-pressed="false" title="Star (add to favorites)" aria-label="Star (add to favorites)"><svg class="icon action-row-fav-off" aria-hidden="true"><use href="#i-star"></use></svg><svg class="icon action-row-fav-on" aria-hidden="true"><use href="#i-star-fill"></use></svg></button><button type="button" class="action-row-main" disabled="" aria-label="Claude is not installed"><span class="action-row-title" title="plain">plain</span></button><button type="button" class="action-row-kebab project-menu-anchor" title="Project actions" aria-label="Project actions"><svg class="icon" aria-hidden="true"><use href="#i-ellipsis-vertical"></use></svg></button></li>',
]


def test_a_row_without_the_title_icon_is_byte_identical(
    authed_page: Page, base_url: str
) -> None:
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    assert authed_page.evaluate(_ROWS) == _GOLDEN
    # An explicit null is the same as leaving the field out (life-os.js
    # passes null for a skill with no bot).
    with_null = _ROWS.replace("title: 'plain',", "title: 'plain', titleIcon: null,")
    assert with_null != _ROWS
    assert authed_page.evaluate(with_null) == _GOLDEN
