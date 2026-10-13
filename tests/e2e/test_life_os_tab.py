"""Life OS tab e2e (issue #102).

Browser-side coverage: the tab renders skill tiles from
``/api/life-os/skills``, the model combo + ``☁️ Detached`` toggle are wired,
and tapping launch POSTs ``/api/life-os/skills/<id>/launch`` with the
combo/toggle state — proving the bare ``/skill`` launch path is reached with
the right model/mode. Hermetic via route-mocks, like the Jobs e2e tests.

The server-side security (Cloudflare refusal, Tailscale gate, path-jail)
is covered by the in-process pytest API suite (tests/test_webapp_api_life_os.py),
which can set client headers/host directly — over loopback the e2e
browser bypasses the gate entirely, so those checks belong there.
"""

from __future__ import annotations

import json as _json
import re

import pytest
from playwright.sync_api import Locator, Page, expect

from tests.e2e._geometry import assert_min_target, assert_no_overlap
from tests.e2e.conftest import (
    flush_requests,
    life_launch_switch,
    open_settings_sheet,
    stable_eval,
    stable_read,
    wait_until,
)
from tests.e2e.test_jobs_log_copy import _CLIPBOARD_MOCK
from tests.e2e.test_overlay_standalone_scrollable import (
    _PATCH_MATCH_MEDIA,
    _PROJECT_STANDALONE_CSS,
)

pytestmark = pytest.mark.smoke

_SEARCH_URL = re.compile(r".*/api/life-os/conversations/search.*")

_FAKE_SKILLS = {
    "available": True,
    "life_os_dir": "E:/automation/life-os",
    "skills": [
        {
            "id": "journal-daily",
            "name": "journal-daily",
            "command": "journal-daily",
            "description": "Turns a transcript into a journal.",
            "skill_md": ".claude/skills/journal-daily/SKILL.md",
        },
        {
            "id": "sparring-work",
            "name": "sparring-work",
            "command": "sparring-work",
            "description": "Sparring partner for work relationships.",
            "skill_md": ".claude/skills/sparring-work/SKILL.md",
        },
    ],
}


def _mock_skills(page: Page) -> None:
    page.route(
        re.compile(r".*/api/life-os/skills(\?.*)?$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps(_FAKE_SKILLS),
        ),
    )


def _mock_recap(
    page: Page, *, staleness: str = "due", age_days: float = 9.0,
    available: bool = True, proposal_pending: bool = False,
) -> None:
    page.route(
        re.compile(r".*/api/life-os/recap-status$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({
                "available": available, "ledger_exists": True,
                "age_days": age_days, "staleness": staleness,
                "proposal_pending": proposal_pending, "proposal_name": None,
            }),
        ),
    )


@pytest.fixture(autouse=True)
def _default_recap(authed_page: Page) -> None:
    """Stub /api/life-os/recap-status for every test so opening the Life OS tab
    is hermetic — without this the live endpoint answers (life-os is checked
    out beside the repo), unhiding the recap tile asynchronously and reflowing
    the list mid-measurement, which jitters the #124 tile-geometry assertion.
    Default is ``available:false`` → the recap tile stays hidden, so tests that
    aren't about the recap see the exact pre-feature layout. The two recap
    tests register their own ``_mock_recap`` after this; Playwright matches
    routes last-registered-first, so that one wins."""
    _mock_recap(authed_page, available=False)
    # The skills field searches conversations from two letters (#1504): a
    # default stub, so no test's typing reaches a real conversation index.
    # Tests that search register their own, which wins (last-registered).
    authed_page.route(_SEARCH_URL, lambda route: route.fulfill(
        status=200, content_type="application/json",
        body=_json.dumps({"available": True, "query": "", "skill": "", "results": []})))


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
            "#lifeOsModelMenu button[data-value='codex:gpt-6-astra']"
        )
    ).to_be_enabled()
    expect(authed_page.locator("#lifeOsDetached")).to_be_attached()


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
    authed_page.locator(
        "#lifeOsModelMenu button[data-value='codex:gpt-6-astra']"
    ).click()
    expect(authed_page.locator("#lifeOsModelCombo")).to_have_attribute(
        "data-value", "codex:gpt-6-astra"
    )
    expect(authed_page.locator("#lifeOsModelBtn")).to_have_text("Codex · Astra")
    assert skills_card.evaluate("el => el.open") is True, (
        "model-dropdown pick must not collapse the Skills panel"
    )


def _launch_journal_daily(
    page: Page, base_url: str, *, model: str | None = None,
    detached: bool = False, resume: bool = False,
) -> dict:
    """Open the Life OS tab, set the model/Detached/Resume controls, tap the
    journal-daily tile's launch, and return the POSTed launch payload. The
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
    if resume:
        life_launch_switch(page, "#lifeOsResume")

    tile = page.locator("#lifeOsList li.lifeos-item[data-id='journal-daily']")
    tile.locator(".action-row-main").click()

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
        authed_page, base_url, model="codex:gpt-6-astra", detached=True,
    )
    # resume defaults to False on a normal (non-resume) launch (issue #151).
    assert payload == {
        "mode": "remote", "model": "codex:gpt-6-astra", "resume": False
    }, payload


def test_life_os_pty_launch_carries_terminal_size(
    authed_page: Page, base_url: str
) -> None:
    """Regression pin for issue #374: a streamed (pty) skill launch sizes
    the PTY at spawn. Skills stream output the moment the PTY exists, so a
    40×120 spawn poured 120-col text that re-wrapped into first-paint
    garble when the overlay's fit() shrank the PTY to phone width. A phone
    launch must carry rows/cols (estimateTermSize, same contract as the
    Coding tab, #126); a desktop client sends the mirror flag instead and
    keeps the Edge-window default."""
    # Detached stays OFF — this is the streamed pty path #374 is about.
    payload = _launch_journal_daily(authed_page, base_url)
    assert payload.get("mode") == "pty"
    if payload.get("desktop"):
        assert "rows" not in payload and "cols" not in payload
    else:
        assert payload.get("rows", 0) >= 10 and payload.get("cols", 0) >= 20


def test_life_os_detached_resume_posts_remote_console(
    authed_page: Page, base_url: str
) -> None:
    """Regression for #239: Detached and Resume are orthogonal on the Life OS
    tab (matching the Coding tab, #157). Flipping both must POST
    ``mode: remote`` AND ``resume: true`` — the picker renders in the detached
    console — rather than Resume silently forcing a streamed PTY."""
    payload = _launch_journal_daily(
        authed_page, base_url, detached=True, resume=True,
    )
    assert payload.get("mode") == "remote", payload
    assert payload.get("resume") is True, payload


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
    _skill_menu_item(authed_page, "journal-daily", ".lifeos-browse-btn").click()

    browser = authed_page.locator("#lifeOsBrowser")
    doc = authed_page.locator("#lifeOsDoc")
    expect(browser).to_be_visible()
    expect(authed_page.locator("#lifeOsBrowserTitle")).to_have_text("journal-daily")
    expect(authed_page.locator(".lifeos-file-btn").first).to_be_visible(timeout=5_000)
    expect(doc).to_be_hidden()

    authed_page.locator(".lifeos-file-btn").first.click()
    expect(doc).to_be_visible()
    expect(authed_page.locator("#lifeOsFileContent")).to_contain_text("body text")
    expect(authed_page.locator("#lifeOsDocTitle")).to_have_text("SKILL.md")
    # Not a conversation log: nothing to rename or delete.
    expect(authed_page.locator("#lifeOsDocLinks")).to_be_hidden()

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
    _skill_menu_item(authed_page, "journal-daily", ".lifeos-browse-btn").click()

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


# ------------------------------------------------- conversations (issue #727)
# Since #1504 they live in the skills field: a search shows them as a second
# group under the skills, a skill's ⋮ Conversations narrows that group to the
# skill (the chip in the field), and a row opens the conversation sheet.

_RESUMABLE_SID = "e70b4cb1-9f3d-4a21-8c55-2b7d19a4f6e0"

_FAKE_CONVERSATIONS = {
    "skill": "journal-daily",
    "available": True,
    "conversations": [
        {
            "skill": "journal-daily",
            "file": "2026-08-01-0900-ferry-booking.md",
            "path": ".claude/skills/journal-daily/conversations/"
                    "2026-08-01-0900-ferry-booking.md",
            "date": "2026-08-01", "slug": "ferry-booking", "turns": 12,
            "sid": _RESUMABLE_SID, "agent": "claude",
            "topic": "booking the ferry", "decisions": "took the 07:40",
            "open_loops": "confirm the return leg", "resumable": True,
            "revision": "a" * 64, "handoff_available": True, "handoff_truncated": False,
        },
        {
            "skill": "journal-daily",
            "file": "2026-06-01-1917-trial.md",
            "path": ".claude/skills/journal-daily/conversations/"
                    "2026-06-01-1917-trial.md",
            "date": "2026-06-01", "slug": "trial", "turns": 4,
            "sid": "", "agent": "claude",
            "topic": "an early trial run", "decisions": "none",
            "open_loops": "none", "resumable": False,
            "resume_reason": "Legacy capture has no source harness; readable only.",
        },
    ],
}


def _mock_conversations(page: Page, body: dict = None) -> None:
    page.route(
        re.compile(r".*/api/life-os/skills/journal-daily/conversations$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps(body if body is not None else _FAKE_CONVERSATIONS),
        ),
    )


def _mock_search(page: Page, results: list, *, seen: list = None, **extra) -> None:
    """Stub the cross-skill search; ``seen`` collects every request URL."""
    def _fulfill(route):
        if seen is not None:
            seen.append(route.request.url)
        route.fulfill(status=200, content_type="application/json", body=_json.dumps(
            {"available": True, "query": "", "skill": "", "results": results, **extra}))
    page.route(_SEARCH_URL, _fulfill)


def _pick_life_model(page: Page, value: str) -> None:
    """Pick ``value`` in the Skills toolbar's model combo (#1503: inside its
    menu, under the model list), the model a handoff starts in (#1504).

    #935: the options arrive with the boot GET /api/config, whose
    ``setOptions`` closes the menu and replaces every option, so the option
    existing is the readiness signal before opening the menu."""
    option = page.locator(f"#lifeOsModelMenu button[data-value='{value}']")
    option.wait_for(state="attached")
    page.locator("#lifeOsModelBtn").click()
    expect(option).to_be_visible()
    option.click()
    expect(page.locator("#lifeOsModelCombo")).to_have_attribute("data-value", value)


_FAKE_TRANSCRIPT = {
    "path": ".claude/skills/journal-daily/conversations/"
            "2026-08-01-0900-ferry-booking.md",
    "name": "2026-08-01-0900-ferry-booking.md",
    "available": True,
    "agent": "claude",
    "truncated": False,
    "entries": [
        {"kind": "user", "text": "book the ferry for Friday",
         "offset": 0, "timestamp": None, "truncated": False},
        {"kind": "assistant", "text": "Booked the **07:40**.",
         "offset": 120, "timestamp": None, "truncated": False},
    ],
}


def _mock_transcript(page: Page, body: dict = None) -> None:
    """Stub the viewer's parsed read (#1119). Synthetic turns only — the real
    life-os checkout holds private conversations and is never read here."""
    page.route(
        re.compile(r".*/api/life-os/file/transcript.*"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps(body if body is not None else _FAKE_TRANSCRIPT),
        ),
    )


def _convo_rows(page: Page) -> Locator:
    return page.locator("#lifeOsConvoList .lifeos-convo-row")


def _open_sheet(page: Page, index: int = 0) -> Locator:
    """Open conversation row ``index``'s sheet: a tap on the row (#1504)."""
    _convo_rows(page).nth(index).locator(".action-row-main").click()
    sheet = page.locator("#lifeOsConvoSheet")
    expect(sheet).to_be_visible(timeout=5_000)
    return sheet


def _close_sheet(page: Page) -> None:
    page.locator("#lifeOsConvoSheetClose").click()
    expect(page.locator("#lifeOsConvoSheet")).to_be_hidden()


def _open_viewer(page: Page, index: int = 0) -> Locator:
    """Open row ``index``'s sheet, then its Read: the reader stacks over it."""
    _open_sheet(page, index).locator(".lifeos-convo-read").click()
    expect(page.locator("#lifeOsConvoViewer")).to_be_visible(timeout=5_000)
    return page.locator("#lifeOsViewerList")


def _month_day(month: str, day: int) -> str:
    """A regex for the row's short date, English and day-first ("1 Aug")
    whatever the browser's locale (#1528: an es browser printed "1 ago"),
    with the year only when it is not the current one."""
    return rf"{day} {month}(?: \d{{4}})?"


def _open_life(page: Page, base_url: str) -> None:
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.locator("#tabLifeOS").click()
    # Attached, not visible: a restored filter may hide the first row.
    expect(page.locator("#lifeOsList li.lifeos-item")).to_have_count(2, timeout=5_000)


def _open_conversations(page: Page, base_url: str) -> None:
    """Open the Life OS tab and journal-daily's ⋮ Conversations."""
    _open_life(page, base_url)
    _skill_menu_item(page, "journal-daily", ".lifeos-convo-btn").click()
    expect(page.locator("#lifeOsConvoGroup")).to_be_visible(timeout=5_000)


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
    tab = lambda mode: sort.locator(f".range-tab[data-sort='{mode}']")  # noqa: E731
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
    # ✕ comes back to the sheet the link opened under it.
    authed_page.locator("#lifeOsViewerClose").click()
    expect(viewer).to_be_hidden()
    expect(authed_page.locator("#lifeOsConvoSheetTitle")).to_have_text("booking the ferry")
    expect(authed_page.locator("#lifeOsConvoSheet")).to_be_visible()

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

    dialog = authed_page.locator("#lifeOsRenameDialog")
    rename = _open_sheet(authed_page, 0).locator(".lifeos-convo-rename")
    rename.click()
    expect(dialog).to_be_visible()
    expect(authed_page.locator("#lifeOsRenameHeading")).to_have_text("Rename conversation")
    expect(authed_page.locator("#lifeOsRenameInput")).to_have_value("")
    expect(authed_page.locator("#lifeOsRenameInput")).to_have_attribute(
        "placeholder", "booking the ferry")
    # The header ✕ cancels without a request, back on the sheet.
    authed_page.locator("#lifeOsRenameClose").click()
    expect(dialog).to_be_hidden()
    expect(authed_page.locator("#lifeOsConvoSheet")).to_be_visible()
    flush_requests(authed_page)
    assert patches == []

    rename.click()
    authed_page.locator("#lifeOsRenameInput").fill("  Ferry  to the island ")
    authed_page.locator("#lifeOsRenameSave").click()
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
    _skill_menu_item(authed_page, "journal-daily", ".lifeos-convo-btn").click()
    expect(first.locator(".action-row-title")).to_have_text("Ferry to the island")
    expect(first).not_to_contain_text("booking the ferry")

    # The dialog opens on the current title; saving it empty clears it.
    _open_sheet(authed_page, 0).locator(".lifeos-convo-rename").click()
    expect(authed_page.locator("#lifeOsRenameInput")).to_have_value("Ferry to the island")
    authed_page.locator("#lifeOsRenameInput").fill("")
    authed_page.locator("#lifeOsRenameSave").click()
    wait_until(authed_page, lambda: len(patches) == 2, "the clearing PATCH")
    assert patches[1][1] == {"title": ""}
    assert titles == {}
    expect(authed_page.locator("#lifeOsConvoSheetTitle")).to_have_text("booking the ferry")
    expect(first.locator(".action-row-title")).to_have_text("booking the ferry")
    assert native == [], native


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
    authed_page.locator("#lifeOsRenameInput").fill("Nope")
    authed_page.locator("#lifeOsRenameSave").click()
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
    _skill_menu_item(authed_page, "journal-daily", ".lifeos-convo-btn").click()

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
    tab = lambda mode: sort.locator(f".range-tab[data-sort='{mode}']")  # noqa: E731
    expect(titles).to_have_text(
        ["the ferry deep dive", "a ferry mention", "ferry in passing"]
    )
    expect(sort).to_have_attribute("data-sort", "relevance")
    expect(sort.locator(".range-tab:visible")).to_have_text(["Relevance", "Recent", "Created"])
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
    _skill_menu_item(authed_page, "journal-daily", ".lifeos-convo-btn").click()
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
        re.compile(r"is-convo-scoped"))
    expect(authed_page.locator("#lifeOsSkillsHead")).to_have_text("Skills (0)")
    expect(_convo_rows(authed_page).first.locator(".action-row-meta")).to_contain_text(
        "journal-daily")


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


# A capture carrying an AskUserQuestion call (#1149). Today's capture reader
# drops tool calls upstream, so this pins the renderer's own guarantee: a
# question card mounted here is history, never a control.
_QUESTION_TRANSCRIPT = dict(_FAKE_TRANSCRIPT, entries=_FAKE_TRANSCRIPT["entries"] + [
    {"kind": "tool_call", "name": "AskUserQuestion", "summary": "questions",
     "call_id": "toolu_viewer", "result": None, "result_truncated": False,
     "sidechain": False, "offset": 300, "timestamp": None,
     "questions": [{"question": "Which sailing?", "header": "Ferry",
                    "multiSelect": False, "options": [
                        {"label": "07:40", "description": "first boat"},
                        {"label": "11:15", "description": "late morning"}]}]},
    {"kind": "tool_call", "name": "ExitPlanMode", "summary": "# Book it",
     "call_id": "toolu_viewer_plan", "plan": "## Book it\n\n- pay by card",
     "plan_truncated": False, "result": "User has approved your plan.",
     "plan_outcome": "approved", "result_truncated": False,
     "sidechain": False, "offset": 400, "timestamp": None},
])


def test_capture_opens_in_the_chat_transcript_view(
    authed_page: Page, base_url: str
) -> None:
    """#1119: Read opens the capture in the session overlay's own Chat
    renderer — real turn cards, the user's prompt as plain text and the reply
    through markdown — not the raw document viewer. One surface, two mounts
    (#979), so the assertion is on that renderer's own classes. #1149: a
    question card renders here too, and read-only. #1504: the reader is a
    sheet over the conversation sheet, which keeps Open raw."""
    _mock_skills(authed_page)
    _mock_conversations(authed_page)
    _mock_transcript(authed_page, _QUESTION_TRANSCRIPT)
    authed_page.route(re.compile(r".*/api/life-os/file\?.*$"), lambda route: route.fulfill(
        status=200, content_type="application/json", body=_json.dumps(
            {"path": "x", "name": "2026-08-01-0900-ferry-booking.md",
             "content": "You: book the ferry", "truncated": False})))
    _open_conversations(authed_page, base_url)

    viewer_list = _open_viewer(authed_page, 0)
    expect(authed_page.locator("#lifeOsViewerTitle")).to_have_text("booking the ferry")
    turns = viewer_list.locator(".tr-turn")
    expect(turns).to_have_count(2)
    expect(turns.first).to_have_class(re.compile(r"\btr-user\b"))
    expect(turns.first).to_contain_text("book the ferry for Friday")
    expect(turns.nth(1)).to_have_class(re.compile(r"\btr-assistant\b"))
    # The reply goes through the markdown renderer; the prompt does not.
    expect(turns.nth(1).locator(".tr-md strong")).to_have_text("07:40")
    # Read-only: no composer anywhere in the reader.
    expect(authed_page.locator("#lifeOsConvoViewer .composer")).to_have_count(0)
    # #1149: the question renders as its card, beside the turns rather than
    # in a group, with no live control — not one enabled option, no text
    # field — and says it holds no answer instead of offering one.
    card = viewer_list.locator(".tr-ask-item")
    expect(card).to_be_visible()
    expect(card.locator(".tr-ask-question")).to_have_text("Which sailing?")
    expect(card.locator(".tr-ask-label")).to_have_text(["07:40", "11:15"])
    expect(card.locator(".tr-ask-desc").first).to_have_text("first boat")
    expect(card).to_have_attribute("data-mode", "history")
    expect(card.locator(".tr-ask-opt:enabled")).to_have_count(0)
    expect(card.locator(".tr-ask-input")).to_be_hidden()
    expect(card.locator(".tr-ask-status")).to_have_text("No answer recorded here")
    # #1151: a plan renders as its card here too — markdown, its outcome,
    # and nothing to tap.
    plan = viewer_list.locator(".tr-plan-item")
    expect(plan.locator(".tr-plan-body h2")).to_have_text("Book it")
    expect(plan).to_have_attribute("data-mode", "approved")
    expect(plan.locator("button")).to_have_count(0)
    # Done comes back to the sheet; its Open raw stacks the raw document over
    # it, and the document's ✕ comes back to the sheet again.
    authed_page.locator("#lifeOsViewerDone").click()
    expect(authed_page.locator("#lifeOsConvoViewer")).to_be_hidden()
    sheet = authed_page.locator("#lifeOsConvoSheet")
    expect(sheet).to_be_visible()
    sheet.locator(".lifeos-convo-open-raw").click()
    expect(authed_page.locator("#lifeOsDoc")).to_be_visible()
    expect(authed_page.locator("#lifeOsFileContent")).to_contain_text("book the ferry")
    authed_page.locator("#lifeOsDocClose").click()
    expect(authed_page.locator("#lifeOsDoc")).to_be_hidden()
    expect(sheet).to_be_visible()


_LONG_TRANSCRIPT = dict(_FAKE_TRANSCRIPT, entries=[
    {"kind": "user" if i % 2 == 0 else "assistant",
     "text": f"synthetic turn {i} " + "about ferry timetables and ticket prices " * 3,
     "offset": i * 100, "timestamp": None, "truncated": False}
    for i in range(40)
])


def test_viewer_latest_pill_jumps_to_the_last_turn(
    authed_page: Page, base_url: str
) -> None:
    """#1140: the shared ↓ Latest pill on the conversation reader. A capture
    that fits has no end to jump to. A long one still opens at its start — it
    reads top-down — but shows the pill straight away, and one tap reaches
    the last turn."""
    _mock_skills(authed_page)
    _mock_conversations(authed_page)
    # The first row's capture is long, the second's two turns.
    authed_page.route(re.compile(r".*/api/life-os/file/transcript.*"), lambda route: route.fulfill(
        status=200, content_type="application/json", body=_json.dumps(
            _LONG_TRANSCRIPT if "ferry-booking" in route.request.url else _FAKE_TRANSCRIPT)))
    _open_conversations(authed_page, base_url)

    pill = authed_page.locator("#lifeOsViewerLatest")
    expect(_open_viewer(authed_page, 1).locator(".tr-turn")).to_have_count(2)
    expect(pill).to_be_hidden()
    authed_page.locator("#lifeOsViewerClose").click()
    _close_sheet(authed_page)

    turns = _open_viewer(authed_page, 0).locator(".tr-turn")
    expect(turns).to_have_count(40)
    expect(turns.first).to_be_in_viewport()
    expect(pill).to_be_visible()

    pill.click()
    expect(turns.last).to_be_in_viewport()
    expect(pill).to_be_hidden()


_MEASURE_PILL = """
() => {
  const pill = document.getElementById('lifeOsViewerLatest');
  const r = pill.getBoundingClientRect();
  const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
  return {
    vh: window.innerHeight, vw: window.innerWidth,
    top: r.top, bottom: r.bottom, left: r.left, right: r.right,
    hittable: !!hit && (hit === pill || pill.contains(hit)),
    hitBy: hit ? (hit.id || hit.className || hit.tagName) : null,
    appOverflowY: getComputedStyle(document.querySelector('main.app')).overflowY,
  };
}
"""


@pytest.mark.iphone
def test_reader_covers_the_tab_bar_in_the_standalone_shell(
    authed_page: Page, base_url: str, browser_name: str
) -> None:
    """#1143: in the installed PWA the floating tab bar sat on top of the
    conversation viewer and hid its ↓ Latest pill. Since #1504 the reader and
    the conversation sheet are native dialogs in the top layer, which the
    vendored shell hides the bar for (body:has(dialog[open])): the pill must
    be on screen and hittable, and .app locked behind them.

    Neither headless engine supports display-mode: standalone, so this
    projects the shell the way #1099's test does. A pass here doesn't replace
    checking on the device."""
    page = authed_page
    if browser_name == "chromium":
        # The phone rules also need a coarse pointer.
        cdp = page.context.new_cdp_session(page)
        cdp.send("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 5})
    page.add_init_script(_PATCH_MATCH_MEDIA)
    page.set_viewport_size({"width": 390, "height": 844})
    _mock_skills(page)
    _mock_conversations(page)
    _mock_transcript(page, _LONG_TRANSCRIPT)
    page.goto(f"{base_url}/", wait_until="load")
    assert page.evaluate(_PROJECT_STANDALONE_CSS) >= 1, (
        "no display-mode: standalone rule to project; the vendored shell "
        "block moved and this would test the browser-tab layout instead"
    )
    tabs = page.locator("nav.tabs")
    expect(tabs).to_be_visible()

    page.locator("#tabLifeOS").click()
    _skill_menu_item(page, "journal-daily", ".lifeos-convo-btn").click()
    expect(_convo_rows(page)).to_have_count(2, timeout=5_000)
    _open_sheet(page, 0)
    expect(tabs).to_be_hidden()

    expect(_open_viewer_from_sheet(page).locator(".tr-turn")).to_have_count(40)
    expect(tabs).to_be_hidden()
    pill = page.locator("#lifeOsViewerLatest")
    expect(pill).to_be_visible()
    m = page.evaluate(_MEASURE_PILL)
    assert m["top"] >= 0 and m["bottom"] <= m["vh"] + 0.5, f"pill off-screen vertically: {m}"
    assert m["left"] >= 0 and m["right"] <= m["vw"] + 0.5, f"pill off-screen horizontally: {m}"
    assert m["hittable"], f"the ↓ Latest pill is covered at its centre by {m['hitBy']!r}: {m}"
    assert m["appOverflowY"] == "hidden", f".app stays scrollable behind the reader: {m}"
    pill.click()
    expect(page.locator("#lifeOsViewerList .tr-turn").last).to_be_in_viewport()

    page.locator("#lifeOsViewerClose").click()
    _close_sheet(page)
    expect(tabs).to_be_visible()


def _open_viewer_from_sheet(page: Page) -> Locator:
    page.locator("#lifeOsConvoSheet .lifeos-convo-read").click()
    expect(page.locator("#lifeOsConvoViewer")).to_be_visible(timeout=5_000)
    return page.locator("#lifeOsViewerList")


def test_unparseable_capture_falls_back_to_the_raw_view(
    authed_page: Page, base_url: str
) -> None:
    """#1119: a capture the parser can make nothing of says so in one line and
    offers the raw file — never a blank pane, and never an empty conversation
    passed off as a read one."""
    _mock_skills(authed_page)
    _mock_conversations(authed_page)
    _mock_transcript(authed_page, {
        "path": _FAKE_TRANSCRIPT["path"], "name": _FAKE_TRANSCRIPT["name"],
        "available": False, "reason": "no_turns", "agent": "", "entries": [],
    })
    _open_conversations(authed_page, base_url)

    viewer_list = _open_viewer(authed_page, 0)
    expect(viewer_list.locator(".tr-turn")).to_have_count(0)
    state = authed_page.locator("#lifeOsViewerState")
    expect(state).to_be_visible()
    expect(state).to_contain_text("no turns this view can read")
    # Passive status belongs beside the surface, not in a toast.
    expect(authed_page.locator("#toast")).to_be_hidden()
    state.locator(".lifeos-viewer-raw").click()
    expect(authed_page.locator("#lifeOsDoc")).to_be_visible()


@pytest.mark.iphone
@pytest.mark.parametrize("source,target", [("claude", "codex:gpt-6-astra"), ("codex", "claude:opus")])
def test_history_source_resume_and_explicit_new_handoff(authed_page: Page, base_url: str, source: str, target: str, browser_name: str) -> None:
    """Source-aware actions in the sheet (#1504); handoff is explicit.

    Resume is the primary whatever the toolbar's model (the old "select a
    model from the source harness" lockout is gone, #1503 keeps the
    conversation's own model). "Start new in <other>" is a link row that
    shows only while the toolbar names the other harness and the capture can
    seed one, and it takes the toolbar's model."""
    page = authed_page
    _mock_skills(page)
    row = dict(_FAKE_CONVERSATIONS["conversations"][0], agent=source,
               handoff_truncated=True, handoff_limit=24000)
    unknown = dict(_FAKE_CONVERSATIONS["conversations"][1], agent="pi",
                   resume_reason="Native resume is not verified for this source harness.")
    unavailable = dict(unknown, topic="Unavailable model source", agent="codex",
                       resume_reason="Codex CLI is unavailable on this computer.")
    _mock_conversations(page, dict(_FAKE_CONVERSATIONS, conversations=[row, unknown, unavailable]))
    launches = []
    def launch(route):
        launches.append(_json.loads(route.request.post_data))
        route.fulfill(status=200, content_type="application/json", body=_json.dumps({
            "session": {"session_id": "synthetic", "kind": "remote"}, "handoff_truncated": True}))
    page.route(re.compile(r".*/api/life-os/skills/journal-daily/conversations/launch$"), launch)
    _open_life(page, base_url)
    life_launch_switch(page, "#lifeOsDetached")
    source_choice = "codex:gpt-6-astra" if source == "codex" else "claude:opus"
    _pick_life_model(page, source_choice)
    _skill_menu_item(page, "journal-daily", ".lifeos-convo-btn").click()
    resume = page.locator("#lifeOsConvoSheetResume")
    note = page.locator("#lifeOsConvoSheetNote")
    handoff = page.locator("#lifeOsConvoSheet .lifeos-convo-handoff")
    # Matching provider: Resume is live, no handoff, and the meta names the source.
    _open_sheet(page, 0)
    expect(resume).to_be_enabled()
    expect(resume).to_have_text("Resume")
    expect(note).to_be_hidden()
    expect(handoff).to_be_hidden()
    expect(page.locator("#lifeOsConvoSheetMeta")).to_contain_text("from " + source.capitalize())
    _close_sheet(page)
    # The other two rows carry their own reasons, each said in its sheet.
    for index, fragment in ((1, "Native resume is not verified"),
                            (2, "CLI is unavailable")):
        _open_sheet(page, index)
        expect(note).to_contain_text(fragment)
        expect(resume).to_be_disabled()
        _close_sheet(page)

    # Other provider: Resume still resumes, and the handoff row appears.
    _pick_life_model(page, target)
    _open_sheet(page, 0)
    target_name = "Codex" if target.startswith("codex") else "Claude"
    expect(resume).to_be_enabled()
    expect(resume).to_have_text("Resume")
    expect(handoff).to_be_visible()
    expect(handoff).to_have_text("Start new in " + target_name)
    # Both projections and themes remain legible and inside the viewport.
    for theme in ("light", "dark"):
        page.evaluate("theme => document.documentElement.dataset.theme = theme", theme)
        expect(handoff).to_be_visible()
        assert page.locator("#lifeOsConvoSheet").evaluate("el => el.scrollWidth <= el.clientWidth")
    # The handoff asks first, in the confirm sheet stacked over this one
    # (#1439, never a native confirm()), with a primary action: it starts
    # something, deletes nothing.
    native: list = []
    page.on("dialog", lambda d: (native.append(d.message), d.dismiss()))
    handoff.click()
    expect(page.locator("#confirmDialogTitle")).to_contain_text("new conversation")
    expect(page.locator("#confirmDialogMessage")).to_contain_text(re.compile(r"24[,.]000"))
    expect(page.locator("#confirmDialogOk")).to_have_class(re.compile(r"\bbutton-primary\b"))
    page.locator("#confirmDialogClose").click()
    flush_requests(page)
    assert not launches
    handoff.click()
    page.locator("#confirmDialogOk").click()
    expect(page.locator("#lifeOsConvoSheet")).to_be_hidden()
    assert len(launches) == 1
    payload = launches[0]
    assert payload["action"] == "handoff" and payload["confirm_new"] is True
    assert payload["model"] == target and payload["mode"] == "remote"
    assert payload["capture"] == {key: row[key] for key in ("path", "revision", "agent", "sid")}
    assert "resume_sid" not in payload and "keep_model" not in payload
    assert native == [], native


def test_life_os_skill_can_be_pinned_and_sorts_to_the_top(
    authed_page: Page, base_url: str
) -> None:
    """#1070's favorites, as Pin to top since #1503 (decision 5).

    "Pin to top" is in the row's kebab (the star is gone: the avatar is the
    row's one leading slot); pinned skills sort above the rest. The state is
    server-side — `life_os_favorites` in the webapp config, the key #1070
    starred into, so an existing star is a pin with no migration — so it
    survives a re-render and a reload, which is what makes it worth a round
    trip at all.

    The skills list is route-mocked (the dev box's real life-os checkout is
    not a fixture), so the mock is what flips: this asserts the ordering and
    the POST the star sends, and the *persistence* half is pinned in the
    non-browser suite against the real endpoint (tests/test_webapp_api_life_os_favorites.py).
    """
    posted: list = []

    def _favorites(route):
        posted.append(route.request.post_data_json)
        route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"ok": True, "life_os_favorites": ["sparring-work"]}),
        )

    # After the POST, the list comes back with sparring-work starred — what
    # the real endpoint would produce. Keyed on the POST having happened,
    # not on a call count: the tab fetches the skills list more than once
    # (tab open plus its own refresh), so a "second call" switch flips
    # before the star is ever tapped and the pre-tap ordering assertion
    # below reads the post-tap payload.
    starred = _json.loads(_json.dumps(_FAKE_SKILLS))
    for sk in starred["skills"]:
        sk["is_favorite"] = sk["id"] == "sparring-work"

    def _skills(route):
        body = starred if posted else _FAKE_SKILLS
        route.fulfill(
            status=200, content_type="application/json", body=_json.dumps(body)
        )

    authed_page.route(re.compile(r".*/api/life-os/skills(\?.*)?$"), _skills)
    authed_page.route(re.compile(r".*/api/life-os/favorites$"), _favorites)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    authed_page.locator("#tabLifeOS").click()

    rows = authed_page.locator("#lifeOsList li.lifeos-item")
    expect(rows.first).to_be_visible(timeout=5_000)
    # Alphabetical to start (nothing pinned or used), and no star anywhere.
    assert _skill_order(authed_page) == ["journal-daily", "sparring-work"]
    expect(authed_page.locator("#lifeOsList .star-btn")).to_have_count(0)
    pin = _skill_menu_item(authed_page, "sparring-work", ".lifeos-pin-btn")
    expect(pin).to_have_text("Pin to top")

    pin.click()
    # Sorted to the top, and marked pinned, from the re-fetched payload.
    expect(
        authed_page.locator("#lifeOsList li.lifeos-item").first
    ).to_have_attribute("data-id", "sparring-work", timeout=5_000)
    assert _skill_order(authed_page) == ["sparring-work", "journal-daily"]
    expect(
        authed_page.locator("#lifeOsList li[data-id='sparring-work']")
    ).to_have_attribute("data-pinned", "true")
    expect(_skill_menu_item(authed_page, "sparring-work", ".lifeos-pin-btn")).to_have_text("Unpin")

    assert posted == [{"id": "sparring-work", "favorite": True}], posted


def _skill_menu_item(page: Page, skill_id: str, item: str):
    """Open a skill row's ⋯ menu and return one of its items (#1128: Read
    and Conversations moved off the row into its one kebab)."""
    row = page.locator(f"#lifeOsList li.lifeos-item[data-id='{skill_id}']")
    row.locator(".action-row-kebab").click()
    return row.locator(item)


def _skill_order(page: Page) -> list:
    return page.locator("#lifeOsList li.lifeos-item").evaluate_all(
        "els => els.map(e => e.getAttribute('data-id'))"
    )


# ---- #1036: both launch routes are passkey-gated, so the client must send
# X-Terminal-Token. Loopback and the e2e autoboot bypass the server gate, so a
# missing header would pass every server-facing test here — this pins the
# client half directly (the #997 failure mode) by making ensureTerminalToken()
# believe the gate is configured and the connection is not loopback, seeding a
# cached token, and asserting it rides the launch POST.
_SEEDED_TERMINAL_TOKEN = "synthetic-terminal-token"


def _pretend_passkey_gate(page: Page) -> None:
    page.add_init_script(
        "localStorage.setItem('launcher.tt', %s);"
        "localStorage.setItem('launcher.tt.exp', String(Date.now() + 3600000));"
        % _json.dumps(_SEEDED_TERMINAL_TOKEN)
    )
    page.route(
        re.compile(r".*/api/webauthn/status$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"configured": True, "credentials": 1}),
        ),
    )

    def _status_over_tailnet(route):
        body = route.fetch().json()
        body["terminal"] = {"reachable": True, "reason": "tailnet"}
        route.fulfill(
            status=200, content_type="application/json", body=_json.dumps(body)
        )

    page.route(re.compile(r".*/api/status$"), _status_over_tailnet)


@pytest.mark.parametrize("target", ["skill", "recap"])
def test_life_os_launch_sends_terminal_token(
    authed_page: Page, base_url: str, target: str
) -> None:
    """Regression for #1036: tapping a Life OS launch (a skill tile's 🚀 or
    the weekly-recap 🚀) sends the passkey terminal token, as the Board's
    issue-start does. Without it a phone behind a
    configured WebAuthn gate gets a 401 and the login overlay (cf. #997).
    The real check is still a tap on a phone with the gate configured."""
    _mock_skills(authed_page)
    _mock_recap(authed_page, staleness="fresh", age_days=1.0)
    _pretend_passkey_gate(authed_page)

    captured: dict = {}

    def _capture(route):
        captured["headers"] = route.request.headers
        route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({
                "launched": "x", "name": "x",
                "session": {"session_id": "x", "kind": "remote"},
            }),
        )

    path = (
        r".*/api/life-os/skills/journal-daily/launch$" if target == "skill"
        else r".*/api/life-os/recap/launch$"
    )
    authed_page.route(re.compile(path), _capture)

    with authed_page.expect_response(re.compile(r".*/api/webauthn/status$")):
        authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    authed_page.locator("#tabLifeOS").click()
    expect(authed_page.locator("#lifeOsList li.lifeos-item").first).to_be_visible(
        timeout=5_000
    )
    # Detached → remote, so the mocked response opens no terminal overlay.
    life_launch_switch(authed_page, "#lifeOsDetached")
    if target == "skill":
        authed_page.locator(
            "#lifeOsList li.lifeos-item[data-id='journal-daily'] .action-row-main"
        ).click()
    else:
        expect(authed_page.locator("#lifeOsRecap")).to_be_visible()
        authed_page.locator("#lifeOsRecapLaunch").click()

    wait_until(authed_page, lambda: "headers" in captured, f"the {target} launch POST")
    assert "headers" in captured, f"{target} launch POST was never intercepted"
    assert captured["headers"].get("x-terminal-token") == _SEEDED_TERMINAL_TOKEN, (
        f"{target} launch sent no X-Terminal-Token — behind a configured "
        "passkey gate this is a 401 + login overlay on the phone (#1036, #997)"
    )


# ------------------------------------------------ Telegram channels (#1366)
_FAKE_SETUP = {
    "file_present": True, "life_os_found": True, "bun": True, "plugin": True,
    "skills": [
        {"id": "journal-daily", "name": "Journal Daily"},
        {"id": "sparring-work", "name": "Sparring Work"},
    ],
}


def _mock_channels(page: Page, body: dict, launches: list) -> None:
    page.route(
        re.compile(r".*/api/life-os/channels$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=_json.dumps(body),
        ),
    )

    def _launch(route):
        launches.append({
            "url": route.request.url,
            "body": _json.loads(route.request.post_data or "{}"),
        })
        route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({
                "launched": "journal-daily", "profile": "health",
                "session": {"session_id": "s-health", "kind": "remote"},
            }),
        )

    page.route(re.compile(r".*/api/life-os/channels/[^/]+/launch$"), _launch)


def test_settings_channels_card_reports_each_profile(
    authed_page: Page, base_url: str
) -> None:
    """With profiles, the Settings sheet lists each one as Ready or Needs setup
    and names what is missing; Re-check re-reads the server (#1369). Since
    #1439 it is the only place a broken profile or a problem is reported."""
    _mock_skills(authed_page)
    body = {
        "profiles": [
            {"id": "health", "label": "Health", "skill": "journal-daily",
             "skill_found": True, "env_present": True, "running": False,
             "session_id": ""},
            {"id": "school", "label": "School", "skill": "nope",
             "skill_found": False, "env_present": False, "running": False,
             "session_id": ""},
        ],
        "problems": ["profile 'house': state_dir does not exist"],
        "setup": _FAKE_SETUP,
    }
    _mock_channels(authed_page, body, [])
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    authed_page.locator("#tabLifeOS").click()
    expect(
        authed_page.locator("#lifeOsList li[data-id='journal-daily'] .action-row-meta")
    ).to_contain_text("Telegram", timeout=5_000)
    # The broken profile and the problem are Settings' alone (#1439).
    expect(authed_page.locator("#paneLifeOS")).not_to_contain_text("School")
    expect(authed_page.locator("#paneLifeOS")).not_to_contain_text("house")
    authed_page.locator("#paneLifeOS > .home-head .settings-open-btn").click()
    expect(authed_page.locator("#paneSettings")).to_be_visible()
    # Closed by default, like its sibling Settings sheets: not open until its
    # row is tapped.
    expect(authed_page.locator("#channelsSheet")).not_to_have_attribute("open", "")
    open_settings_sheet(authed_page, "channelsSheet")
    rows = authed_page.locator("#channelProfileChecks .channel-check")
    expect(rows).to_have_count(2)
    expect(rows.nth(0).locator(".channel-check-chip")).to_have_text("Ready")
    expect(rows.nth(1).locator(".channel-check-chip")).to_have_text("Needs setup")
    expect(rows.nth(1)).to_contain_text("skill “nope” not found")
    expect(rows.nth(1)).to_contain_text("no .env")
    expect(authed_page.locator("#channelProfileProblems")).to_contain_text("house")
    # Names and booleans only: no drive path, no token line anywhere in the card.
    expect(authed_page.locator("#channelsSheet")).not_to_contain_text(
        re.compile(r"[A-Za-z]:[\\/]|TELEGRAM_BOT_TOKEN=\d")
    )
    body["profiles"][1].update({"skill_found": True, "env_present": True})
    authed_page.locator("#channelRecheck").click()
    expect(rows.nth(1).locator(".channel-check-chip")).to_have_text("Ready")


def test_channel_session_carries_its_label_in_the_coding_list(
    authed_page: Page, base_url: str
) -> None:
    """A telegram:<profile> session reads "Telegram · Health" on its Coding
    row, so a chat-bound session is recognisable at a glance (#1366)."""
    session = {
        "session_id": "s-tg", "kind": "pty", "agent": "claude",
        "label": "telegram:health", "project_dir": "E:/automation/life-os",
        "name": "journal-daily", "flags": "", "started_at": "2026-07-19T06:30:00Z",
        "alive": True, "rows": 40, "cols": 120, "live_title": "",
        "prompt_title": "", "manual_title": "", "output_chars": 10,
    }
    authed_page.route(
        re.compile(r".*/api/claude-code/sessions$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"sessions": [session]}),
        ),
    )
    # Channel sessions are hidden from the list by default (#1384); this test is
    # about the row they wear when shown, so the setting is off here.
    def _show_channels(route):
        resp = route.fetch()
        body = resp.json()
        body["hide_channel_sessions"] = False
        route.fulfill(response=resp, json=body)

    authed_page.route(re.compile(r".*/api/config$"), _show_channels)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    tag = authed_page.locator(".session-item .session-channel-tag")
    expect(tag).to_have_text("Telegram · Health", timeout=10_000)
