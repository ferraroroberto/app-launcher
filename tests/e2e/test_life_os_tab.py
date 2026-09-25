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

from tests.e2e._geometry import assert_min_target
from tests.e2e.conftest import stable_read
from tests.e2e.test_jobs_log_copy import _CLIPBOARD_MOCK
from tests.e2e.test_overlay_standalone_scrollable import (
    _PATCH_MATCH_MEDIA,
    _PROJECT_STANDALONE_CSS,
)

pytestmark = pytest.mark.smoke

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


def test_life_os_recap_tile_shows_staleness_badge(
    authed_page: Page, base_url: str
) -> None:
    """Regression for #167: the Weekly-recap tile renders above the skills
    list with a staleness badge whose state class + label track the
    recap-status payload (here overdue, with a draft pending). Hermetic —
    /skills + /recap-status are route-mocked."""
    _mock_skills(authed_page)
    _mock_recap(
        authed_page, staleness="overdue", age_days=20.0, proposal_pending=True
    )
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    authed_page.locator("#tabLifeOS").click()

    recap = authed_page.locator("#lifeOsRecap")
    expect(recap).to_be_visible(timeout=5_000)
    badge = authed_page.locator("#lifeOsRecapBadge")
    expect(badge).to_have_class(re.compile(r"\boverdue\b"))
    expect(badge).to_contain_text("20d ago")
    expect(badge).to_contain_text("overdue")
    expect(badge).to_contain_text("draft ready")


def test_life_os_recap_launch_posts(
    authed_page: Page, base_url: str
) -> None:
    """Tapping 🚀 on the recap tile POSTs /api/life-os/recap/launch with the
    options-card toggle state — proving the /weekly-recap review launch path
    is reached. Detached on so it launches remote (no terminal overlay)."""
    _mock_skills(authed_page)
    _mock_recap(authed_page, staleness="fresh", age_days=1.0)

    captured: dict = {}

    def _capture(route):
        captured["body"] = route.request.post_data or ""
        route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({
                "launched": "weekly-recap", "name": "weekly-recap",
                "agent": "claude", "mode": "remote", "model": "sonnet",
                "session": {"session_id": "r", "kind": "remote"},
            }),
        )

    authed_page.route(
        re.compile(r".*/api/life-os/recap/launch$"), _capture
    )

    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    authed_page.locator("#tabLifeOS").click()
    expect(authed_page.locator("#lifeOsRecap")).to_be_visible(timeout=5_000)

    authed_page.locator("#lifeOsDetached").click()
    authed_page.locator("#lifeOsRecapLaunch").click()

    authed_page.wait_for_timeout(400)
    assert "body" in captured, "recap launch POST was never intercepted"
    payload = _json.loads(captured["body"])
    assert payload["mode"] == "remote", payload
    # No model picked → the combo's Sonnet default rides along (#540).
    assert payload["model"] == "claude:sonnet", payload


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

    # The model dropdown + both toggles render in the Skills card's toolbar
    # (#1132 moved them out of its <summary>).
    toolbar = authed_page.locator("details.lifeos-list-card .card-toolbar")
    for cid in ("#lifeOsModelCombo", "#lifeOsDetached", "#lifeOsResume"):
        expect(toolbar.locator(cid)).to_be_visible()

    # A toggle tap flips the switch but must not collapse the open panel.
    skills_card = authed_page.locator("details.lifeos-list-card")
    assert skills_card.evaluate("el => el.open") is True
    authed_page.locator("#lifeOsDetached").click()
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
        page.locator("#lifeOsDetached").click()
    if resume:
        page.locator("#lifeOsResume").click()

    tile = page.locator("#lifeOsList li.lifeos-item[data-id='journal-daily']")
    tile.locator(".action-row-main").click()

    # Wait for the launch route to capture the POST body.
    page.wait_for_timeout(400)
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


def test_life_os_browser_full_screen_doc_toggle(
    authed_page: Page, base_url: str
) -> None:
    """📖 Browse shows a full-screen file list; tapping a file opens it
    full-screen with a ✕ close-doc button that's hidden until then, and ✕
    returns to the list. Hermetic — /files + /file are route-mocked."""
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
    tile = authed_page.locator(
        "#lifeOsList li.lifeos-item[data-id='journal-daily']"
    )
    expect(tile).to_be_visible(timeout=5_000)
    # Read lives in the row's ⋯ menu since #1128.
    tile.locator(".action-row-kebab").click()
    tile.locator("button[title^='Browse']").click()

    # File list full-screen; content layer + ✕ hidden.
    expect(authed_page.locator("#lifeOsBrowser")).to_be_visible()
    expect(authed_page.locator(".lifeos-file-btn").first).to_be_visible(
        timeout=5_000
    )
    expect(authed_page.locator("#lifeOsFileContent")).to_be_hidden()
    expect(authed_page.locator("#lifeOsDocClose")).to_be_hidden()

    # Open a file → content + ✕ visible.
    authed_page.locator(".lifeos-file-btn").first.click()
    expect(authed_page.locator("#lifeOsFileContent")).to_be_visible()
    expect(authed_page.locator("#lifeOsFileContent")).to_contain_text(
        "body text"
    )
    expect(authed_page.locator("#lifeOsDocClose")).to_be_visible()

    # ✕ closes the doc → back to the list, ✕ hidden again.
    authed_page.locator("#lifeOsDocClose").click()
    expect(authed_page.locator("#lifeOsFileContent")).to_be_hidden()
    expect(authed_page.locator("#lifeOsDocClose")).to_be_hidden()


def test_life_os_delete_conversation_log_from_doc_toolbar(
    authed_page: Page, base_url: str
) -> None:
    """🗑️ never appears in the browse list; it shows in the document toolbar
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
    authed_page.on("dialog", lambda d: d.accept())

    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    authed_page.locator("#tabLifeOS").click()
    _skill_menu_item(authed_page, "journal-daily", ".lifeos-browse-btn").click()

    # No delete control anywhere in the list, and the toolbar 🗑️ stays hidden.
    expect(authed_page.locator(".lifeos-file-btn").first).to_be_visible(
        timeout=5_000
    )
    expect(authed_page.locator(".lifeos-file-del")).to_have_count(0)
    expect(authed_page.locator("#lifeOsDocDelete")).to_be_hidden()

    # Open the memory file → 🗑️ stays hidden (not a conversation log).
    authed_page.locator(
        ".lifeos-file-btn:has-text('observations.md')"
    ).click()
    expect(authed_page.locator("#lifeOsFileContent")).to_be_visible()
    expect(authed_page.locator("#lifeOsDocDelete")).to_be_hidden()
    authed_page.locator("#lifeOsDocClose").click()

    # Open the conversation log → 🗑️ appears in the bar.
    authed_page.locator(
        ".lifeos-file-btn:has-text('trial.md')"
    ).click()
    expect(authed_page.locator("#lifeOsFileContent")).to_be_visible()
    expect(authed_page.locator("#lifeOsDocDelete")).to_be_visible()

    # Confirm delete → DELETE fires, doc closes back to the list, log gone.
    authed_page.locator("#lifeOsDocDelete").click()
    authed_page.wait_for_timeout(400)
    assert deleted["hit"], "DELETE /api/life-os/file was never called"
    expect(authed_page.locator("#lifeOsFileContent")).to_be_hidden()
    expect(authed_page.locator("#lifeOsDocDelete")).to_be_hidden()
    expect(
        authed_page.locator(".lifeos-file-btn:has-text('trial.md')")
    ).to_have_count(0)


# ------------------------------------------------- conversations (issue #727)

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


def _open_convos_model_menu(page: Page, value: str) -> Locator:
    """Open the Conversations model combo once ``value`` is one of its options.

    #935: the options arrive with the boot GET /api/config, and
    ``wireModelCombo``'s ``setOptions`` closes the menu and replaces every
    option. The tab is interactive before that fetch resolves, so on a loaded
    host the test could open the menu first and have it shut underneath it —
    the option click then timed out on "element is not visible". The option
    existing is the real readiness signal: ``setOptions`` runs once at boot, so
    a menu opened after it stays open. ``wait_for`` rather than ``expect``'s 5s
    keeps the suite's default action budget the option click already had.
    """
    option = page.locator(f"#lifeOsConvosModelMenu button[data-value='{value}']")
    option.wait_for(state="attached")
    page.locator("#lifeOsConvosModelCombo .model-combo-trigger").click()
    expect(option).to_be_visible()
    return option


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


def _open_viewer(page: Page, index: int = 0) -> Locator:
    """Expand conversation row ``index`` and open it in the transcript viewer."""
    rows = page.locator("#lifeOsConvoList .lifeos-convo-row")
    rows.nth(index).locator(".lifeos-convo-head").click()
    rows.nth(index).locator(".lifeos-convo-read").click()
    expect(page.locator("#lifeOsConvoViewer")).to_be_visible(timeout=5_000)
    return page.locator("#lifeOsViewerList")


def _open_viewer_menu(page: Page) -> Locator:
    page.locator("#lifeOsViewerMenu").click()
    menu = page.locator("#lifeOsConvoViewer .row-menu")
    expect(menu).to_be_visible()
    return menu


def _open_conversations(page: Page, base_url: str) -> None:
    """Open the Life OS tab and the per-skill Conversations view."""
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.locator("#tabLifeOS").click()
    expect(page.locator("#lifeOsList li.lifeos-item").first).to_be_visible(
        timeout=5_000
    )
    _skill_menu_item(page, "journal-daily", ".lifeos-convo-btn").click()
    expect(page.locator("#lifeOsConvos")).to_be_visible(timeout=5_000)


def test_life_os_conversations_open_from_tile(
    authed_page: Page, base_url: str
) -> None:
    """#727: a tile's 🕘 opens that skill's digested conversation index —
    newest-first, each row showing its date and topic, expandable to the
    decisions / open loops the digest recorded."""
    _mock_skills(authed_page)
    _mock_conversations(authed_page)
    _open_conversations(authed_page, base_url)

    rows = authed_page.locator("#lifeOsConvoList .lifeos-convo-row")
    expect(rows).to_have_count(2)
    expect(rows.first.locator(".lifeos-convo-topic")).to_have_text(
        "booking the ferry"
    )
    expect(rows.first.locator(".lifeos-convo-when")).to_have_text("2026-08-01")

    # Collapsed by default; tapping the row reveals the digest + actions.
    detail = rows.first.locator(".lifeos-convo-detail")
    expect(detail).to_be_hidden()
    rows.first.locator(".lifeos-convo-head").click()
    expect(detail).to_be_visible()
    expect(detail).to_contain_text("confirm the return leg")


@pytest.mark.iphone
def test_life_os_conversation_sort_toggle(
    authed_page: Page, base_url: str
) -> None:
    """#886: the conversations list orders by last interaction by default,
    flips to creation-date order on one tap, shows both dates per row, and
    remembers the choice — all without a second fetch.

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

    rows = authed_page.locator("#lifeOsConvoList .lifeos-convo-row")
    expect(rows).to_have_count(2)
    sort = authed_page.locator("#lifeOsConvosSort")
    expect(sort).to_contain_text("Recent")

    # Default: most recently interacted with first — the resumed trial run,
    # even though it was created two months before the ferry booking.
    expect(rows.first.locator(".lifeos-convo-topic")).to_have_text(
        "an early trial run"
    )
    # Both dates, stacked: the active sort's on top, the other beneath it.
    expect(rows.first.locator(".lifeos-convo-when-primary")).to_have_text(
        "2026-09-05"
    )
    expect(rows.first.locator(".lifeos-convo-when-alt")).to_have_text("2026-06-01")
    # A capture never resumed has one date, not the same day printed twice.
    expect(rows.nth(1).locator(".lifeos-convo-when-primary")).to_have_text(
        "2026-08-01"
    )
    expect(rows.nth(1).locator(".lifeos-convo-when-alt")).to_have_count(0)

    # A fourth control overflows the 430px bar. The row is a horizontal
    # scroll container (#514), but two flex defaults made it deform instead:
    # the title (the only `min-width: 0` item) collapsed to one letter, and
    # the buttons shrank below their text and wrapped. Pin both — the new
    # control has to be reachable, and the skill name still readable.
    box = stable_read(lambda: sort.bounding_box())
    width = stable_read(
        lambda: authed_page.evaluate("window.innerWidth")
    )
    assert box["x"] >= 0 and box["x"] + box["width"] <= width + 1, (
        f"sort toggle is clipped by the bar: {box} in a {width}px viewport"
    )
    title = stable_read(
        lambda: authed_page.locator("#lifeOsConvosTitle").bounding_box()
    )
    assert title["width"] >= 70, f"skill name collapsed to {title['width']}px"
    assert box["height"] <= 40, f"sort toggle wrapped to {box['height']}px"

    before = fetches["n"]
    sort.click()
    expect(sort).to_contain_text("Created")
    expect(rows.first.locator(".lifeos-convo-topic")).to_have_text(
        "booking the ferry"
    )
    expect(rows.nth(1).locator(".lifeos-convo-when-primary")).to_have_text(
        "2026-06-01"                     # creation now on top for the trial
    )
    assert fetches["n"] == before, "re-sorting refetched the list"

    # The choice sticks across a reload of the whole tab.
    _open_conversations(authed_page, base_url)
    expect(authed_page.locator("#lifeOsConvosSort")).to_contain_text("Created")
    expect(
        authed_page.locator("#lifeOsConvoList .lifeos-convo-row")
        .first.locator(".lifeos-convo-topic")
    ).to_have_text("booking the ferry")


def test_life_os_conversations_empty_state_when_no_index(
    authed_page: Page, base_url: str
) -> None:
    """A skill the indexer hasn't digested yet gets an honest empty state —
    not a blank pane and not an error toast."""
    _mock_skills(authed_page)
    _mock_conversations(
        authed_page,
        {"skill": "journal-daily", "available": False, "conversations": []},
    )
    _open_conversations(authed_page, base_url)

    expect(authed_page.locator("#lifeOsConvoState")).to_be_visible()
    expect(authed_page.locator("#lifeOsConvoState")).to_contain_text(
        "No conversation index yet"
    )
    expect(authed_page.locator("#lifeOsConvoList .lifeos-convo-row")).to_have_count(0)


@pytest.mark.iphone
def test_life_os_convos_bar_buttons_match_model_selector(
    authed_page: Page, base_url: str
) -> None:
    """#864: the "‹ Skills" back button and "All skills" toggle used to sit
    taller (44px, font-label) than the model selector (36px, font-caption)
    in the same header row — all three must now share one height/font.

    And each is still a 44px target: the rendered design review measured the
    bar's text buttons at 73x36 and 83x36 (TOUCH-01), so they now carry the
    combo's vertical-only expansion, and the sort button is held to it too."""
    _mock_skills(authed_page)
    _mock_conversations(authed_page)
    _open_conversations(authed_page, base_url)

    back = authed_page.locator("#lifeOsConvosBack")
    scope = authed_page.locator("#lifeOsConvosScope")
    combo = authed_page.locator("#lifeOsConvosModelCombo .model-combo-trigger")
    expect(scope).to_be_visible()  # opened scoped from a tile, so the toggle shows
    for locator in (back, scope, combo):
        expect(locator).to_have_css("height", "36px")
        expect(locator).to_have_css("font-size", "12.48px")
    sort = authed_page.locator("#lifeOsConvosSort")
    expect(sort).to_be_visible()
    for locator in (back, scope, sort, combo):
        assert_min_target(locator)


def test_life_os_unresumable_row_says_so(
    authed_page: Page, base_url: str
) -> None:
    """#727, re-homed in the viewer (#1119): a capture with no stored session
    id is readable but cannot be reopened. A phone has no hover, so the reason
    is a visible note under the bar rather than only a disabled menu row's
    tooltip — and roughly a quarter of the archive is in this state, so it
    must not read as breakage."""
    _mock_skills(authed_page)
    _mock_conversations(authed_page)
    _mock_transcript(authed_page)
    _open_conversations(authed_page, base_url)

    _open_viewer(authed_page, 1)
    note = authed_page.locator("#lifeOsViewerNote")
    expect(note).to_be_visible()
    expect(note).to_contain_text("readable only")
    _open_viewer_menu(authed_page)
    expect(authed_page.locator(".lifeos-viewer-resume")).to_have_count(0)
    authed_page.keyboard.press("Escape")
    authed_page.locator("#lifeOsViewerBack").click()

    # The resumable row is the contrast: it offers the action, and says
    # nothing about why it could not.
    _open_viewer(authed_page, 0)
    expect(authed_page.locator("#lifeOsViewerNote")).to_be_hidden()
    _open_viewer_menu(authed_page)
    expect(authed_page.locator(".lifeos-viewer-resume")).to_be_enabled()


def test_life_os_viewer_menu_shows_resume_for_a_resumable_capture(
    authed_page: Page, base_url: str
) -> None:
    """#1137: the viewer's ⋮ menu drops hidden rows from the DOM on every
    open, so ``to_be_enabled`` alone would pass on a row that is attached but
    never painted. With a matching model the Resume row must be on screen,
    enabled and named for its provider."""
    _mock_skills(authed_page)
    _mock_conversations(authed_page)
    _mock_transcript(authed_page)
    authed_page.add_init_script(_CLIPBOARD_MOCK)
    _open_conversations(authed_page, base_url)
    _open_convos_model_menu(authed_page, "claude:opus").click()

    _open_viewer(authed_page, 0)
    menu = _open_viewer_menu(authed_page)
    resume = menu.locator(".lifeos-viewer-resume")
    expect(resume).to_be_visible()
    expect(resume).to_be_enabled()
    expect(resume).to_contain_text("Resume in Claude")
    # #1170: Copy link sits with the safe actions; destructive Delete stays last.
    expect(menu.locator(".row-menu-btn").last).to_have_class(
        re.compile(r"\blifeos-viewer-delete\b"))
    menu.locator(".lifeos-viewer-copy-link").click()
    authed_page.wait_for_function(
        "() => Array.isArray(window.__copied) && window.__copied.length > 0")
    assert authed_page.evaluate("() => window.__copied[0]") == (
        f"{base_url}/?convo=journal-daily/2026-08-01-0900-ferry-booking.md")


def test_life_os_convo_link_opens_that_conversation(
    authed_page: Page, base_url: str
) -> None:
    """#1170: a copied ``?convo=<skill>/<file>`` link lands on the Life OS tab
    with that capture open in the viewer, and strips the param. A link that no
    longer resolves says so in the Conversations overlay — never a blank
    pane.

    #1222: the link must not wait on the rest of boot. The git-status fan-out
    that ran ahead of it took 1–7 s on an idle box and timed the link out
    under load, so it is held pending here for the whole test — the viewer
    has to open with it still in flight."""
    _mock_skills(authed_page)
    _mock_conversations(authed_page)
    _mock_transcript(authed_page)
    authed_page.route(re.compile(r".*/api/claude-code/git-status$"),
                      lambda route: None)
    authed_page.goto(
        f"{base_url}/?convo=journal-daily/2026-08-01-0900-ferry-booking.md",
        wait_until="domcontentloaded")
    expect(authed_page.locator("#lifeOsConvoViewer")).to_be_visible(timeout=10_000)
    expect(authed_page.locator("#lifeOsViewerTitle")).to_have_text("booking the ferry")
    expect(authed_page.locator("#tabLifeOS")).to_have_attribute("aria-selected", "true")
    assert "convo=" not in authed_page.url

    authed_page.goto(f"{base_url}/?convo=journal-daily/2026-01-01-gone.md",
                     wait_until="domcontentloaded")
    expect(authed_page.locator("#lifeOsConvoState")).to_contain_text(
        "no longer matches", timeout=10_000)
    expect(authed_page.locator("#lifeOsConvoViewer")).to_be_hidden()

    authed_page.goto(f"{base_url}/?convo=no-such-skill/x.md",
                     wait_until="domcontentloaded")
    expect(authed_page.locator("#lifeOsConvoState")).to_contain_text(
        "no-such-skill", timeout=10_000)


@pytest.mark.iphone
def test_life_os_row_leads_with_resume(
    authed_page: Page, base_url: str
) -> None:
    """#1137: after #1119 a row offered only Read, burying Resume — the
    action nearly every capture is opened for — two taps deep in the viewer.
    The row leads with it again, follows the model combo in place (without
    collapsing the row), and posts exactly what the viewer's Resume posts."""
    _mock_skills(authed_page)
    _mock_conversations(authed_page)
    _mock_transcript(authed_page)
    authed_page.add_init_script(_CLIPBOARD_MOCK)
    launches = []

    def _launch(route):
        launches.append(_json.loads(route.request.post_data or ""))
        route.fulfill(status=200, content_type="application/json", body=_json.dumps(
            {"session": {"session_id": "synthetic", "kind": "remote"}}))

    authed_page.route(
        re.compile(r".*/api/life-os/skills/journal-daily/conversations/launch$"),
        _launch,
    )
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    authed_page.locator("#tabLifeOS").click()
    # Detached, so the resume lands in a console, not the terminal overlay.
    authed_page.locator("#lifeOsDetached").click()
    _skill_menu_item(authed_page, "journal-daily", ".lifeos-convo-btn").click()
    expect(authed_page.locator("#lifeOsConvos")).to_be_visible(timeout=5_000)
    _open_convos_model_menu(authed_page, "claude:opus").click()

    rows = authed_page.locator("#lifeOsConvoList .lifeos-convo-row")
    rows.first.locator(".lifeos-convo-head").click()
    actions = rows.first.locator(".lifeos-convo-actions")
    resume = actions.locator(".lifeos-convo-resume")
    expect(resume).to_be_visible()
    expect(resume).to_be_enabled()
    expect(resume).to_contain_text("Resume in Claude")
    expect(actions.locator(".lifeos-convo-read")).to_be_visible()
    # Resume is the primary action: first in the strip.
    expect(actions.locator("button").first).to_have_class(
        re.compile(r"\blifeos-convo-resume\b"))
    expect(actions.locator(".lifeos-convo-nosession")).to_have_count(0)
    expect(actions.locator(".lifeos-convo-handoff")).to_have_count(0)
    # Rename / Delete / Open raw stay in the viewer's ⋮ menu; Copy link (#1170)
    # joins Read on the row.
    expect(actions.locator("button")).to_have_count(3)
    # One row, one size (#1170): Resume stays the tinted primary, Read and
    # Copy link are outlined, and every button is the same 44px tall — Resume
    # used to keep .button-tint's 14px block padding and outgrow Read.
    expect(resume).to_have_class(re.compile(r"\bbutton-tint\b"))
    for cls in (".lifeos-convo-read", ".lifeos-convo-copy-link"):
        expect(actions.locator(cls)).to_have_class(re.compile(r"\bbutton-ghost\b"))
    for i in range(3):
        expect(actions.locator("button").nth(i)).to_have_css("height", "44px")
    # Copy link writes the ?convo= deep link inside the tap (iOS).
    actions.locator(".lifeos-convo-copy-link").click()
    authed_page.wait_for_function(
        "() => Array.isArray(window.__copied) && window.__copied.length > 0")
    assert authed_page.evaluate("() => window.__copied[0]") == (
        f"{base_url}/?convo=journal-daily/2026-08-01-0900-ferry-booking.md")
    expect(authed_page.locator("#toast")).to_contain_text("link copied")

    # Another provider: the open row updates in place — Resume greys out with
    # its reason on screen, and the explicit handoff appears.
    _open_convos_model_menu(authed_page, "codex:gpt-6-astra").click()
    expect(rows.first.locator(".lifeos-convo-detail")).to_be_visible()
    expect(resume).to_be_disabled()
    expect(actions.locator(".lifeos-convo-nosession")).to_contain_text(
        "Select a Claude model")
    expect(actions.locator(".lifeos-convo-handoff")).to_contain_text(
        "Start new in Codex")
    for cls in (".lifeos-convo-resume", ".lifeos-convo-handoff"):
        expect(actions.locator(cls)).to_have_css("height", "44px")

    # An unresumable row says why and offers only Read.
    rows.nth(1).locator(".lifeos-convo-head").click()
    other = rows.nth(1).locator(".lifeos-convo-actions")
    expect(other.locator(".lifeos-convo-nosession")).to_contain_text("readable only")
    expect(other.locator(".lifeos-convo-resume")).to_have_count(0)
    expect(other.locator(".lifeos-convo-read")).to_be_visible()

    # Back on a matching model, the row's Resume posts the viewer's payload.
    _open_convos_model_menu(authed_page, "claude:opus").click()
    resume.click()
    expect(authed_page.locator("#lifeOsConvos")).to_be_hidden()
    assert launches == [{
        "mode": "remote", "model": "claude:opus", "action": "resume",
        "capture": {key: _FAKE_CONVERSATIONS["conversations"][0][key]
                    for key in ("path", "revision", "agent", "sid")},
    }], launches


@pytest.mark.iphone
def test_life_os_conversation_resume_posts_the_session_id(
    authed_page: Page, base_url: str
) -> None:
    """#727, the point of the whole feature: Resume posts that exact
    ``resume_sid`` — no native picker — honouring the Skills header's
    Detached toggle and model combo like every other Life OS launch."""
    _mock_skills(authed_page)
    _mock_conversations(authed_page)
    _mock_transcript(authed_page)

    def _fulfill_launch(route):
        route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({
                "launched": "journal-daily", "name": "journal-daily",
                "agent": "claude", "mode": "remote", "model": "opus",
                "resume": True, "resume_sid": _RESUMABLE_SID,
                "session": {"session_id": "x", "kind": "remote"},
            }),
        )

    authed_page.route(
        re.compile(r".*/api/life-os/skills/journal-daily/conversations/launch$"),
        _fulfill_launch,
    )

    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    authed_page.locator("#tabLifeOS").click()
    expect(authed_page.locator("#lifeOsList li.lifeos-item").first).to_be_visible(
        timeout=5_000
    )
    # Detached on, so the resume lands in a console instead of opening the
    # terminal overlay (nothing to tear down in the assertion).
    authed_page.locator("#lifeOsDetached").click()

    _skill_menu_item(authed_page, "journal-daily", ".lifeos-convo-btn").click()
    expect(authed_page.locator("#lifeOsConvos")).to_be_visible(timeout=5_000)
    expect(authed_page.locator("#lifeOsConvosModelCombo")).to_be_visible()
    option = _open_convos_model_menu(authed_page, "claude:opus")
    menu = authed_page.locator("#lifeOsConvosModelMenu")
    expect(menu).to_have_class(re.compile(r"\bmodel-combo-menu--portal\b"))
    assert menu.evaluate("el => el.parentElement === document.body")
    menu_box = menu.bounding_box()
    assert menu_box is not None
    viewport = authed_page.viewport_size or {"width": 0, "height": 0}
    assert menu_box["x"] >= 0 and menu_box["y"] >= 0
    assert menu_box["x"] + menu_box["width"] <= viewport["width"]
    assert menu_box["y"] + menu_box["height"] <= viewport["height"]
    z_indexes = authed_page.evaluate(
        """() => [
          Number(getComputedStyle(document.getElementById('lifeOsConvosModelMenu')).zIndex),
          Number(getComputedStyle(document.getElementById('lifeOsConvos')).zIndex)
        ]"""
    )
    assert z_indexes[0] > z_indexes[1], z_indexes
    hit_target = option.evaluate(
        """el => {
          const box = el.getBoundingClientRect();
          const hit = document.elementFromPoint(
            box.left + box.width / 2, box.top + box.height / 2
          );
          return !!hit && (hit === el || el.contains(hit));
        }"""
    )
    assert hit_target, "conversation model option is clipped by the toolbar"
    for theme in ("light", "dark"):
        authed_page.evaluate(
            "value => document.documentElement.dataset.theme = value", theme
        )
        expect(option).to_be_visible()
        assert menu.evaluate("el => getComputedStyle(el).backgroundColor") != "rgba(0, 0, 0, 0)"
    option.click()
    expect(authed_page.locator("#lifeOsModelCombo")).to_have_attribute(
        "data-value", "claude:opus"
    )
    _open_viewer(authed_page, 0)
    _open_viewer_menu(authed_page)
    with authed_page.expect_request(
        re.compile(
            r".*/api/life-os/skills/journal-daily/conversations/launch$"
        )
    ) as request_info:
        authed_page.locator(".lifeos-viewer-resume").click()

    payload = _json.loads(request_info.value.post_data or "")
    assert payload == {
        "mode": "remote", "model": "claude:opus", "action": "resume",
        "capture": {key: _FAKE_CONVERSATIONS["conversations"][0][key]
                    for key in ("path", "revision", "agent", "sid")},
    }, payload


def test_life_os_header_search_spans_every_skill(
    authed_page: Page, base_url: str
) -> None:
    """#727: the Skills header's 🔎 opens the same view unscoped, and a query
    returns ranked hits from every skill, each tagged with the skill it came
    from — the case the per-skill index cannot answer."""
    _mock_skills(authed_page)

    seen: dict = {}

    def _capture_search(route):
        seen["url"] = route.request.url
        route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({
                "available": True, "query": "ferry", "skill": "",
                "results": [
                    dict(_FAKE_CONVERSATIONS["conversations"][0]),
                    dict(
                        _FAKE_CONVERSATIONS["conversations"][1],
                        skill="sparring-work",
                        topic="the ferry conversation at work",
                    ),
                ],
            }),
        )

    authed_page.route(
        re.compile(r".*/api/life-os/conversations/search.*"), _capture_search
    )

    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    authed_page.locator("#tabLifeOS").click()
    expect(authed_page.locator("#lifeOsList li.lifeos-item").first).to_be_visible(
        timeout=5_000
    )
    authed_page.locator("#lifeOsConvoSearch").click()
    expect(authed_page.locator("#lifeOsConvos")).to_be_visible(timeout=5_000)
    # Opened unscoped: the scope toggle is meaningless and stays hidden.
    expect(authed_page.locator("#lifeOsConvosScope")).to_be_hidden()

    authed_page.locator("#lifeOsConvoQuery").fill("ferry")
    rows = authed_page.locator("#lifeOsConvoList .lifeos-convo-row")
    expect(rows).to_have_count(2, timeout=5_000)
    # No skill filter in the request, and every row names its own skill.
    assert "skill=" not in seen.get("url", ""), seen
    expect(rows.first.locator(".lifeos-convo-tag")).to_have_text("journal-daily")
    expect(rows.nth(1).locator(".lifeos-convo-tag")).to_have_text("sparring-work")


def test_life_os_search_results_keep_server_relevance_order(
    authed_page: Page, base_url: str
) -> None:
    """#1074: the client used to push search hits through the #886 date sort,
    so the best match sank below rows that merely mention the word.

    The fixture makes the top-ranked hit the *oldest* row on both date axes,
    so a list that re-sorted by either date would render it last — and the
    three orderings disagree pairwise, so each state of the toggle is
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

    authed_page.route(
        re.compile(r".*/api/life-os/conversations/search.*"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({
                "available": True, "query": "ferry", "skill": "",
                "results": [best, mention, passing],   # server's rank order
            }),
        ),
    )

    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    authed_page.locator("#tabLifeOS").click()
    expect(authed_page.locator("#lifeOsList li.lifeos-item").first).to_be_visible(
        timeout=5_000
    )
    authed_page.locator("#lifeOsConvoSearch").click()
    expect(authed_page.locator("#lifeOsConvos")).to_be_visible(timeout=5_000)

    sort = authed_page.locator("#lifeOsConvosSort")
    # Browsing starts on the remembered date sort; a query switches the
    # control to relevance rather than leaving a dead toggle behind.
    expect(sort).to_contain_text("Recent")

    authed_page.locator("#lifeOsConvoQuery").fill("ferry")
    rows = authed_page.locator("#lifeOsConvoList .lifeos-convo-row")
    expect(rows).to_have_count(3, timeout=5_000)
    topics = rows.locator(".lifeos-convo-topic")
    expect(topics).to_have_text(
        ["the ferry deep dive", "a ferry mention", "ferry in passing"]
    )
    expect(sort).to_contain_text("Relevance")

    # A date ordering of the hits is still reachable — the toggle is not a
    # one-way trip into relevance.
    sort.click()
    expect(sort).to_contain_text("Recent")
    expect(topics).to_have_text(
        ["ferry in passing", "a ferry mention", "the ferry deep dive"]
    )
    sort.click()
    expect(sort).to_contain_text("Created")
    expect(topics).to_have_text(
        ["a ferry mention", "ferry in passing", "the ferry deep dive"]
    )

    # …and relevance survives the round trip, because the re-sort runs off
    # the server's array rather than the rendered one.
    sort.click()
    expect(sort).to_contain_text("Relevance")
    expect(topics).to_have_text(
        ["the ferry deep dive", "a ferry mention", "ferry in passing"]
    )

    # Clearing the box drops the transient ordering: browsing keeps its own
    # date default (#886/#890), and no "Relevance" label is left stranded.
    authed_page.locator("#lifeOsConvoQuery").fill("")
    expect(sort).to_contain_text("Recent", timeout=5_000)


def test_life_os_search_unavailable_is_not_an_error(
    authed_page: Page, base_url: str
) -> None:
    """A missing search CLI or database degrades to a stated 'unavailable'
    inside the view — never an error toast, and never a blank list."""
    _mock_skills(authed_page)
    authed_page.route(
        re.compile(r".*/api/life-os/conversations/search.*"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({
                "available": False,
                "reason": "no conversation index has been built yet",
                "results": [],
            }),
        ),
    )

    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    authed_page.locator("#tabLifeOS").click()
    expect(authed_page.locator("#lifeOsList li.lifeos-item").first).to_be_visible(
        timeout=5_000
    )
    authed_page.locator("#lifeOsConvoSearch").click()
    authed_page.locator("#lifeOsConvoQuery").fill("ferry")

    state = authed_page.locator("#lifeOsConvoState")
    expect(state).to_be_visible(timeout=5_000)
    expect(state).to_contain_text("Search unavailable")
    # Passive/background status belongs beside the surface it describes; a
    # toast is for user-initiated command results only.
    expect(authed_page.locator("#toast")).to_be_hidden()


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
    """#1119: the row's book icon opens the capture in the session overlay's
    own Chat renderer — real turn cards, the user's prompt as plain text and
    the reply through markdown — not the raw document viewer. One surface,
    two mounts (#979), so the assertion is on that renderer's own classes.
    #1149: a question card renders here too, and read-only."""
    _mock_skills(authed_page)
    _mock_conversations(authed_page)
    _mock_transcript(authed_page, _QUESTION_TRANSCRIPT)
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
    # Read-only: no composer anywhere in this overlay.
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
    # #1140: a capture that fits on screen has no end to jump to.
    pill = authed_page.locator("#lifeOsViewerLatest")
    expect(pill).to_have_count(1)
    expect(pill).to_be_hidden()
    # The raw viewer is still one tap away, and lands back here on close.
    _open_viewer_menu(authed_page)
    expect(authed_page.locator(".lifeos-viewer-groups")).to_be_disabled()
    authed_page.locator(".lifeos-viewer-open-raw").click()
    expect(authed_page.locator("#lifeOsBrowser")).to_be_visible()
    authed_page.locator("#lifeOsDocClose").click()
    expect(authed_page.locator("#lifeOsBrowser")).to_be_hidden()
    expect(authed_page.locator("#lifeOsConvoViewer")).to_be_visible()
    authed_page.locator("#lifeOsViewerBack").click()
    expect(authed_page.locator("#lifeOsConvos")).to_be_visible()


_LONG_TRANSCRIPT = dict(_FAKE_TRANSCRIPT, entries=[
    {"kind": "user" if i % 2 == 0 else "assistant",
     "text": f"synthetic turn {i} " + "about ferry timetables and ticket prices " * 3,
     "offset": i * 100, "timestamp": None, "truncated": False}
    for i in range(40)
])


def test_viewer_latest_pill_jumps_to_the_last_turn(
    authed_page: Page, base_url: str
) -> None:
    """#1140: the shared ↓ Latest pill on the conversation viewer. A finished
    conversation still opens at its start — it reads top-down — but a long
    one shows the pill straight away, and one tap reaches the last turn."""
    _mock_skills(authed_page)
    _mock_conversations(authed_page)
    _mock_transcript(authed_page, _LONG_TRANSCRIPT)
    _open_conversations(authed_page, base_url)

    turns = _open_viewer(authed_page, 0).locator(".tr-turn")
    expect(turns).to_have_count(40)
    expect(turns.first).to_be_in_viewport()
    pill = authed_page.locator("#lifeOsViewerLatest")
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
    scrollRange: document.scrollingElement.scrollHeight - document.scrollingElement.clientHeight,
    appOverflowY: getComputedStyle(document.querySelector('main.app')).overflowY,
  };
}
"""


@pytest.mark.iphone
def test_viewer_covers_the_tab_bar_in_the_standalone_shell(
    authed_page: Page, base_url: str, browser_name: str
) -> None:
    """#1143: in the installed PWA the floating tab bar sat on top of the
    conversation viewer and hid its ↓ Latest pill. The standalone shell makes
    .app position: fixed, so the list and the viewer (both nested inside
    .app) stack below the body-level bar unless its hide rule names them. The
    viewer only ever opens over the list, so the list's missing entry is what
    showed on the phone. tests/test_overlay_nav_hide.py checks each entry.

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
    expect(page.locator("#lifeOsConvos")).to_be_visible(timeout=5_000)
    expect(tabs).to_be_hidden()

    expect(_open_viewer(page, 0).locator(".tr-turn")).to_have_count(40)
    expect(tabs).to_be_hidden()
    pill = page.locator("#lifeOsViewerLatest")
    expect(pill).to_be_visible()
    m = page.evaluate(_MEASURE_PILL)
    assert m["top"] >= 0 and m["bottom"] <= m["vh"] + 0.5, f"pill off-screen vertically: {m}"
    assert m["left"] >= 0 and m["right"] <= m["vw"] + 0.5, f"pill off-screen horizontally: {m}"
    assert m["hittable"], f"the ↓ Latest pill is covered at its centre by {m['hitBy']!r}: {m}"
    assert m["appOverflowY"] == "hidden", f".app stays scrollable behind the viewer: {m}"
    assert m["scrollRange"] >= 1, (
        f"the document lost its 1px scrollable overflow under the viewer (#1099): {m}"
    )
    pill.click()
    expect(page.locator("#lifeOsViewerList .tr-turn").last).to_be_in_viewport()

    page.locator("#lifeOsViewerBack").click()
    page.locator("#lifeOsConvosBack").click()
    expect(page.locator("#lifeOsConvos")).to_be_hidden()
    expect(tabs).to_be_visible()


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
    expect(authed_page.locator("#lifeOsBrowser")).to_be_visible()


@pytest.mark.iphone
@pytest.mark.parametrize("source,target", [("claude", "codex:gpt-6-astra"), ("codex", "claude:opus")])
def test_history_source_resume_and_explicit_new_handoff(authed_page: Page, base_url: str, source: str, target: str, browser_name: str) -> None:
    """Source-aware actions, now in the viewer menu (#1119); handoff is explicit.

    The menu re-resolves every row on open, so the model chosen in the
    Conversations bar (under the viewer) decides what it offers."""
    page = authed_page
    _mock_skills(page)
    row = dict(_FAKE_CONVERSATIONS["conversations"][0], agent=source,
               handoff_truncated=True, handoff_limit=24000)
    unknown = dict(_FAKE_CONVERSATIONS["conversations"][1], agent="pi",
                   resume_reason="Native resume is not verified for this source harness.")
    unavailable = dict(unknown, topic="Unavailable model source", agent="codex",
                       resume_reason="Codex CLI is unavailable on this computer.")
    _mock_conversations(page, dict(_FAKE_CONVERSATIONS, conversations=[row, unknown, unavailable]))
    _mock_transcript(page)
    launches = []
    def launch(route):
        launches.append(_json.loads(route.request.post_data))
        route.fulfill(status=200, content_type="application/json", body=_json.dumps({
            "session": {"session_id": "synthetic", "kind": "remote"}, "handoff_truncated": True}))
    page.route(re.compile(r".*/api/life-os/skills/journal-daily/conversations/launch$"), launch)
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.locator("#tabLifeOS").click()
    page.locator("#lifeOsDetached").click()
    _skill_menu_item(page, "journal-daily", ".lifeos-convo-btn").click()
    rows = page.locator("#lifeOsConvoList .lifeos-convo-row")
    source_choice = "codex:gpt-6-astra" if source == "codex" else "claude:opus"
    _open_convos_model_menu(page, source_choice).click()
    # Matching provider: Resume is live and there is nothing to hand off to.
    _open_viewer(page, 0)
    _open_viewer_menu(page)
    expect(page.locator(".lifeos-viewer-resume")).to_be_enabled()
    expect(page.locator(".lifeos-viewer-handoff")).to_have_count(0)
    page.keyboard.press("Escape")
    page.locator("#lifeOsViewerBack").click()
    rows.first.locator(".lifeos-convo-head").click()   # collapse it again
    expect(rows.first.locator(".lifeos-convo-detail")).to_contain_text("Source: " + source)
    # The other two rows carry their own reasons, each said in its viewer.
    for index, fragment in ((1, "Native resume is not verified"),
                            (2, "CLI is unavailable")):
        _open_viewer(page, index)
        expect(page.locator("#lifeOsViewerNote")).to_contain_text(fragment)
        page.locator("#lifeOsViewerBack").click()
        rows.nth(index).locator(".lifeos-convo-head").click()

    # Other provider: Resume greys out with its reason, handoff appears.
    _open_convos_model_menu(page, target).click()
    _open_viewer(page, 0)
    expect(page.locator("#lifeOsViewerNote")).to_contain_text("Select a")
    _open_viewer_menu(page)
    expect(page.locator(".lifeos-viewer-resume")).to_be_disabled()
    expect(page.locator(".lifeos-viewer-handoff")).to_be_visible()
    # Both projections and themes remain legible and inside the viewport.
    for theme in ("light", "dark"):
        page.evaluate("theme => document.documentElement.dataset.theme = theme", theme)
        expect(page.locator(".lifeos-viewer-handoff")).to_be_visible()
        assert page.locator("#lifeOsConvoViewer").evaluate("el => el.scrollWidth <= el.clientWidth")
    messages = []
    def cancel(dialog):
        messages.append(dialog.message)
        dialog.dismiss()
    page.once("dialog", cancel)
    page.locator(".lifeos-viewer-handoff").click()
    assert not launches
    assert "NEW conversation" in messages[0] and re.search(r"24[,.]000", messages[0])
    _open_viewer_menu(page)
    page.once("dialog", lambda dialog: dialog.accept())
    page.locator(".lifeos-viewer-handoff").click()
    expect(page.locator("#lifeOsConvoViewer")).to_be_hidden()
    expect(page.locator("#lifeOsConvos")).to_be_hidden()
    assert len(launches) == 1
    payload = launches[0]
    assert payload["action"] == "handoff" and payload["confirm_new"] is True
    assert payload["model"] == target and payload["mode"] == "remote"
    assert payload["capture"] == {key: row[key] for key in ("path", "revision", "agent", "sid")}
    assert "resume_sid" not in payload


def test_life_os_skill_can_be_starred_and_sorts_to_the_top(
    authed_page: Page, base_url: str
) -> None:
    """#1070 — the Coding tab's favorites treatment (#250), on the skills list.

    A skill row carries a star; starred skills sort above the rest; the
    partition is stable, so both groups stay in the scanner's alphabetical
    order rather than reshuffling around a star. The state is server-side —
    `life_os_favorites` in the webapp config — so it survives a re-render
    and a reload, which is what makes it worth a round trip at all.

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
    # Alphabetical to start, nothing starred.
    assert _skill_order(authed_page) == ["journal-daily", "sparring-work"]
    star = authed_page.locator(
        "#lifeOsList li[data-id='sparring-work'] .star-btn"
    )
    expect(star).to_have_count(1)
    expect(star).to_have_attribute("aria-pressed", "false")

    star.click()
    # Sorted to the top, and painted as starred, from the re-fetched payload.
    expect(
        authed_page.locator("#lifeOsList li.lifeos-item").first
    ).to_have_attribute("data-id", "sparring-work", timeout=5_000)
    assert _skill_order(authed_page) == ["sparring-work", "journal-daily"]
    expect(
        authed_page.locator("#lifeOsList li[data-id='sparring-work'] .star-btn")
    ).to_have_attribute("aria-pressed", "true")

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
    authed_page.locator("#lifeOsDetached").click()
    if target == "skill":
        authed_page.locator(
            "#lifeOsList li.lifeos-item[data-id='journal-daily'] .action-row-main"
        ).click()
    else:
        expect(authed_page.locator("#lifeOsRecap")).to_be_visible()
        authed_page.locator("#lifeOsRecapLaunch").click()

    authed_page.wait_for_timeout(400)
    assert "headers" in captured, f"{target} launch POST was never intercepted"
    assert captured["headers"].get("x-terminal-token") == _SEEDED_TERMINAL_TOKEN, (
        f"{target} launch sent no X-Terminal-Token — behind a configured "
        "passkey gate this is a 401 + login overlay on the phone (#1036, #997)"
    )
