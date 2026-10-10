"""#1478 — a reply's mention of a file this session edited is a file chip.

Step 5/6 of #1472. Pinned against a stubbed transcript (every path and line
synthetic):

* a code span or bare path naming an edited file (its whole path, or its name
  with any folders above it, unique among the edited files) becomes a chip
  with the file glyph and the file's name, a line reference kept; a name two
  edited files share, a file not edited, a URL and a fenced listing stay as
  they were;
* the chip is the code chip in the accent: same height in the line, accent
  fill, and nothing scrolls sideways at 390px, light and dark;
* a chip in the turn that edited the file opens Changed files on This turn,
  focused on it, with no request; a chip in a later turn opens the whole
  session's list, focused on it;
* a file edited after the reply named it links on the tick the edit arrives;
* the Life OS conversation viewer, which shares the renderer, has no chips.
"""
from __future__ import annotations

import json as _json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e import _fake_clock
from tests.e2e.conftest import stable_eval
from tests.e2e.test_chat_edited_card import _POLL_MS, _SID, _Stub, _boot, _edit, _turn
from tests.e2e.test_life_os_tab import (
    _FAKE_TRANSCRIPT,
    _mock_conversations,
    _mock_skills,
    _mock_transcript,
    _open_conversations,
    _open_viewer,
)

pytestmark = pytest.mark.smoke

_REPLY = (
    "Reworked `queue.ts` and `src/ui/a.ts:12`, see src/net/queue.ts. and README.md.\n\n"
    "Left `a.ts` alone (two edited files share it), and `src/other.ts` was never edited.\n\n"
    "Docs: https://example.com/src/net/queue.ts\n\n"
    "```\nqueue.ts\n```"
)

_NEWEST = [
    _turn("user", "rework the queue", 10),
    _edit(20, "src/net/queue.ts", 3, 0),
    _edit(30, "src/ui/a.ts", 1, 1),
    _edit(40, "src/legacy/a.ts", 2, 0),
    _edit(50, "README.md", 2, 0, verb="wrote", created=True),
    _turn("assistant", _REPLY, 60),
    _turn("user", "and the queue?", 70),
    _turn("assistant", "Still in `queue.ts`.", 80),
]


def _replies(page: Page):
    return page.locator("#transcriptList .tr-reply")


@pytest.mark.iphone
def test_a_reply_links_the_files_the_session_edited(authed_page: Page, base_url: str) -> None:
    page = authed_page
    stub = _boot(page, base_url, _NEWEST, cursor=None)
    first = _replies(page).first
    chips = first.locator(".tr-file-chip")
    expect(chips.locator(".tr-file-chip-name")).to_have_text(["queue.ts", "a.ts:12", "queue.ts", "README.md"])
    expect(chips.first).to_have_attribute("title", re.compile(r"/src/net/queue\.ts$"))
    expect(chips.first).to_have_accessible_name("Open the diff of queue.ts")
    expect(chips.first.locator("svg.icon")).to_have_count(1)
    # Ambiguous, never edited, a URL and a listing stay as they were.
    expect(first.locator("p code")).to_have_text(["a.ts", "src/other.ts"])
    expect(first.locator("a[href]")).to_have_text("https://example.com/src/net/queue.ts")
    expect(first.locator("pre code")).to_have_text("queue.ts")
    # The bare path keeps its trailing full stop outside the chip.
    expect(first).to_contain_text("queue.ts. and README.md.")
    expect(_replies(page).nth(1).locator(".tr-file-chip-name")).to_have_text("queue.ts")

    fit = """
    (reply) => {
      const box = document.getElementById('transcriptBody');
      const chip = reply.querySelector('.tr-file-chip');
      const code = reply.querySelector('code');
      const cs = getComputedStyle(chip);
      return {chip: chip.getBoundingClientRect().height, code: code.getBoundingClientRect().height,
              bg: cs.backgroundColor, codeBg: getComputedStyle(code).backgroundColor,
              reply: reply.scrollWidth - reply.clientWidth, body: box.scrollWidth - box.clientWidth};
    }
    """
    for theme in ("light", "dark"):
        page.evaluate("t => document.documentElement.setAttribute('data-theme', t)", theme)
        m = stable_eval(first, fit)
        assert abs(m["chip"] - m["code"]) <= 2, f"the chip and the code chip differ in height ({theme}): {m}"
        assert m["bg"] not in ("rgba(0, 0, 0, 0)", m["codeBg"]), f"the chip is not on the accent ({theme}): {m}"
        assert m["reply"] <= 0 and m["body"] <= 0, f"sideways scroll ({theme}): {m}"
    page.evaluate("document.documentElement.setAttribute('data-theme', 'light')")

    # In the turn that edited it: This turn, focused, drawn from the page.
    chips.nth(1).click()
    expect(page.locator("#changesOverlay")).to_be_visible()
    scope = page.locator("#changesList .chg-scope-btn")
    expect(scope.first).to_have_attribute("aria-pressed", "true")
    focus = page.locator("#changesList .chg-file.chg-focus")
    expect(focus.locator(".chg-base")).to_have_text("a.ts")
    expect(focus.locator(".chg-dir")).to_have_text("src/ui")
    expect(focus).to_have_js_property("open", True)
    assert stub.changes == [], "This turn is drawn from the loaded steps"
    page.locator("#changesClose").click()
    expect(page.locator("#changesOverlay")).to_be_hidden()

    # In a later turn: the whole session, focused on the file.
    _replies(page).nth(1).locator(".tr-file-chip").click()
    expect(page.locator("#changesList .chg-summary")).to_contain_text("from this session’s transcript")
    expect(focus.locator(".chg-base")).to_have_text("queue.ts")
    expect(focus).to_have_js_property("open", True)
    assert any(u.endswith("/changed-files") for u in stub.changes), stub.changes


class _LiveStub(_Stub):
    """The base stub, whose next live tick appends ``tick``, once set."""

    def __init__(self, *args, **kw) -> None:
        self.tick: list = []
        super().__init__(*args, **kw)

    def _read(self, route) -> None:
        if "after=" not in route.request.url or not self.tick:
            return super()._read(route)
        self.calls.append(route.request.url)
        # A forward read returns everything from the old tail on: the
        # message that was still pending, now settled, then what is new.
        entries = [self.newest[-1]] + self.tick
        self.newest, self.tick = self.newest + self.tick, []
        tail = self.newest[-1]["offset"]
        route.fulfill(status=200, content_type="application/json", body=_json.dumps({
            "available": True, "source": "native", "reason": None, "session_id": _SID,
            "tool_errors": "reported", "activity": None, "tail": tail, "size": tail + 1,
            "changed": True, "reset": False, "entries": entries, "pending": [],
        }))


def test_a_file_edited_after_the_reply_named_it_links_when_the_edit_arrives(
    authed_page: Page, base_url: str
) -> None:
    page = authed_page
    # The reply is settled, not the page's pending tail, so the tick has to
    # redo a reply already on screen rather than draw a fresh one.
    newest = [
        _turn("user", "plan it", 10),
        _turn("assistant", "I'll change `src/late.ts` next.", 20),
        _turn("user", "go ahead", 30),
    ]
    stub = _boot(page, base_url, newest, cursor=None, stub_cls=_LiveStub)
    reply = _replies(page).first
    expect(reply.locator("code")).to_have_text("src/late.ts")
    expect(reply.locator(".tr-file-chip")).to_have_count(0)

    stub.tick = [_edit(40, "src/late.ts", 1, 0)]
    before = stub.ticks()
    for _ in range(5):
        _fake_clock.advance(page, _POLL_MS + 1)
        if stub.ticks() > before:
            break
    assert stub.ticks() > before, "the live poll never fired on the fake clock"
    expect(reply.locator(".tr-file-chip-name")).to_have_text("late.ts")
    expect(reply.locator("code")).to_have_count(0)


def test_the_life_os_viewer_has_no_file_chips(authed_page: Page, base_url: str) -> None:
    capture = dict(_FAKE_TRANSCRIPT, entries=[
        _FAKE_TRANSCRIPT["entries"][0],
        _edit(60, "notes.md", 2, 0),
        _FAKE_TRANSCRIPT["entries"][1] | {"offset": 120, "text": "Updated `notes.md`."},
    ])
    _mock_skills(authed_page)
    _mock_conversations(authed_page)
    _mock_transcript(authed_page, capture)
    _open_conversations(authed_page, base_url)
    viewer = _open_viewer(authed_page, 0)
    expect(viewer.locator(".tr-reply code")).to_have_text("notes.md")
    expect(viewer.locator(".tr-file-chip")).to_have_count(0)
