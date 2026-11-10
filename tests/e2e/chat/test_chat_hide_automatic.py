"""Hide automatic: the Chat pane folds the turns nobody typed behind a divider.

The server marks a prompt it knows the launcher or the harness wrote
(``auto``), and the pane folds each such prompt with its reply into a slim
divider row. Pinned here: off for an ordinary session and on for the chief,
the ⋮ menu's toggle and its remembered choice, a divider that expands and folds
again, a run holding a file sent to the owner staying visible, and the
divider's touch target. The grouping rules themselves are
``tests/js/automatic_fold.test.mjs``; the server's marking is
``tests/unit/transcript/test_automatic_turns.py``.
"""

from __future__ import annotations

import json as _json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.fixtures.ui import open_session_row, stub_session_mirror
from tests.e2e.session.test_session_mode_toggle import (
    _mock_git_status,
    _mock_sessions_list,
    _row,
    _session_row,
)
from tests.e2e.session.test_session_transcript import _menu_item

pytestmark = pytest.mark.smoke

_SID = "sid-hide-automatic"
_BELL = "⟳ chief-inbox: pending — run chief_ops.py inbox"


def _entry(kind: str, text: str, offset: int, **extra) -> dict:
    return {"kind": kind, "timestamp": f"2026-11-03T12:{offset // 60:02d}:{offset % 60:02d}Z",
            "text": text, "truncated": False, "sidechain": False, "offset": offset, **extra}


def _bell(offset: int) -> dict:
    return _entry("user", _BELL, offset, auto="doorbell")


def _sent_file(offset: int) -> dict:
    return {"kind": "tool_call", "timestamp": "2026-11-03T12:01:12Z", "name": "SendUserFile",
            "summary": "plan.md", "result": "sent", "result_truncated": False,
            "sidechain": False, "caption": "", "offset": offset,
            "sent_files": [{"offset": offset, "n": 0, "name": "plan.md", "kind": "text"}]}


# Two quiet bells, a bell whose reply sends the owner a file, one more quiet bell.
_ENTRIES = [
    _entry("user", "what changed today?", 0),
    _entry("assistant", "Two fixes landed.", 10),
    _bell(20), _entry("assistant", "Nothing new.", 30),
    _bell(40), _entry("assistant", "Lanes still running.", 50),
    _bell(60), _entry("assistant", "The workshop plan is ready.", 70), _sent_file(75),
    _bell(90), _entry("assistant", "Nothing new.", 100),
    _entry("user", "ok, Friday", 110),
    _entry("assistant", "Noted.", 120),
]


def _open(page: Page, base_url: str, *, chief: bool) -> None:
    _mock_git_status(page)
    row = _session_row(_SID, kind="remote", agent="claude", title="Automatic demo")
    if chief:
        row["label"] = "chief"
    _mock_sessions_list(page, [row])
    body = {
        "available": True, "source": "native", "reason": None, "session_id": _SID,
        "next_cursor": None, "entries": _ENTRIES, "tail": 130, "size": 131,
        "tool_errors": "reported",
    }
    page.route(
        re.compile(r".*/api/claude-code/sessions/" + re.escape(_SID) + r"/transcript(\?.*)?$"),
        lambda route: route.fulfill(status=200, content_type="application/json", body=_json.dumps(body)),
    )
    page.route(
        re.compile(r".*/api/claude-code/sessions/" + re.escape(_SID) + r"/transcript/file\?.*"),
        lambda route: route.fulfill(status=200, content_type="text/plain", body="# plan"),
    )
    page.goto(base_url, wait_until="domcontentloaded")
    stub_session_mirror(page)
    open_session_row(page, _row(page, _SID), mode="chat")
    expect(page.locator("#transcriptList .tr-prompt-item").first).to_be_visible()


def _prompts(page: Page, text: str | None = None):
    items = page.locator("#transcriptList .tr-user:visible")
    return items if text is None else items.filter(has_text=text)


def test_an_ordinary_session_shows_everything_until_asked(authed_page: Page, base_url: str) -> None:
    page = authed_page
    _open(page, base_url, chief=False)
    expect(page.locator("#transcriptList .tr-auto-divider")).to_have_count(0)
    expect(_prompts(page)).to_have_count(6)

    _menu_item(page, "Hide automatic: doorbell and compact-summary turns").click()
    dividers = page.locator("#transcriptList .tr-auto-divider")
    expect(dividers).to_have_count(2)
    expect(dividers.nth(0)).to_have_text(re.compile(r"2 automatic turns"))
    expect(dividers.nth(1)).to_have_text(re.compile(r"1 automatic turn\b"))
    # Two typed prompts stay, and the bell whose reply sent a file stays too.
    expect(_prompts(page)).to_have_count(3)
    expect(_prompts(page, "ok, Friday")).to_have_count(1)
    expect(page.locator("#transcriptList .tr-file-item")).to_be_visible()
    expect(page.locator("#transcriptList .tr-reply:visible", has_text="Nothing new.")).to_have_count(0)

    _menu_item(page, "Show automatic: doorbell and compact-summary turns").click()
    expect(dividers).to_have_count(0)
    expect(_prompts(page)).to_have_count(6)


@pytest.mark.iphone
def test_the_chief_starts_folded_a_divider_expands_and_folds_and_the_choice_is_remembered(
    authed_page: Page, base_url: str
) -> None:
    page = authed_page
    _open(page, base_url, chief=True)
    dividers = page.locator("#transcriptList .tr-auto-divider")
    expect(dividers).to_have_count(2)
    expect(_prompts(page)).to_have_count(3)

    # The divider is a full-width touch target.
    first = dividers.first
    box = first.bounding_box()
    list_box = page.locator("#transcriptList").bounding_box()
    assert box is not None and list_box is not None
    assert box["height"] >= 44, box
    assert abs(box["width"] - list_box["width"]) <= 1, (box, list_box)

    expect(first).to_have_attribute("aria-expanded", "false")
    first.click()
    expect(first).to_have_attribute("aria-expanded", "true")
    expect(page.locator("#transcriptList .tr-reply:visible", has_text="Nothing new.")).to_have_count(1)
    expect(_prompts(page)).to_have_count(5)
    # Expanding one group leaves the other folded.
    expect(dividers.nth(1)).to_have_attribute("aria-expanded", "false")

    first.click()
    expect(first).to_have_attribute("aria-expanded", "false")
    expect(_prompts(page)).to_have_count(3)

    # The menu's choice is remembered for the session across a reload.
    _menu_item(page, "Show automatic: doorbell and compact-summary turns").click()
    expect(dividers).to_have_count(0)

    page.reload(wait_until="domcontentloaded")
    open_session_row(page, _row(page, _SID), mode="chat")
    expect(page.locator("#transcriptList .tr-prompt-item").first).to_be_visible()
    # The chief's default is on, but this session's saved choice is off.
    expect(page.locator("#transcriptList .tr-auto-divider")).to_have_count(0)
    expect(_prompts(page)).to_have_count(6)
