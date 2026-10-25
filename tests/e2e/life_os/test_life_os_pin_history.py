"""Life OS tab e2e: source-history hand-off and pinning a skill."""

from __future__ import annotations

import json as _json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import (
    flush_requests,
    life_launch_switch,
)

from tests.e2e.life_os._support import (
    _FAKE_CONVERSATIONS,
    _FAKE_SKILLS,
    _close_sheet,
    _mock_conversations,
    _mock_skills,
    _open_life,
    _open_sheet,
    _pick_life_model,
    _skill_menu_item,
)


pytestmark = pytest.mark.smoke


@pytest.mark.iphone
@pytest.mark.parametrize("source,target", [("claude", "codex:gpt-6-luna"), ("codex", "claude:opus")])
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
    source_choice = "codex:gpt-6-luna" if source == "codex" else "claude:opus"
    _pick_life_model(page, source_choice)
    _skill_menu_item(page, "journal-daily", "[data-item=lifeos-convo-btn]").click()
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
    pin = _skill_menu_item(authed_page, "sparring-work", "[data-item=lifeos-pin-btn]")
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
    expect(_skill_menu_item(authed_page, "sparring-work", "[data-item=lifeos-pin-btn]")).to_have_text("Unpin")

    assert posted == [{"id": "sparring-work", "favorite": True}], posted


def _skill_order(page: Page) -> list:
    return page.locator("#lifeOsList li.lifeos-item").evaluate_all(
        "els => els.map(e => e.getAttribute('data-id'))"
    )
