"""#1349 — an edit step in Chat opens as its diff.

Before this, an Edit/Write step opened to its file path and the tool's
"updated successfully" line, so seeing what changed meant leaving for an
editor. Pinned here, against a stubbed page (every path and line synthetic):

* an Edit whose harness recorded real line numbers shows a gutter and a
  ``@@`` header, added lines tinted with ``--diff-add-*`` and removed ones
  with ``--diff-del-*``, in both themes; the tool's result stays underneath;
* a page's capped diff offers **Show full diff** (a 44px target), which
  fetches ``/transcript/diff`` by the step's ref and swaps the whole diff in;
* a Write worked out from its input shows as all added with **no** gutter —
  it has no real line numbers, and none are made up;
* a failed Edit and a tool the server doesn't recognise keep today's body;
* at phone width a long diff line wraps inside the box: no sideways scroll
  in the diff or the page.

Style reads use auto-retrying ``expect().to_have_css`` and the geometry read
goes through ``stable_read`` (#680) — Chat re-renders on its own poll.
"""
from __future__ import annotations

import json as _json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import OVERLAY_OPEN_MS, stable_read
from tests.e2e.test_session_mode_toggle import (
    _mock_git_status,
    _mock_sessions_list,
    _row,
    _session_row,
)

pytestmark = [pytest.mark.smoke, pytest.mark.iphone]

_SID = "sid-step-diffs-1349"
_LONG = "+" + "wrap_me_" * 40

_EDIT_DIFF = {
    "hunks": [{"old_start": 40, "new_start": 40,
               "lines": [" keep", "-old line", "+new line", "+added line", _LONG]}],
    "numbered": True, "truncated": True, "offset": 512, "n": 0,
}
_FULL_DIFF = {
    "hunks": [{"old_start": 40, "new_start": 40,
               "lines": [" keep", "-old line", "+new line", "+added line", _LONG]
               + ["+more %d" % i for i in range(6)]}],
    "numbered": True, "truncated": False,
}


def _call(name, action, *, result="ok", error=False, summary=""):
    e = {"kind": "tool_call", "timestamp": "2026-09-30T10:00:02Z", "name": name,
         "summary": summary, "result": result, "result_truncated": False, "sidechain": False}
    if action:
        e["action"] = action
    if error:
        e["error"] = True
    return e


def _page_body() -> dict:
    return {
        "available": True, "source": "native", "reason": None, "session_id": _SID,
        "next_cursor": None, "tool_errors": "reported",
        "entries": [
            {"kind": "user", "timestamp": "2026-09-30T10:00:00Z",
             "text": "tidy the module", "truncated": False, "sidechain": False},
            _call("Edit", {"verb": "edited", "path": "src/pkg/module.py", "added": 8, "removed": 1,
                           "diff": _EDIT_DIFF},
                  result="The file src/pkg/module.py has been updated successfully."),
            _call("Write", {"verb": "wrote", "path": "notes/new.md", "added": 2, "removed": 0,
                            "diff": {"hunks": [{"old_start": None, "new_start": None,
                                                "lines": ["+# Title", "+body"]}],
                                     "numbered": False, "truncated": False, "offset": 900, "n": 0}}),
            _call("Edit", {"verb": "edited", "path": "src/gone.py", "added": 1, "removed": 1},
                  result="String to replace not found in file.", error=True,
                  summary="src/gone.py"),
            _call("Grep", None, result="src/pkg/module.py:3:keep", summary="keep"),
            {"kind": "assistant", "timestamp": "2026-09-30T10:00:05Z",
             "text": "Done.", "truncated": False, "sidechain": False},
        ],
    }


def _mock(page: Page, diff_calls: list) -> None:
    _mock_git_status(page)
    _mock_sessions_list(page, [_session_row(_SID, kind="remote", agent="claude", title="Diff demo")])
    body = _page_body()
    page.route(
        re.compile(r".*/api/claude-code/sessions/" + re.escape(_SID) + r"/transcript(\?.*)?$"),
        lambda route: route.fulfill(status=200, content_type="application/json",
                                    body=_json.dumps(body)),
    )

    def _diff(route):
        diff_calls.append(route.request.url)
        route.fulfill(status=200, content_type="application/json", body=_json.dumps({
            "available": True, "reason": None, "session_id": _SID,
            "path": "src/pkg/module.py", "diff": _FULL_DIFF,
        }))

    page.route(
        re.compile(r".*/api/claude-code/sessions/" + re.escape(_SID) + r"/transcript/diff\?.*"),
        _diff,
    )


def _open_items(page: Page):
    _row(page, _SID).locator(".session-open").click()
    expect(page.locator("#terminalOverlay")).to_be_visible(timeout=OVERLAY_OPEN_MS)
    expect(page.locator("#transcriptList .tr-turn")).not_to_have_count(0)
    page.locator("#terminalMenu").click()
    menu = page.locator("#terminalOverlay .terminal-menu")
    expect(menu).to_be_visible()
    menu.get_by_role("menuitem", name="Show tool calls and system entries").click()
    group = page.locator("#transcriptList .tr-group")
    expect(group).to_be_visible()
    group.locator("summary.collapse-summary").click()
    items = group.locator(".tr-item")
    expect(items).to_have_count(4)
    return items


_MEASURE = """
(() => {
  const box = document.querySelector('#transcriptList .tr-item[open] .tr-diff');
  if (!box || !box.clientWidth) return null;
  const de = document.documentElement;
  const pane = document.getElementById('transcriptBody');
  return {scroll: box.scrollWidth, client: box.clientWidth,
          pane: pane.scrollWidth - pane.clientWidth,
          doc: de.scrollWidth - de.clientWidth};
})()
"""


def test_edit_step_opens_as_a_numbered_diff_with_full_fetch(
    authed_page: Page, base_url: str
) -> None:
    diff_calls: list = []
    _mock(authed_page, diff_calls)
    authed_page.set_viewport_size({"width": 390, "height": 844})
    authed_page.goto(base_url, wait_until="domcontentloaded")
    items = _open_items(authed_page)
    edit, write, failed, grep = (items.nth(i) for i in range(4))

    # The folded row still reads name + counts, exactly as before.
    expect(edit.locator(".tr-item-name")).to_have_text("module.py")
    expect(edit.locator(".tr-item-hint")).to_have_text("+8 −1")

    edit.locator("summary").click()
    diff = edit.locator(".tr-diff")
    expect(diff).to_be_visible()
    expect(edit.locator(".tr-diff-path")).to_have_text("src/pkg/module.py")
    expect(diff.locator(".d-hunk")).to_have_text("@@ -40,2 +40,4 @@")
    expect(diff.locator(".d-del .d-ln")).to_have_text("41")
    expect(diff.locator(".d-add .d-ln").first).to_have_text("41")
    expect(diff.locator(".d-del .d-txt")).to_have_text("-old line")
    # The result stays, underneath and quiet.
    expect(edit.locator(".tr-diff-result")).to_contain_text("updated successfully")

    # Tinted from the --diff-* tokens, in both themes, and the two differ.
    seen = {}
    for theme in ("light", "dark"):
        authed_page.evaluate(f"document.documentElement.dataset.theme = '{theme}'")
        add = diff.locator(".d-add").first
        expect(add).not_to_have_css("background-color", "rgba(0, 0, 0, 0)")
        expect(diff.locator(".d-del")).not_to_have_css("background-color", "rgba(0, 0, 0, 0)")
        seen[theme] = add.evaluate("el => getComputedStyle(el).backgroundColor")
    assert seen["light"] != seen["dark"], seen
    expect(diff.locator(".d-add").first).to_have_css("background-color", "rgba(46, 160, 67, 0.15)")
    authed_page.evaluate("document.documentElement.dataset.theme = 'light'")
    expect(diff.locator(".d-add").first).to_have_css("background-color", "rgb(218, 251, 225)")

    # A long line wraps inside the box: nothing to pan, in the diff or page.
    m = stable_read(lambda: authed_page.evaluate(_MEASURE))
    assert m is not None, "no open diff found"
    assert m["scroll"] <= m["client"], f"diff scrolls sideways: {m}"
    assert m["pane"] <= 0 and m["doc"] <= 0, f"page scrolls sideways: {m}"

    # The capped diff offers the rest, at the fleet tap floor.
    more = edit.locator(".tr-diff-more")
    expect(more).to_have_text("Show full diff")
    box = stable_read(lambda: more.bounding_box())
    assert box["height"] >= 44, f"Show full diff under the 44px floor: {box}"
    more.click()
    expect(diff.locator(".d-add")).to_have_count(9)
    expect(edit.locator(".tr-diff-more")).to_have_count(0)
    assert len(diff_calls) == 1 and "offset=512" in diff_calls[0] and "n=0" in diff_calls[0], diff_calls

    # A Write worked out from its input: all added, and no made-up numbers.
    write.locator("summary").click()
    wdiff = write.locator(".tr-diff")
    expect(wdiff.locator(".d-add")).to_have_count(2)
    expect(wdiff.locator(".d-del")).to_have_count(0)
    expect(wdiff.locator(".d-ln")).to_have_count(0)
    expect(wdiff.locator(".d-hunk")).to_have_count(0)
    expect(write.locator(".tr-diff-more")).to_have_count(0)

    # A failed edit and an unrecognised tool keep today's body.
    failed.locator("summary").click()
    expect(failed.locator(".tr-diff")).to_have_count(0)
    expect(failed.locator(".tr-pre").first).to_have_text("src/gone.py")
    grep.locator("summary").click()
    expect(grep.locator(".tr-diff")).to_have_count(0)
    expect(grep.locator(".tr-pre").first).to_have_text("keep")
