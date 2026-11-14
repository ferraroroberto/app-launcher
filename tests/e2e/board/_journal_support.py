"""The Board's "Since you last looked" card, as the answer-sheet test checks it.

Hermetic: /api/board/chief-journal is mocked with made-up entries and the
look stamp is captured, so nothing reads or writes the owner's real journal.
Covered across the helpers: new entries newest first with a kind chip and a
linked ref and a Clear button (the older one is never listed), the one-line
state with no button once a look moves them out of sight, Clear stamping that
look itself, an unreadable journal as a quiet line, and no
journal as a hidden card.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from playwright.sync_api import Page, expect

from tests.e2e.board._chief_support import _open_board


def _stamp(minutes_ago: int) -> str:
    moment = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _entry(minutes_ago: int, kind: str, ref: str, text: str, *, new: bool) -> dict:
    url = f"https://github.com/octo/{ref.replace('#', '/issues/')}" if ref else ""
    return {"ts": _stamp(minutes_ago), "kind": kind, "ref": ref, "text": text,
            "ref_url": url, "new": new}


def _journal(*entries: dict) -> dict:
    return {
        "state": "ok", "entries": list(entries),
        "new_count": sum(1 for e in entries if e["new"]), "last_look": _stamp(30),
    }


def serve_journal(page: Page) -> tuple:
    """Serve a journal of two new entries and an older one; a captured look
    stamp turns them all old, as the server's does. Returns ``(served, stamps)``."""
    served = {"body": _journal(
        _entry(2, "merged", "demo-repo#12", "merged: made-up change", new=True),
        _entry(9, "asked", "", "waiting on a made-up answer", new=True),
        _entry(45, "dispatched", "demo-repo#11", "lane for an older item", new=False),
    )}
    stamps: list = []

    def _look(route):
        stamps.append(route.request.method)
        served["body"] = _journal(*(dict(e, new=False) for e in served["body"]["entries"]))
        route.fulfill(json={"last_look": _stamp(0)})

    page.route(
        re.compile(r".*/api/board/chief-journal$"),
        lambda route: route.fulfill(json=served["body"]),
    )
    page.route(re.compile(r".*/api/board/chief/journal/looked$"), _look)
    return served, stamps


def assert_new_entries_then_clear_button(page: Page) -> None:
    card = page.locator("#boardJournal")
    expect(card).to_be_visible()
    expect(card.locator(".board-journal-count")).to_have_text("2")
    rows = card.locator(".board-journal-row")
    expect(rows).to_have_count(2)
    expect(rows.nth(0).locator(".action-row-title")).to_have_text("merged: made-up change")
    expect(rows.nth(0).locator(".board-journal-kind")).to_have_text("merged")
    expect(rows.nth(0).locator(".action-row-meta")).to_contain_text("min ago · demo-repo#12")
    expect(rows.nth(1).locator(".board-journal-kind")).to_have_text("asked")
    # A ref opens its issue; an entry with no ref reports and does nothing.
    expect(rows.nth(0)).to_have_class(re.compile(r"\bis-link\b"))
    expect(rows.nth(1)).not_to_have_class(re.compile(r"\bis-link\b"))
    # The older entry is never listed; Clear ends the card.
    expect(card.get_by_role("button", name="Clear")).to_have_count(1)
    expect(card.get_by_role("button", name=re.compile("Show"))).to_have_count(0)


def assert_a_look_quiets_the_card(page: Page, stamps: list) -> None:
    """After the answer sheet's Done: one stamp went out and nothing is new."""
    card = page.locator("#boardJournal")
    expect(card).to_have_attribute("data-quiet", "true")
    assert stamps == ["POST"], stamps
    expect(card.locator(".board-journal-line")).to_have_text("Nothing new since you last looked.")
    expect(card.locator(".board-journal-count")).to_have_text("")
    expect(card.locator(".board-journal-row")).to_have_count(0)
    expect(card.get_by_role("button")).to_have_count(0)


def assert_clear_stamps_the_look(page: Page, base_url: str, served: dict, stamps: list) -> None:
    """Tapping Clear sends one stamp; the card re-reads and collapses to the quiet line."""
    card = page.locator("#boardJournal")
    served["body"] = _journal(
        _entry(3, "merged", "demo-repo#14", "merged: another made-up change", new=True),
    )
    before = len(stamps)
    _open_board(page, base_url)
    expect(card.locator(".board-journal-row")).to_have_count(1)
    card.get_by_role("button", name="Clear").click()
    expect(card).to_have_attribute("data-quiet", "true")
    assert stamps[before:] == ["POST"], stamps
    expect(card.locator(".board-journal-count")).to_have_text("")
    expect(card.get_by_role("button")).to_have_count(0)


def assert_unreadable_then_missing(page: Page, base_url: str, served: dict) -> None:
    """A journal the server cannot read is one muted line, a missing one hides the card."""
    card = page.locator("#boardJournal")
    served["body"] = {"state": "unreadable"}
    _open_board(page, base_url)
    expect(card.locator(".board-journal-line")).to_have_text("Journal unreadable.")
    expect(page.locator("#toast")).not_to_be_visible()
    served["body"] = {"state": "empty"}
    _open_board(page, base_url)
    expect(card).to_be_hidden()
