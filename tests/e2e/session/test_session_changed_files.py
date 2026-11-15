"""#1349 — a session's ⋮ → Changed files.

Every file the session's edits touched, folded from its transcript (never
git), in the Show changes panel (#977) so the two viewers match. Pinned
against stubbed routes (every path and line synthetic):

* the item sits in the session view's own ⋮ menu for every agent with a
  transcript reader (#1356) — a Codex session's opens the same panel, its
  unnumbered diffs show no gutter and say so, and a deleted file is a step;
* the panel opens *over* the session view, says what it read, lists each
  file with its A / M / D badge and +/− counts, and opens a file into its
  own edits in order (numbered diffs, the Chat step renderer);
* the rows meet the 44px floor and nothing scrolls sideways at 390px;
* a long list (past 12) gets a search field over name and folder, with the
  match count and a no-match way back (#2424); a short one has none;
* a long transcript read in part offers Load older: each tap merges the next older
  chunk (a file in two chunks is one row, counts summed) until the start (#2425);
* no edits, and a session whose transcript can't be read, each get their
  own sentence — never a blank panel.

Geometry goes through ``stable_read`` (#680): the session view re-renders on its own poll.
"""
from __future__ import annotations

import json as _json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.fixtures.waits import OVERLAY_OPEN_MS, stable_read
from tests.e2e.session.test_session_mode_toggle import (
    _mock_git_status,
    _mock_sessions_list,
    _row,
    _session_row,
)

pytestmark = [pytest.mark.smoke, pytest.mark.iphone]

_SID = "sid-changed-files-1349"
_TRANSCRIPT = {
    "available": True, "source": "native", "reason": None, "session_id": _SID,
    "next_cursor": None, "tool_errors": "reported",
    "entries": [
        {"kind": "user", "timestamp": "2026-09-30T10:00:00Z", "text": "tidy up",
         "truncated": False, "sidechain": False},
        {"kind": "assistant", "timestamp": "2026-09-30T10:00:05Z", "text": "Done.",
         "truncated": False, "sidechain": False},
    ],
}
_FILES = {
    "available": True, "reason": None, "session_id": _SID, "source": "transcript",
    "partial": False, "project_exists": True,
    "counts": {"additions": 9, "deletions": 3},
    "files": [
        {"path": "docs/notes.md", "key": "E:/work/proj/docs/notes.md", "status": "A",
         "additions": 5, "deletions": 0, "steps": 2},
        {"path": "src/pkg/" + "very_long_module_name_" * 4 + ".py",
         "key": "E:/work/proj/src/pkg/long.py", "status": "M", "additions": 4, "deletions": 2, "steps": 1},
        {"path": "old.txt", "key": "E:/work/proj/old.txt", "status": "D",
         "additions": 0, "deletions": 1, "steps": 1},
    ],
}
_STEPS = {
    "available": True, "reason": None, "session_id": _SID, "truncated": False, "partial": False,
    "steps": [
        {"timestamp": "2026-09-30T10:01:00Z", "created": True,
         "diff": {"hunks": [{"old_start": 0, "new_start": 1, "lines": ["+# Notes", "+one", "+two"]}],
                  "numbered": True, "truncated": False}},
        {"timestamp": "2026-09-30T10:04:00Z", "created": False,
         "diff": {"hunks": [{"old_start": 3, "new_start": 3, "lines": [" two", "+three", "+four"]}],
                  "numbered": True, "truncated": False}},
    ],
}


def _mock(page: Page, *, agent: str = "claude", files: dict = _FILES, steps: dict = _STEPS) -> list:
    _mock_git_status(page)
    _mock_sessions_list(page, [_session_row(_SID, kind="remote", agent=agent, title="Changes demo")])
    page.route(
        re.compile(r".*/api/claude-code/sessions/" + re.escape(_SID) + r"/transcript(\?.*)?$"),
        lambda route: route.fulfill(status=200, content_type="application/json",
                                    body=_json.dumps(_TRANSCRIPT)),
    )
    page.route(
        re.compile(r".*/api/claude-code/sessions/" + re.escape(_SID) + r"/changed-files$"),
        lambda route: route.fulfill(status=200, content_type="application/json",
                                    body=_json.dumps(files)),
    )
    diffs: list = []

    def _diff(route):
        diffs.append(route.request.url)
        route.fulfill(status=200, content_type="application/json", body=_json.dumps(steps))

    page.route(
        re.compile(r".*/api/claude-code/sessions/" + re.escape(_SID) + r"/changed-files/diff\?.*"),
        _diff,
    )
    return diffs


def _open_session(page: Page, base_url: str) -> None:
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(base_url, wait_until="domcontentloaded")
    _row(page, _SID).locator(".session-open").click()
    expect(page.locator("#terminalOverlay")).to_be_visible(timeout=OVERLAY_OPEN_MS)


def _menu(page: Page):
    page.locator("#terminalMenu").click()
    menu = page.locator(".terminal-menu")
    expect(menu).to_be_visible()
    return menu


_FIT = """
(() => {
  const overlay = document.getElementById('changesOverlay');
  const rows = [...overlay.querySelectorAll('.chg-file > summary')];
  if (!rows.length || !rows[0].getBoundingClientRect().height) return null;
  const de = document.documentElement;
  const body = document.getElementById('changesBody');
  const r = overlay.getBoundingClientRect();
  const top = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
  return {minRow: Math.min(...rows.map(x => x.getBoundingClientRect().height)),
          body: body.scrollWidth - body.clientWidth, doc: de.scrollWidth - de.clientWidth,
          onTop: overlay.contains(top)};
})()
"""


def test_changed_files_lists_the_sessions_edits_and_opens_one(
    authed_page: Page, base_url: str
) -> None:
    diffs = _mock(authed_page)
    _open_session(authed_page, base_url)
    _menu(authed_page).get_by_role("menuitem", name="Changed files").click()

    overlay = authed_page.locator("#changesOverlay")
    expect(overlay).to_be_visible()
    expect(authed_page.locator("#changesTitle")).to_have_text("Changed files · Changes demo")
    summary = authed_page.locator("#changesList .chg-summary")
    expect(summary).to_contain_text("3 files")
    expect(summary).to_contain_text("+9")
    expect(summary).to_contain_text("from this session’s transcript")
    rows = authed_page.locator("#changesList .chg-file")
    expect(rows).to_have_count(3)
    expect(rows.locator(".chg-badge")).to_have_text(["A", "M", "D"])
    expect(rows.nth(2).locator(".chg-badge")).to_have_attribute("title", "deleted")
    expect(rows.first.locator(".chg-add")).to_have_text("+5")
    # One flat list: no Changes / Untracked groups for a session.
    expect(authed_page.locator("#changesList .chg-group-title")).to_have_count(0)
    expect(authed_page.locator("#changesList .chg-filter")).to_have_count(0)   # a short list needs no search
    assert diffs == []

    fit = stable_read(lambda: authed_page.evaluate(_FIT))
    assert fit["onTop"], f"the panel is not above the session view: {fit}"
    assert fit["minRow"] >= 44, f"a file row is under the 44px floor: {fit}"
    assert fit["body"] <= 0 and fit["doc"] <= 0, f"sideways scroll at 390px: {fit}"

    first = rows.first
    first.locator("summary").click()
    steps = first.locator(".chg-step")
    expect(steps).to_have_count(2)
    expect(steps.nth(0)).to_contain_text("Created")
    expect(steps.nth(1)).to_contain_text("Edit 2 of 2")
    diffs_shown = first.locator(".tr-diff")
    expect(diffs_shown).to_have_count(2)
    expect(diffs_shown.nth(1).locator(".d-add .d-ln").first).to_have_text("4")
    expect(diffs_shown.nth(0).locator(".d-add").first).not_to_have_css("background-color", "rgba(0, 0, 0, 0)")
    assert len(diffs) == 1 and "path=E%3A%2Fwork%2Fproj%2Fdocs%2Fnotes.md" in diffs[0], diffs

    _assert_long_list_filters_by_name_and_folder(authed_page)
    _assert_empty_and_unreadable_say_so(authed_page)
    _assert_partial_read_loads_older_in_chunks(authed_page, diffs)

    # Closing lands back on the session view it came from.
    authed_page.locator("#changesClose").click()
    expect(overlay).to_be_hidden()
    expect(authed_page.locator("#terminalOverlay")).to_be_visible()


def _serve_files(page: Page, body: dict) -> None:
    pattern = re.compile(r".*/api/claude-code/sessions/" + re.escape(_SID) + r"/changed-files$")
    page.unroute(pattern)
    page.route(pattern, lambda route: route.fulfill(
        status=200, content_type="application/json", body=_json.dumps(body)))


def _assert_empty_and_unreadable_say_so(page: Page) -> None:
    """No edits, and a transcript that can't be read, each get their own
    sentence after Refresh re-reads — never a blank panel."""
    _serve_files(page, {**_FILES, "files": [], "counts": {"additions": 0, "deletions": 0}})
    page.locator("#changesRefresh").click()
    state = page.locator("#changesState")
    expect(state).to_have_text("No files changed in this session")
    expect(page.locator("#changesList .chg-file")).to_have_count(0)

    _serve_files(page, {"available": False, "reason": "no_transcript", "session_id": _SID,
                        "entries": [], "next_cursor": None, "source": None})
    page.locator("#changesRefresh").click()
    expect(state).to_have_text("No transcript for this session")


def _many_files(n: int = 14) -> dict:
    """`n` synthetic files: two named `*_alpha.py` under `src/`, the rest `docs/note_<i>.md`."""
    names = ["src/one_alpha.py", "src/two_alpha.py"] + [f"docs/note_{i}.md" for i in range(n - 2)]
    return {
        **_FILES,
        "counts": {"additions": n * 2, "deletions": n},
        "files": [
            {"path": p, "key": "E:/work/proj/" + p, "status": "M", "additions": 2, "deletions": 1, "steps": 1}
            for p in names
        ],
    }


def _assert_long_list_filters_by_name_and_folder(page: Page) -> None:
    """A long list gets a search field: name or folder narrows it, the summary counts the
    matches, no match says so with a way back, and Refresh keeps the text."""
    _serve_files(page, _many_files())
    page.locator("#changesRefresh").click()

    rows = page.locator("#changesList .chg-file:visible")
    expect(rows).to_have_count(14)
    field = page.locator("#changesList .chg-filter input")
    expect(field).to_have_attribute("placeholder", "Filter 14 files…")
    summary = page.locator("#changesList .chg-summary")
    expect(summary).to_contain_text("14 files")

    field.fill("ALPHA")                                   # case-insensitive, on the name
    expect(rows).to_have_count(2)
    expect(summary).to_contain_text("2 of 14 files")
    expect(summary).to_contain_text("+4")

    field.fill("docs/")                                   # on the folder
    expect(rows).to_have_count(12)
    expect(summary).to_contain_text("12 of 14 files")

    field.fill("zzz-nothing")
    expect(rows).to_have_count(0)
    none = page.locator("#changesList .empty-state")
    expect(none).to_contain_text("No files match")
    none.get_by_role("button", name="Clear filter").click()
    expect(rows).to_have_count(14)
    expect(none).to_be_hidden()
    expect(field).to_have_value("")
    expect(summary).to_have_text(re.compile(r"^14 files · "))

    fit = stable_read(lambda: page.evaluate(_FIT))
    assert fit["body"] <= 0 and fit["doc"] <= 0, f"sideways scroll at 390px: {fit}"
    box = stable_read(lambda: field.locator("xpath=..").bounding_box())
    assert box and box["height"] >= 44, f"the filter field is under the 44px floor: {box}"

    field.fill("alpha")                                   # Refresh re-reads and keeps the text
    page.locator("#changesRefresh").click()
    expect(page.locator("#changesList .chg-filter input")).to_have_value("alpha")
    expect(rows).to_have_count(2)


def test_changed_files_opens_for_a_codex_session_with_unnumbered_diffs(
    authed_page: Page, base_url: str
) -> None:
    steps = {
        **_STEPS,
        "steps": [
            {"timestamp": "2026-09-30T10:01:00Z", "created": False,
             "diff": {"hunks": [{"old_start": None, "new_start": None, "lines": [" ctx", "-old", "+new"]}],
                      "numbered": False, "truncated": False}},
            {"timestamp": "2026-09-30T10:04:00Z", "created": False, "deleted": True,
             "diff": {"hunks": [], "numbered": False, "truncated": False}},
        ],
    }
    diffs = _mock(authed_page, agent="codex", steps=steps)
    _open_session(authed_page, base_url)
    _menu(authed_page).get_by_role("menuitem", name="Changed files").click()
    expect(authed_page.locator("#changesList .chg-file")).to_have_count(3)

    first = authed_page.locator("#changesList .chg-file").first
    first.locator("summary").click()
    labels = first.locator(".chg-step")
    expect(labels).to_have_count(2)
    expect(labels.nth(0)).to_contain_text("Edit 1 of 2")
    expect(labels.nth(1)).to_contain_text("Deleted")
    expect(first.locator(".tr-diff")).to_have_count(1)          # a delete draws no diff
    expect(first.locator(".tr-diff .d-add")).to_have_count(1)
    expect(first.locator(".tr-diff .d-ln")).to_have_count(0)    # unnumbered: no gutter
    expect(first).to_contain_text("This agent’s diffs carry no line numbers")
    assert len(diffs) == 1, diffs


def _assert_partial_read_loads_older_in_chunks(page: Page, diffs: list) -> None:
    """A long transcript says only part of it was read; Load older adds the next
    older chunk per tap (a file in two chunks stays one row, counts summed) and
    goes away at the start. An older-only file's diff reads as far back as the list."""
    seen_diffs = len(diffs)
    chunks = {
        None: {**_FILES, "partial": True, "next_cursor": 5000},
        "5000": {
            **_FILES, "partial": True, "next_cursor": 1200, "counts": {"additions": 5, "deletions": 1},
            "files": [
                {"path": "docs/notes.md", "key": "E:/work/proj/docs/notes.md", "status": "A",
                 "additions": 2, "deletions": 0, "steps": 1},
                {"path": "lib/early.py", "key": "E:/work/proj/lib/early.py", "status": "A",
                 "additions": 3, "deletions": 1, "steps": 1},
            ],
        },
        "1200": {
            **_FILES, "partial": False, "next_cursor": None, "counts": {"additions": 1, "deletions": 0},
            "files": [{"path": "zeta.py", "key": "E:/work/proj/zeta.py", "status": "A",
                       "additions": 1, "deletions": 0, "steps": 1}],
        },
    }
    asked: list = []

    def _chunk(route):
        match = re.search(r"before=(\d+)", route.request.url)
        asked.append(match.group(1) if match else None)
        route.fulfill(status=200, content_type="application/json",
                      body=_json.dumps(chunks[match.group(1) if match else None]))

    page.route(
        re.compile(r".*/api/claude-code/sessions/" + re.escape(_SID) + r"/changed-files(\?.*)?$"), _chunk)
    page.locator("#changesRefresh").click()

    rows = page.locator("#changesList .chg-file")
    older = page.locator("#changesList .chg-older")
    summary = page.locator("#changesList .chg-summary")
    expect(rows).to_have_count(3)
    expect(page.locator("#changesList .chg-partial")).to_contain_text("only part of it was read")
    expect(older).to_have_text("Load older")
    box = stable_read(lambda: older.bounding_box())
    assert box and box["height"] >= 44, f"Load older is under the 44px floor: {box}"

    older.click()
    expect(rows).to_have_count(4)
    expect(summary).to_contain_text("4 files")
    expect(summary).to_contain_text("+14")
    expect(rows.filter(has_text="notes.md").locator(".chg-add")).to_have_text("+7")   # one row, 5 + 2
    expect(older).to_have_text("Load older")

    older.click()
    expect(rows).to_have_count(5)
    expect(summary).to_contain_text("+15")
    expect(page.locator("#changesList .chg-partial")).to_have_count(0)
    assert asked == [None, "5000", "1200"], asked

    rows.filter(has_text="zeta.py").locator("summary").click()
    expect(page.locator("#changesList .chg-step").first).to_be_visible()
    assert len(diffs) == seen_diffs + 1 and "since=0" in diffs[-1], diffs
    fit = stable_read(lambda: page.evaluate(_FIT))
    assert fit["body"] <= 0 and fit["doc"] <= 0, f"sideways scroll at 390px: {fit}"
