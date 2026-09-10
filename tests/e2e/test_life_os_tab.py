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
from playwright.sync_api import Page, expect

from tests.e2e.conftest import stable_read

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
    # Skills card's summary (#496; provider parity in #845).
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

    # The model dropdown + both toggles render inside the Skills <details>
    # summary.
    summary = authed_page.locator("details.lifeos-list-card summary")
    for cid in ("#lifeOsModelCombo", "#lifeOsDetached", "#lifeOsResume"):
        expect(summary.locator(cid)).to_be_visible()

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


def test_life_os_launch_posts_mode_and_model(
    authed_page: Page, base_url: str
) -> None:
    """#540/#845: launch carries the provider-qualified model choice."""
    _mock_skills(authed_page)

    captured: dict = {}

    def _capture_launch(route):
        captured["body"] = route.request.post_data or ""
        route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({
                "launched": "journal-daily", "name": "journal-daily",
                "agent": "claude", "mode": "remote", "model": "fable",
                "session": {"session_id": "x", "kind": "remote"},
            }),
        )

    authed_page.route(
        re.compile(r".*/api/life-os/skills/journal-daily/launch$"),
        _capture_launch,
    )

    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    authed_page.locator("#tabLifeOS").click()
    expect(authed_page.locator("#lifeOsList li.lifeos-item").first).to_be_visible(
        timeout=5_000
    )

    # Pick a non-default model + Detached on (so it launches detached → no
    # terminal overlay / WS to deal with in the assertion).
    authed_page.locator("#lifeOsModelBtn").click()
    authed_page.locator(
        "#lifeOsModelMenu button[data-value='codex:gpt-6-astra']"
    ).click()
    authed_page.locator("#lifeOsDetached").click()

    tile = authed_page.locator(
        "#lifeOsList li.lifeos-item[data-id='journal-daily']"
    )
    tile.locator(".lifeos-launch").click()

    # Wait for the launch route to capture the POST body.
    authed_page.wait_for_timeout(400)
    assert "body" in captured, "launch POST was never intercepted"
    payload = _json.loads(captured["body"])
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
    _mock_skills(authed_page)

    captured: dict = {}

    def _capture_launch(route):
        captured["body"] = route.request.post_data or ""
        # Answer with kind=remote so the client skips opening the terminal
        # overlay against the fake sid — only the request payload matters.
        route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({
                "launched": "journal-daily", "name": "journal-daily",
                "agent": "claude", "mode": "pty", "model": "sonnet",
                "session": {"session_id": "x", "kind": "remote"},
            }),
        )

    authed_page.route(
        re.compile(r".*/api/life-os/skills/journal-daily/launch$"),
        _capture_launch,
    )

    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    authed_page.locator("#tabLifeOS").click()
    expect(authed_page.locator("#lifeOsList li.lifeos-item").first).to_be_visible(
        timeout=5_000
    )

    # Detached stays OFF — this is the streamed pty path #374 is about.
    tile = authed_page.locator(
        "#lifeOsList li.lifeos-item[data-id='journal-daily']"
    )
    tile.locator(".lifeos-launch").click()

    authed_page.wait_for_timeout(400)
    assert "body" in captured, "launch POST was never intercepted"
    payload = _json.loads(captured["body"])
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
    _mock_skills(authed_page)

    captured: dict = {}

    def _capture_launch(route):
        captured["body"] = route.request.post_data or ""
        route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({
                "launched": "journal-daily", "name": "journal-daily",
                "agent": "claude", "mode": "remote", "model": "sonnet",
                "resume": True,
                "session": {"session_id": "x", "kind": "remote"},
            }),
        )

    authed_page.route(
        re.compile(r".*/api/life-os/skills/journal-daily/launch$"),
        _capture_launch,
    )

    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    authed_page.locator("#tabLifeOS").click()
    expect(authed_page.locator("#lifeOsList li.lifeos-item").first).to_be_visible(
        timeout=5_000
    )

    # Flip Detached + Resume both on. A remote launch has no terminal overlay
    # / WS, so the assertion stays clean.
    authed_page.locator("#lifeOsDetached").click()
    authed_page.locator("#lifeOsResume").click()

    tile = authed_page.locator(
        "#lifeOsList li.lifeos-item[data-id='journal-daily']"
    )
    tile.locator(".lifeos-launch").click()

    authed_page.wait_for_timeout(400)
    assert "body" in captured, "launch POST was never intercepted"
    payload = _json.loads(captured["body"])
    assert payload.get("mode") == "remote", payload
    assert payload.get("resume") is True, payload


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
    name = tile.locator(".coding-name")
    actions = tile.locator(".row-actions.agent-actions")
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
    authed_page.locator(
        "#lifeOsList li.lifeos-item[data-id='journal-daily'] button[title^='Browse']"
    ).click()

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


def _open_conversations(page: Page, base_url: str) -> None:
    """Open the Life OS tab and the per-skill Conversations view."""
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.locator("#tabLifeOS").click()
    expect(page.locator("#lifeOsList li.lifeos-item").first).to_be_visible(
        timeout=5_000
    )
    page.locator(
        "#lifeOsList li.lifeos-item[data-id='journal-daily'] .lifeos-convo-btn"
    ).click()
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


def test_life_os_convos_bar_buttons_match_model_selector(
    authed_page: Page, base_url: str
) -> None:
    """#864: the "‹ Skills" back button and "All skills" toggle used to sit
    taller (44px, font-label) than the model selector (36px, font-caption)
    in the same header row — all three must now share one height/font."""
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


def test_life_os_unresumable_row_says_so(
    authed_page: Page, base_url: str
) -> None:
    """#727: a capture with no stored session id is readable but cannot be
    reopened. A phone has no hover, so the reason is a visible chip rather
    than a disabled button with a tooltip — and roughly a quarter of the
    archive is in this state, so it must not read as breakage."""
    _mock_skills(authed_page)
    _mock_conversations(authed_page)
    _open_conversations(authed_page, base_url)

    rows = authed_page.locator("#lifeOsConvoList .lifeos-convo-row")
    rows.nth(1).locator(".lifeos-convo-head").click()
    detail = rows.nth(1).locator(".lifeos-convo-detail")
    expect(detail).to_be_visible()
    expect(detail.locator(".lifeos-convo-nosession")).to_be_visible()
    expect(detail.locator(".lifeos-convo-resume")).to_have_count(0)

    # The resumable row is the contrast: it offers the action.
    rows.first.locator(".lifeos-convo-head").click()
    expect(
        rows.first.locator(".lifeos-convo-detail .lifeos-convo-resume")
    ).to_be_visible()


def test_life_os_conversation_resume_posts_the_session_id(
    authed_page: Page, base_url: str
) -> None:
    """#727, the point of the whole feature: ↺ on a row posts that exact
    ``resume_sid`` — no native picker — honouring the Skills header's
    Detached toggle and model combo like every other Life OS launch."""
    _mock_skills(authed_page)
    _mock_conversations(authed_page)

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

    authed_page.locator(
        "#lifeOsList li.lifeos-item[data-id='journal-daily'] .lifeos-convo-btn"
    ).click()
    expect(authed_page.locator("#lifeOsConvos")).to_be_visible(timeout=5_000)
    convo_model = authed_page.locator("#lifeOsConvosModelCombo")
    expect(convo_model).to_be_visible()
    convo_model.locator(".model-combo-trigger").click()
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
    option = authed_page.locator(
        "#lifeOsConvosModelMenu button[data-value='claude:opus']"
    )
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
    rows = authed_page.locator("#lifeOsConvoList .lifeos-convo-row")
    rows.first.locator(".lifeos-convo-head").click()
    with authed_page.expect_request(
        re.compile(
            r".*/api/life-os/skills/journal-daily/conversations/launch$"
        )
    ) as request_info:
        rows.first.locator(".lifeos-convo-resume").click()

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


@pytest.mark.parametrize("source,target", [("claude", "codex:gpt-6-astra"), ("codex", "claude:opus")])
def test_history_source_resume_and_explicit_new_handoff(authed_page: Page, base_url: str, source: str, target: str, browser_name: str) -> None:
    """Source-aware actions refresh without collapsing a row; handoff is explicit."""
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
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.locator("#tabLifeOS").click()
    page.locator("#lifeOsDetached").click()
    page.locator("#lifeOsList li.lifeos-item[data-id='journal-daily'] .lifeos-convo-btn").click()
    rows = page.locator("#lifeOsConvoList .lifeos-convo-row")
    rows.first.locator(".lifeos-convo-head").click()
    detail = rows.first.locator(".lifeos-convo-detail")
    source_choice = "codex:gpt-6-astra" if source == "codex" else "claude:opus"
    combo = page.locator("#lifeOsConvosModelCombo")
    combo.locator(".model-combo-trigger").click()
    page.locator(f"#lifeOsConvosModelMenu button[data-value='{source_choice}']").click()
    expect(detail.locator(".lifeos-convo-resume")).to_be_enabled()
    expect(detail).to_contain_text("Source: " + source)
    expect(detail.locator(".lifeos-convo-handoff")).to_have_count(0)
    combo.locator(".model-combo-trigger").click()
    page.locator(f"#lifeOsConvosModelMenu button[data-value='{target}']").click()
    expect(detail).to_be_visible()
    expect(detail.locator(".lifeos-convo-resume")).to_be_disabled()
    expect(detail.locator(".lifeos-convo-handoff")).to_be_visible()
    rows.nth(1).locator(".lifeos-convo-head").click()
    expect(rows.nth(1)).to_contain_text("Native resume is not verified")
    rows.nth(2).locator(".lifeos-convo-head").click()
    expect(rows.nth(2)).to_contain_text("CLI is unavailable")
    # Both projections and themes remain legible and inside the viewport.
    for theme in ("light", "dark"):
        page.evaluate("theme => document.documentElement.dataset.theme = theme", theme)
        expect(detail.locator(".lifeos-convo-handoff")).to_be_visible()
        assert page.locator("#lifeOsConvos").evaluate("el => el.scrollWidth <= el.clientWidth")
    messages = []
    def cancel(dialog):
        messages.append(dialog.message)
        dialog.dismiss()
    page.once("dialog", cancel)
    detail.locator(".lifeos-convo-handoff").click()
    assert not launches
    assert "NEW conversation" in messages[0] and re.search(r"24[,.]000", messages[0])
    page.once("dialog", lambda dialog: dialog.accept())
    detail.locator(".lifeos-convo-handoff").click()
    expect(page.locator("#lifeOsConvos")).to_be_hidden()
    assert len(launches) == 1
    payload = launches[0]
    assert payload["action"] == "handoff" and payload["confirm_new"] is True
    assert payload["model"] == target and payload["mode"] == "remote"
    assert payload["capture"] == {key: row[key] for key in ("path", "revision", "agent", "sid")}
    assert "resume_sid" not in payload
