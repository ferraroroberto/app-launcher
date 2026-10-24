"""Route mocks and openers shared by the Life OS e2e modules."""

from __future__ import annotations

import json as _json
import re

from playwright.sync_api import Locator, Page, expect

from tests.e2e.conftest import (
    open_row_menu,
)


_SEARCH_URL = re.compile(r".*/api/life-os/conversations/search.*")
_ARCHIVED_URL = re.compile(r".*/api/life-os/archived(\?.*)?$")

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
    _skill_menu_item(page, "journal-daily", "[data-item=lifeos-convo-btn]").click()
    expect(page.locator("#lifeOsConvoGroup")).to_be_visible(timeout=5_000)


def _mock_skill_launches(page: Page) -> list:
    """Capture every skill launch POST as (skill id, payload); the response is
    a detached session so no terminal opens against the fake sid."""
    launches: list = []

    def _launch(route):
        skill = route.request.url.split("/api/life-os/skills/")[1].split("/")[0]
        launches.append((skill, _json.loads(route.request.post_data)))
        route.fulfill(status=200, content_type="application/json", body=_json.dumps(
            {"session": {"session_id": "x", "kind": "remote"}}))
    page.route(re.compile(r".*/api/life-os/skills/[^/]+/launch$"), _launch)
    return launches


def _skill_menu_item(page: Page, skill_id: str, item: str):
    """Open a skill row's ⋯ menu and return one of its items (#1128: Read
    and Conversations moved off the row into its one kebab)."""
    row = page.locator(f"#lifeOsList li.lifeos-item[data-id='{skill_id}']")
    row.locator(".action-row-kebab").click()
    return open_row_menu(page).locator(item)
