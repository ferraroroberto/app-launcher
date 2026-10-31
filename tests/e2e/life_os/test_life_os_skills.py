"""Life OS tab e2e (issue #102).

Browser-side coverage: the tab renders skill tiles from
``/api/life-os/skills``, the model combo + ``☁️ Detached`` toggle are wired,
and a kebab Start new conversation POSTs ``/api/life-os/skills/<id>/launch`` with the
combo/toggle state — proving the bare ``/skill`` launch path is reached with
the right model/mode. Hermetic via route-mocks, like the Jobs e2e tests.

The server-side security (Cloudflare refusal, Tailscale gate, path-jail)
is covered by the in-process pytest API suite (tests/api/),
which can set client headers/host directly — over loopback the e2e
browser bypasses the gate entirely, so those checks belong there.

The tab's other browser coverage lives alongside in ``tests/e2e/life_os/``; the shared
mocks are in ``_support.py`` and the default recap route in ``conftest.py``."""

from __future__ import annotations

import json as _json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e._surface import surface
from tests.e2e.fixtures.ui import life_launch_switch
from tests.e2e.fixtures.waits import flush_requests, wait_until

from tests.e2e.life_os._support import (
    _convo_rows,
    _mock_conversations,
    _mock_skill_launches,
    _mock_skills,
    _open_life,
    _skill_menu_item,
)


pytestmark = pytest.mark.smoke


def test_life_os_tab_renders_skill_tiles(authed_page: Page, base_url: str) -> None:
    _mock_skills(authed_page)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    authed_page.wait_for_selector("#tabLifeOS", state="attached", timeout=5_000)
    authed_page.locator("#tabLifeOS").click()

    expect(authed_page.locator("#paneLifeOS")).to_be_visible()
    tiles = authed_page.locator("#lifeOsList li.lifeos-item")
    expect(tiles.first).to_be_visible(timeout=5_000)
    assert tiles.count() == 2
    expect(tiles.first).to_contain_text("journal-daily")
    # The shared Claude/Codex model dropdown + Detached toggle live in the
    # Skills card's toolbar (#496; provider parity in #845; out of the
    # summary since #1132).
    expect(authed_page.locator("#lifeOsModelCombo")).to_be_attached()
    expect(
        authed_page.locator(
            "#lifeOsModelMenu button[data-value='codex:gpt-6-luna']"
        )
    ).to_be_enabled()
    expect(authed_page.locator("#lifeOsDetached")).to_be_attached()
    surface(authed_page, "lifeos.tab")


def test_life_os_toggles_live_in_skills_summary_without_options_card(
    authed_page: Page, base_url: str
) -> None:
    """#496 round 2: the separate Life OS options card is gone — the model
    combo + Detached/Resume controls sit in the Skills card's summary (same
    structure as the Coding tab's Projects card, #540), and interacting with
    one must not collapse the panel."""
    _mock_skills(authed_page)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    authed_page.locator("#tabLifeOS").click()
    expect(authed_page.locator("#paneLifeOS")).to_be_visible()

    # The old standalone options card no longer exists.
    expect(authed_page.locator("#lifeOsOptions")).to_have_count(0)

    # The model dropdown renders in the Skills card's toolbar (#1132 moved
    # it out of its <summary>); both toggles live in its menu (#1503).
    toolbar = authed_page.locator("details.lifeos-list-card .card-toolbar")
    expect(toolbar.locator("#lifeOsModelCombo")).to_be_visible()
    expect(toolbar.locator(".launch-toolbar-switches")).to_have_count(0)

    # A toggle tap flips the switch but must not collapse the open panel.
    skills_card = authed_page.locator("details.lifeos-list-card")
    assert skills_card.evaluate("el => el.open") is True
    life_launch_switch(authed_page, "#lifeOsDetached")
    expect(authed_page.locator("#lifeOsDetached")).to_have_attribute(
        "aria-checked", "true"
    )
    assert skills_card.evaluate("el => el.open") is True, (
        "toggle tap must not collapse the Skills panel"
    )

    # Opening + picking in the model dropdown likewise must not collapse the
    # panel (#540 — its trigger/options are click targets inside the summary).
    authed_page.locator("#lifeOsModelBtn").click()
    expect(authed_page.locator("#lifeOsModelMenu")).to_be_visible()
    surface(authed_page, "lifeos.model-menu")
    authed_page.locator(
        "#lifeOsModelMenu button[data-value='codex:gpt-6-luna']"
    ).click()
    expect(authed_page.locator("#lifeOsModelCombo")).to_have_attribute(
        "data-value", "codex:gpt-6-luna"
    )
    expect(authed_page.locator("#lifeOsModelBtn")).to_have_text("Codex · Luna")
    assert skills_card.evaluate("el => el.open") is True, (
        "model-dropdown pick must not collapse the Skills panel"
    )


def _launch_journal_daily(
    page: Page, base_url: str, *, model: str | None = None,
    detached: bool = False,
) -> dict:
    """Open the Life OS tab, set the model/Detached controls, tap the
    journal-daily kebab's Start new conversation (a row tap opens the
    Conversations list since #1691), and return the POSTed launch payload. The
    mocked response is ``kind=remote`` so the client never opens a terminal
    overlay against the fake sid — ``launchSkill`` reads only ``body.session``,
    so the request payload is the whole observable."""
    _mock_skills(page)

    captured: dict = {}

    def _capture_launch(route):
        captured["body"] = route.request.post_data or ""
        route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({
                "launched": "journal-daily", "name": "journal-daily",
                "session": {"session_id": "x", "kind": "remote"},
            }),
        )

    page.route(
        re.compile(r".*/api/life-os/skills/journal-daily/launch$"),
        _capture_launch,
    )

    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.locator("#tabLifeOS").click()
    expect(page.locator("#lifeOsList li.lifeos-item").first).to_be_visible(
        timeout=5_000
    )

    if model:
        page.locator("#lifeOsModelBtn").click()
        page.locator(f"#lifeOsModelMenu button[data-value='{model}']").click()
    if detached:
        life_launch_switch(page, "#lifeOsDetached")

    _skill_menu_item(page, "journal-daily", "[data-item=lifeos-start-new-btn]").click()

    # Wait for the launch route to capture the POST body.
    wait_until(page, lambda: "body" in captured, "the launch POST")
    assert "body" in captured, "launch POST was never intercepted"
    return _json.loads(captured["body"])


def test_life_os_launch_posts_mode_and_model(
    authed_page: Page, base_url: str
) -> None:
    """#540/#845: launch carries the provider-qualified model choice. Detached
    on, so it launches detached → no terminal overlay / WS in the assertion."""
    payload = _launch_journal_daily(
        authed_page, base_url, model="codex:gpt-6-luna", detached=True,
    )
    # resume defaults to False on a normal (non-resume) launch (issue #151).
    assert payload == {
        "mode": "remote", "model": "codex:gpt-6-luna", "resume": False
    }, payload


def test_life_os_pty_launch_carries_terminal_size(
    authed_page: Page, base_url: str
) -> None:
    """Regression pin for issue #374: a streamed (pty) skill launch sizes
    the PTY at spawn. Skills stream output the moment the PTY exists, so a
    40×120 spawn poured 120-col text that re-wrapped into first-paint
    garble when the overlay's fit() shrank the PTY to phone width. A phone
    launch must carry rows/cols (launch-size.js estimateTermSize, same contract as the
    Coding tab, #126); a desktop client sends the mirror flag instead and
    keeps the Edge-window default."""
    # Detached stays OFF — this is the streamed pty path #374 is about.
    payload = _launch_journal_daily(authed_page, base_url)
    assert payload.get("mode") == "pty"
    if payload.get("desktop"):
        assert "rows" not in payload and "cols" not in payload
    else:
        assert payload.get("rows", 0) >= 10 and payload.get("cols", 0) >= 20


@pytest.mark.iphone
def test_life_os_row_tap_opens_conversations_and_starts_nothing(
    authed_page: Page, base_url: str
) -> None:
    """#1691: a skill row's tap opens that skill's Conversations list and
    starts no session (no /launch request, whatever the Resume switch says);
    the list's Start new then starts a fresh one."""
    page = authed_page
    _mock_skills(page)
    _mock_conversations(page)
    launches = _mock_skill_launches(page)
    _open_life(page, base_url)
    life_launch_switch(page, "#lifeOsDetached")
    life_launch_switch(page, "#lifeOsResume")

    row = page.locator("#lifeOsList li.lifeos-item[data-id='journal-daily']")
    expect(row.locator(".action-row-main")).to_have_attribute(
        "aria-label", "Conversations with journal-daily")
    row.locator(".action-row-main").click()

    expect(page.locator("#lifeOsConvoGroup")).to_be_visible(timeout=5_000)
    expect(_convo_rows(page).first).to_be_visible()
    expect(page.locator("#lifeOsConvoScope")).to_contain_text("journal-daily")
    flush_requests(page)
    assert launches == [], launches

    page.locator("#lifeOsConvoStartNew").click()
    wait_until(page, lambda: len(launches) == 1, "the Start new launch")
    assert launches[0] == (
        "journal-daily", {"mode": "remote", "model": "claude:sonnet", "resume": False}
    ), launches


@pytest.mark.iphone
def test_life_os_row_tap_on_a_skill_with_no_conversations_offers_start_new(
    authed_page: Page, base_url: str
) -> None:
    """#1691: a skill with no conversations yet opens the same list, empty,
    with Start new as the way to the first one."""
    page = authed_page
    _mock_skills(page)
    _mock_conversations(
        page, {"skill": "journal-daily", "available": True, "conversations": []})
    launches = _mock_skill_launches(page)
    _open_life(page, base_url)

    page.locator(
        "#lifeOsList li.lifeos-item[data-id='journal-daily'] .action-row-main"
    ).click()

    expect(page.locator("#lifeOsConvoGroup")).to_be_visible(timeout=5_000)
    expect(_convo_rows(page)).to_have_count(0)
    start = page.locator("#lifeOsConvoStartNew")
    expect(start).to_be_visible()
    assert launches == [], launches
    life_launch_switch(page, "#lifeOsDetached")
    start.click()
    wait_until(page, lambda: len(launches) == 1, "the Start new launch")
    assert launches[0][0] == "journal-daily", launches


@pytest.mark.iphone
def test_life_os_tile_keeps_name_and_buttons_on_one_row(
    authed_page: Page, base_url: str
) -> None:
    """Regression for #124: a Life tile's name and its action strip stay on a
    single inline row even on a narrow phone — they must NOT inherit the
    Coding tab's stack-on-narrow rule (#120) via the shared ``.coding-item``
    class. On the WebKit projection this runs at the iPhone width (430px <
    the 520px breakpoint), so it exercises the media query directly.

    The tile carried two actions (📖 + 🚀) when this was written and three
    (📖 + 🕘 + 🚀) since #727 added Conversations — which is exactly why the
    assertion is geometric rather than a count: what matters is that the row
    never stacks, whatever it holds.

    Asserted via geometry: when inline, the name and the action strip both
    span the tile's full height and so overlap vertically; when wrongly
    stacked, the name sits in the top band and the actions in a bottom strip
    with no vertical overlap.
    """
    _mock_skills(authed_page)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    authed_page.locator("#tabLifeOS").click()

    tile = authed_page.locator(
        "#lifeOsList li.lifeos-item[data-id='journal-daily']"
    )
    expect(tile).to_be_visible(timeout=5_000)

    # Gate the geometry read on the *children* being laid out, not just the
    # tile (#182). `_default_recap` already stubs the recap tile hidden, so the
    # recap reflow isn't the cause here; the residual race is a plain layout
    # settle on the loaded hosted runner — expect(tile).to_be_visible() can
    # pass a tick before the child .coding-name / action strip are painted, so
    # a single immediate bounding_box() read returns None. Wait for both parts
    # to be visible, then poll until both boxes settle.
    # Since #1128 the row is the vendored action-row: the title and the one
    # trailing kebab are what must share the line.
    name = tile.locator(".action-row-title")
    actions = tile.locator(".action-row-kebab")
    expect(name).to_be_visible(timeout=5_000)
    expect(actions).to_be_visible(timeout=5_000)

    name_box = actions_box = None
    for _ in range(50):
        name_box = name.bounding_box()
        actions_box = actions.bounding_box()
        if name_box and actions_box:
            break
        authed_page.wait_for_timeout(100)
    assert name_box and actions_box, "tile parts not laid out"

    # Vertical overlap → same row (inline). No overlap → stacked (the bug).
    overlap = (
        name_box["y"] < actions_box["y"] + actions_box["height"]
        and actions_box["y"] < name_box["y"] + name_box["height"]
    )
    assert overlap, (
        f"Life tile is stacked, not inline: name={name_box}, "
        f"actions={actions_box} — #124 regression"
    )
    # Actions sit to the right of the name, not beneath it.
    assert actions_box["x"] >= name_box["x"] + name_box["width"] - 2, (
        f"action strip is not right of the name: name={name_box}, "
        f"actions={actions_box}"
    )


def test_life_os_browser_sheet_opens_a_file_over_the_list(
    authed_page: Page, base_url: str
) -> None:
    """Read opens the Browse sheet on the skill's file list; a file opens in
    the document sheet over it, whose ✕ comes back to the list, and the
    list's ✕ closes Browse (#1504: sheets, one back style). Hermetic —
    /files + /file are route-mocked."""
    _mock_skills(authed_page)
    authed_page.route(
        re.compile(r".*/api/life-os/skills/journal-daily/files$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({
                "skill": {"id": "journal-daily", "name": "journal-daily"},
                "files": [
                    {"path": ".claude/skills/journal-daily/SKILL.md",
                     "name": "SKILL.md", "category": "skill"},
                    {"path": ".claude/skills/journal-daily/memory/observations.md",
                     "name": "observations.md", "category": "memory"},
                ],
            }),
        ),
    )
    authed_page.route(
        re.compile(r".*/api/life-os/file\?.*$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({
                "path": "x", "name": "SKILL.md",
                "content": "# Heading\n\nbody text", "truncated": False,
            }),
        ),
    )

    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    authed_page.locator("#tabLifeOS").click()
    # Read lives in the row's ⋯ menu since #1128.
    _skill_menu_item(authed_page, "journal-daily", "[data-item=lifeos-browse-btn]").click()

    browser = authed_page.locator("#lifeOsBrowser")
    doc = authed_page.locator("#lifeOsDoc")
    expect(browser).to_be_visible()
    expect(authed_page.locator("#lifeOsBrowserTitle")).to_have_text("journal-daily")
    expect(authed_page.locator(".lifeos-file-btn").first).to_be_visible(timeout=5_000)
    expect(doc).to_be_hidden()
    surface(authed_page, "lifeos.browser")

    authed_page.locator(".lifeos-file-btn").first.click()
    expect(doc).to_be_visible()
    expect(authed_page.locator("#lifeOsFileContent")).to_contain_text("body text")
    expect(authed_page.locator("#lifeOsDocTitle")).to_have_text("SKILL.md")
    # Not a conversation log: nothing to rename or delete.
    expect(authed_page.locator("#lifeOsDocLinks")).to_be_hidden()
    surface(authed_page, "lifeos.doc")

    authed_page.locator("#lifeOsDocClose").click()
    expect(doc).to_be_hidden()
    expect(browser).to_be_visible()
    authed_page.locator("#lifeOsBrowserClose").click()
    expect(browser).to_be_hidden()


def test_life_os_delete_conversation_log_from_the_document_sheet(
    authed_page: Page, base_url: str
) -> None:
    """🗑️ never appears in the browse list; Delete log shows under the text
    only when the open file is a conversation log. Confirming DELETEs and
    returns to the list, which reloads without the log. Hermetic — /files
    reload drops the log on the 2nd call, DELETE is mocked."""
    _mock_skills(authed_page)

    calls = {"n": 0}

    def _files(route):
        calls["n"] += 1
        convs = [] if calls["n"] > 1 else [{
            "path": ".claude/skills/journal-daily/conversations/trial.md",
            "name": "trial.md", "category": "conversations",
        }]
        files = convs + [{
            "path": ".claude/skills/journal-daily/memory/observations.md",
            "name": "observations.md", "category": "memory",
        }]
        route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"skill": {"id": "journal-daily",
                              "name": "journal-daily"}, "files": files}),
        )

    deleted = {"hit": False}

    def _file(route):
        # GET returns content; DELETE records the hit. Same path, two verbs.
        if route.request.method == "DELETE":
            deleted["hit"] = True
            route.fulfill(status=200, content_type="application/json",
                          body=_json.dumps({"deleted": "x"}))
        else:
            route.fulfill(status=200, content_type="application/json",
                          body=_json.dumps({"path": "x", "name": "trial.md",
                                            "content": "log body",
                                            "truncated": False}))

    authed_page.route(
        re.compile(r".*/api/life-os/skills/journal-daily/files$"), _files
    )
    authed_page.route(
        re.compile(r".*/api/life-os/file\?.*$"), _file
    )
    native: list = []
    authed_page.on("dialog", lambda d: (native.append(d.message), d.dismiss()))

    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    authed_page.locator("#tabLifeOS").click()
    _skill_menu_item(authed_page, "journal-daily", "[data-item=lifeos-browse-btn]").click()

    # No delete control anywhere in the list.
    expect(authed_page.locator(".lifeos-file-btn").first).to_be_visible(
        timeout=5_000
    )
    expect(authed_page.locator("#lifeOsFileList button:not(.lifeos-file-btn)")).to_have_count(0)

    # Open the memory file → Delete log stays hidden (not a conversation log).
    authed_page.locator(".lifeos-file-btn:has-text('observations.md')").click()
    expect(authed_page.locator("#lifeOsDoc")).to_be_visible()
    expect(authed_page.locator("#lifeOsDocDelete")).to_be_hidden()
    authed_page.locator("#lifeOsDocClose").click()

    # Open the conversation log → Delete log shows under its text.
    authed_page.locator(".lifeos-file-btn:has-text('trial.md')").click()
    expect(authed_page.locator("#lifeOsFileContent")).to_contain_text("log body")
    expect(authed_page.locator("#lifeOsDocDelete")).to_be_visible()

    # Confirm delete (the vendored confirm sheet, #1439, never a native
    # confirm()) → DELETE fires, the document closes back to the list, log gone.
    authed_page.locator("#lifeOsDocDelete").click()
    expect(authed_page.locator("#confirmDialog")).to_be_visible()
    expect(authed_page.locator("#confirmDialogMessage")).to_contain_text("trial.md")
    authed_page.locator("#confirmDialogOk").click()
    wait_until(authed_page, lambda: deleted["hit"], "the doc DELETE")
    assert native == [], native
    expect(authed_page.locator("#lifeOsDoc")).to_be_hidden()
    expect(authed_page.locator("#lifeOsBrowser")).to_be_visible()
    expect(
        authed_page.locator(".lifeos-file-btn:has-text('trial.md')")
    ).to_have_count(0)


@pytest.mark.iphone
def test_life_os_browser_folds_sub_folders_below_the_top_level_files(
    authed_page: Page, base_url: str
) -> None:
    """A category's top-level files list first and each sub-folder is one
    collapsed row with its file count, so a curated file is on the first
    screen above generated output; tapping the row expands it."""
    _mock_skills(authed_page)
    prefix = ".claude/skills/journal-daily/context/"
    names = ["_history/a.md", "_history/b.md", "corpus/c.md", "style-guide.md"]
    authed_page.route(
        re.compile(r".*/api/life-os/skills/journal-daily/files$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"skill": {"id": "journal-daily",
                              "name": "journal-daily"}, "files": [
                {"path": prefix + n, "name": n, "category": "context"}
                for n in names]}),
        ),
    )
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    authed_page.locator("#tabLifeOS").click()
    _skill_menu_item(authed_page, "journal-daily", "[data-item=lifeos-browse-btn]").click()

    rows = authed_page.locator("#lifeOsFileList li:not([hidden])")
    expect(rows.nth(1)).to_have_text("style-guide.md")
    folder = authed_page.locator(".lifeos-folder-btn:has-text('_history')")
    expect(folder).to_contain_text("_history · 2 files")
    expect(folder).to_have_attribute("aria-expanded", "false")
    expect(authed_page.locator(".lifeos-file-btn:has-text('a.md')")).to_be_hidden()

    folder.click()
    expect(folder).to_have_attribute("aria-expanded", "true")
    expect(authed_page.locator(".lifeos-file-btn:has-text('a.md')")).to_be_visible()
    expect(authed_page.locator(".lifeos-file-btn:has-text('c.md')")).to_be_hidden()
