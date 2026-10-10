"""#1477 — an "Edited N files" card closes each Chat turn that edited files.

Step 4/6 of #1472. Pinned against a stubbed transcript (every path and line
synthetic):

* once a turn is over (a prompt follows it), it ends with one card: the
  turn's total +N −M, one row per file it edited (A/M/D badge, name in mono,
  folder as the hint, its own +N −M), the first 3 rows then "Show N more";
  a failed edit counts nowhere, and the card stays when ⋮ Hide steps hides
  the step lines;
* a turn cut by a page boundary counts what is loaded and says "Earlier steps
  not loaded · Load older"; Load older pulls the page in and the card
  recounts;
* the newest turn shows no card while the activity line says the agent is
  working, and gets it on the tick that says it stopped;
* a row opens Changed files on "This turn", focused on that file, drawn from
  the loaded steps with no request; "Whole session" reads the session's list
  and opens the same file there;
* the rows meet rows.md and nothing scrolls sideways at 390px, light and dark;
* the Life OS conversation viewer, which shares the renderer, has no card.

Geometry goes through ``stable_eval`` (#680/#1346): Chat re-renders on its
own poll, which the fake clock holds still here (#1376).
"""
from __future__ import annotations

import json as _json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e import _fake_clock
from tests.e2e.conftest import OVERLAY_OPEN_MS, stable_eval
from tests.e2e.test_life_os_tab import (
    _FAKE_TRANSCRIPT,
    _mock_conversations,
    _mock_skills,
    _mock_transcript,
    _open_conversations,
    _open_viewer,
)
from tests.e2e.test_session_mode_toggle import (
    _mock_git_status,
    _mock_sessions_list,
    _row,
    _session_row,
)

pytestmark = pytest.mark.smoke

_SID = "sid-edited-card-1477"
_ROOT = "E:/automation/modeproj"   # _session_row's project_dir
_POLL_MS = 3_000                    # session-transcript.js LIVE_POLL_MS


def _diff(lines):
    return {"hunks": [{"old_start": 1, "new_start": 1, "lines": lines}],
            "numbered": True, "truncated": False, "offset": 0, "n": 0}


def _edit(offset, path, added, removed, *, verb="edited", created=False, error=False):
    action = {"verb": verb, "path": _ROOT + "/" + path}
    if verb != "deleted":
        action.update(added=added, removed=removed,
                      diff=_diff(["-x"] * removed + ["+line %d" % i for i in range(added)]))
    if created:
        action["created"] = True
    e = {"kind": "tool_call", "timestamp": "2026-10-09T10:00:%02dZ" % (offset % 60), "name": "Edit",
         "summary": path, "result": "ok", "result_truncated": False, "sidechain": False,
         "offset": offset, "action": action}
    if error:
        e["error"] = True
    return e


def _turn(kind, text, offset):
    return {"kind": kind, "timestamp": "2026-10-09T10:01:00Z", "text": text,
            "truncated": False, "sidechain": False, "offset": offset}


# The older page: the turn's prompt and its first edit.
_OLDER = [
    _turn("user", "rework the queue", 10),
    _edit(20, "src/net/queue.ts", 2, 1),
]
# The newest page starts mid-turn: the rest of its edits, its reply, then a
# second exchange with no edits.
_NEWEST = [
    _edit(30, "src/net/queue.ts", 3, 0),
    _edit(40, "README.md", 2, 0, verb="wrote", created=True),
    _edit(50, "src/ui/a.ts", 1, 1),
    _edit(60, "src/ui/b.ts", 4, 2),
    _edit(70, "old.txt", 0, 0, verb="deleted"),
    _edit(80, "src/ui/c.ts", 9, 9, error=True),   # failed: counts nowhere
    _turn("assistant", "Reworked the queue.", 90),
    _turn("user", "thanks", 100),
    _turn("assistant", "Any time.", 110),
]
_SESSION_FILES = {
    "available": True, "reason": None, "session_id": _SID, "source": "transcript",
    "partial": False, "project_exists": True, "counts": {"additions": 30, "deletions": 9},
    "files": [
        {"path": "README.md", "key": _ROOT + "/README.md", "status": "A",
         "additions": 2, "deletions": 0, "steps": 1},
        {"path": "src/ui/b.ts", "key": _ROOT + "/src/ui/b.ts", "status": "M",
         "additions": 20, "deletions": 4, "steps": 3},
        {"path": "src/net/queue.ts", "key": _ROOT + "/src/net/queue.ts", "status": "M",
         "additions": 8, "deletions": 5, "steps": 4},
    ],
}
_STEPS = {
    "available": True, "reason": None, "session_id": _SID, "truncated": False, "partial": False,
    "steps": [{"timestamp": "2026-10-09T09:00:00Z", "created": False, "diff": _diff(["+early"])}],
}


class _Stub:
    """The transcript route, all three reads: the newest page, an older page
    (``before=``) and a live tick (``after=``)."""

    def __init__(self, page: Page, newest: list, *, cursor=25, activity=None) -> None:
        self.newest = newest
        self.cursor = cursor
        self.activity = activity
        self.calls: list[str] = []
        self.changes: list[str] = []
        page.route(re.compile(r".*/api/claude-code/sessions/" + _SID + r"/transcript(\?.*)?$"), self._read)
        page.route(re.compile(r".*/api/claude-code/sessions/" + _SID + r"/changed-files$"), self._files)
        page.route(re.compile(r".*/api/claude-code/sessions/" + _SID + r"/changed-files/diff\?.*"), self._files)

    def _read(self, route) -> None:
        url = route.request.url
        self.calls.append(url)
        tail = self.newest[-1]["offset"]
        body = {"available": True, "source": "native", "reason": None, "session_id": _SID,
                "tool_errors": "reported", "activity": self.activity, "tail": tail, "size": tail + 1}
        if "after=" in url:
            body.update(changed=False, reset=False, entries=[], pending=[])
        elif "before=" in url:
            body.update(entries=_OLDER, next_cursor=None)
        else:
            body.update(entries=self.newest, next_cursor=self.cursor)
        route.fulfill(status=200, content_type="application/json", body=_json.dumps(body))

    def _files(self, route) -> None:
        url = route.request.url
        self.changes.append(url)
        route.fulfill(status=200, content_type="application/json",
                      body=_json.dumps(_STEPS if "/diff?" in url else _SESSION_FILES))

    def ticks(self) -> int:
        return sum(1 for u in self.calls if "after=" in u)


def _boot(page: Page, base_url: str, newest: list = _NEWEST, *, height: int = 1600, **kw) -> _Stub:
    """Chat open on the stub. The window is phone-wide but tall enough that
    the list never scrolls: a scroll to its top would pull the older page in
    by itself and race the assertions on the cut turn."""
    _mock_git_status(page)
    _mock_sessions_list(page, [_session_row(_SID, kind="remote", agent="claude", title="Edits demo")])
    stub = _Stub(page, newest, **kw)
    _fake_clock.install(page)
    page.set_viewport_size({"width": 390, "height": height})
    page.goto(base_url, wait_until="domcontentloaded")
    _row(page, _SID).locator(".session-open").click()
    expect(page.locator("#terminalOverlay")).to_be_visible(timeout=OVERLAY_OPEN_MS)
    expect(page.locator("#transcriptList .tr-turn")).not_to_have_count(0)
    return stub


def _card(page: Page):
    return page.locator("#transcriptList .tr-edited")


_FIT = """
(card) => {
  const box = document.getElementById('transcriptBody');
  const de = document.documentElement;
  const h = (sel) => [...card.querySelectorAll(sel)].map(n => n.getBoundingClientRect().height);
  return {head: Math.min(...h('.tr-edited-head')), rows: Math.min(...h('.tr-edited-row')),
          older: Math.min(...h('.tr-edited-older')),
          card: card.scrollWidth - card.clientWidth,
          body: box.scrollWidth - box.clientWidth, doc: de.scrollWidth - de.clientWidth};
}
"""


@pytest.mark.iphone
def test_an_ended_turn_closes_with_its_edited_files(authed_page: Page, base_url: str) -> None:
    page = authed_page
    stub = _boot(page, base_url)
    card = _card(page)
    # Only the turn that edited files has one; the later exchange has none.
    expect(card).to_have_count(1)
    expect(page.locator("#transcriptList .tr-agent-item").first.locator(".tr-edited")).to_have_count(1)
    expect(card.locator(".tr-edited-title")).to_have_text("Edited 5 files")
    expect(card.locator(".tr-edited-head .chg-counts")).to_have_text("+10 −3")
    rows = card.locator(".tr-edited-row")
    expect(rows).to_have_count(3)
    expect(rows.locator(".chg-badge")).to_have_text(["M", "A", "M"])
    expect(rows.nth(1).locator(".chg-badge")).to_have_attribute("title", "added")
    expect(rows.locator(".chg-base")).to_have_text(["queue.ts", "README.md", "a.ts"])
    expect(rows.locator(".chg-dir")).to_have_text(["src/net", "project root", "src/ui"])
    expect(rows.first.locator(".chg-counts")).to_have_text("+3 −0")
    expect(card.locator(".tr-edited-partial")).to_have_text("Earlier steps not loaded · Load older")

    for theme in ("light", "dark"):
        page.evaluate("t => document.documentElement.setAttribute('data-theme', t)", theme)
        expect(rows.first.locator(".chg-badge")).not_to_have_css("background-color", "rgba(0, 0, 0, 0)")
        fit = stable_eval(card, _FIT)
        assert fit["head"] >= 52 and fit["rows"] >= 52, f"a card row is under rows.md ({theme}): {fit}"
        assert fit["older"] >= 44, f"Load older is under the 44px floor ({theme}): {fit}"
        assert fit["card"] <= 0 and fit["body"] <= 0 and fit["doc"] <= 0, f"sideways scroll ({theme}): {fit}"
    page.evaluate("document.documentElement.setAttribute('data-theme', 'light')")

    more = card.locator(".tr-edited-more")
    expect(more).to_have_text("Show 2 more")
    more.click()
    expect(rows).to_have_count(5)
    expect(rows.locator(".chg-badge")).to_have_text(["M", "A", "M", "M", "D"])
    expect(more).to_have_count(0)

    # Hide steps leaves the card: the work still has a trace.
    page.locator("#terminalMenu").click()
    page.locator("#terminalOverlay .terminal-menu").get_by_role("menuitem", name="Hide steps").click()
    expect(page.locator("#transcriptList .tr-run").first).to_be_hidden()
    expect(card).to_be_visible()

    # Load older pulls the turn's start in; the card recounts and stays open.
    card.locator(".tr-edited-older").click()
    expect(card.locator(".tr-edited-partial")).to_have_count(0)
    expect(card.locator(".tr-edited-head .chg-counts")).to_have_text("+12 −4")
    expect(rows.first.locator(".chg-counts")).to_have_text("+5 −1")
    expect(rows).to_have_count(5)
    assert any("before=" in u for u in stub.calls), stub.calls
    assert stub.changes == [], "the card is folded on the page, never fetched"


@pytest.mark.iphone
def test_a_row_opens_changed_files_on_this_turn_then_the_whole_session(
    authed_page: Page, base_url: str
) -> None:
    page = authed_page
    stub = _boot(page, base_url)
    card = _card(page)
    card.locator(".tr-edited-more").click()
    card.locator('.tr-edited-row[data-path$="src/ui/b.ts"]').click()

    overlay = page.locator("#changesOverlay")
    expect(overlay).to_be_visible()
    expect(page.locator("#changesTitle")).to_have_text("Changed files · Edits demo")
    scope = page.locator("#changesList .chg-scope-btn")
    expect(scope).to_have_text(["This turn", "Whole session"])
    expect(scope.first).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#changesList .chg-summary")).to_have_text(re.compile(r"^5 files · \+10 −3 · this turn$"))
    rows = page.locator("#changesList .chg-file")
    expect(rows.locator(".chg-base")).to_have_text(["old.txt", "README.md", "queue.ts", "a.ts", "b.ts"])
    focus = page.locator("#changesList .chg-file.chg-focus")
    expect(focus).to_have_count(1)
    expect(focus.locator(".chg-base")).to_have_text("b.ts")
    expect(focus).to_have_js_property("open", True)
    expect(focus.locator(".chg-step")).to_have_text([re.compile(r"^Edit 1 of 1")])
    expect(focus.locator(".tr-diff .d-add")).to_have_count(4)
    expect(focus.locator(".tr-diff-path")).to_have_count(0)    # the row already names the file
    fit = stable_eval(focus, "(el) => ({row: el.querySelector('summary').getBoundingClientRect().height,"
                             " body: document.getElementById('changesBody').scrollWidth"
                             " - document.getElementById('changesBody').clientWidth})")
    assert fit["row"] >= 52 and fit["body"] <= 0, fit
    assert stub.changes == [], "This turn is drawn from the loaded steps"

    scope.nth(1).click()
    expect(scope.nth(1)).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#changesList .chg-summary")).to_contain_text("from this session’s transcript")
    expect(rows).to_have_count(3)
    expect(focus.locator(".chg-base")).to_have_text("b.ts")
    expect(focus).to_have_js_property("open", True)
    expect(focus.locator(".chg-step")).to_have_text([re.compile(r"^Edit 1 of 3")])
    assert any(u.endswith("/changed-files") for u in stub.changes), stub.changes
    assert any("path=E%3A%2Fautomation%2Fmodeproj%2Fsrc%2Fui%2Fb.ts" in u for u in stub.changes), stub.changes

    scope.first.click()
    expect(rows).to_have_count(5)
    page.locator("#changesClose").click()
    expect(overlay).to_be_hidden()
    expect(page.locator("#terminalOverlay")).to_be_visible()


@pytest.mark.iphone
def test_the_newest_turn_gets_its_card_when_the_agent_stops(authed_page: Page, base_url: str) -> None:
    page = authed_page
    # Enough earlier exchanges that the list scrolls; no older page, so
    # nothing loads by itself when it does.
    history = []
    for i in range(8):
        history += [_turn("user", "question %d" % i, 2 + 4 * i), _turn("assistant", "answer %d" % i, 4 + 4 * i)]
    newest = history + [_turn("user", "tidy", 40), _edit(50, "src/ui/a.ts", 1, 0), _turn("assistant", "On it", 60)]
    working = {"since": "2026-10-09T10:00:00Z", "actions": 1, "last": "Thinking", "working": True}
    stub = _boot(page, base_url, newest, cursor=None, activity=working, height=700)
    expect(page.locator("#transcriptList .tr-step")).to_be_visible()
    expect(_card(page)).to_have_count(0)

    stub.activity = dict(working, working=False)
    before = stub.ticks()
    for _ in range(5):
        _fake_clock.advance(page, _POLL_MS + 1)
        if stub.ticks() > before:
            break
    assert stub.ticks() > before, "the live poll never fired on the fake clock"
    expect(_card(page).locator(".tr-edited-title")).to_have_text("Edited 1 file")
    expect(_card(page).locator(".tr-edited-partial")).to_have_count(0)
    # A reader following the conversation stays at its end, the new card included.
    gap = page.evaluate("() => { const b = document.getElementById('transcriptBody');"
                        " return {gap: b.scrollHeight - b.clientHeight - b.scrollTop, over: b.scrollHeight - b.clientHeight}; }")
    assert gap["over"] > 0, f"the list does not scroll, so this pins nothing: {gap}"
    assert gap["gap"] <= 2, f"the card landed below a reader who was at the bottom: {gap}"


def test_the_life_os_viewer_has_no_edited_card(authed_page: Page, base_url: str) -> None:
    capture = dict(_FAKE_TRANSCRIPT, entries=[
        _FAKE_TRANSCRIPT["entries"][0],
        _edit(60, "notes.md", 2, 0),
        _FAKE_TRANSCRIPT["entries"][1] | {"offset": 120},
    ])
    _mock_skills(authed_page)
    _mock_conversations(authed_page)
    _mock_transcript(authed_page, capture)
    _open_conversations(authed_page, base_url)
    viewer = _open_viewer(authed_page, 0)
    expect(viewer.locator(".tr-turn")).to_have_count(2)
    expect(viewer.locator(".tr-step")).to_have_count(1)
    expect(viewer.locator(".tr-edited")).to_have_count(0)
