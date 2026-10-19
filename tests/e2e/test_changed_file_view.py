"""#1689 — a changed file's File view, Copy path and Open on PC.

In a session's Changed files panel, an opened file shows a Diff | File switch
(Diff the default), its absolute path as selectable text, Copy path and, on
the PC itself only, Open on PC. Pinned against stubbed routes (every path and
line synthetic):

* on the phone: File renders the file for its type (Markdown here) through a
  content request made once, Diff comes back without a refetch, Copy path puts
  the Windows path on the clipboard with a "Path copied" toast, and there is
  no Open on PC; the tool row meets the 44px floor and nothing scrolls
  sideways at 390px, light and dark;
* on the PC (a loopback page): Open on PC posts the file's path and says what
  happened; a code file gets a line-number gutter, and a file gone from disk
  says so instead of an empty view.

Geometry goes through ``stable_eval`` (#680/#1346): the session view
re-renders on its own poll.
"""
from __future__ import annotations

import json as _json
import re
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import stable_eval
from tests.e2e.test_jobs_log_copy import _CLIPBOARD_MOCK
from tests.e2e.test_session_changed_files import _SID, _menu, _mock, _open_session

pytestmark = pytest.mark.smoke

_NOTES = "# Notes\n\nOne **bold** line.\n\n<script>window.__ran = true</script>\n"
_CODE = "def f():\n    return 1\n\n\nprint(f())\n"


def _mock_file_routes(page: Page) -> dict:
    seen: dict = {"content": [], "open": []}

    def _content(route):
        path = parse_qs(urlparse(route.request.url).query)["path"][0]
        seen["content"].append(path)
        base = {"session_id": _SID}
        if path.endswith("notes.md"):
            body = {**base, "available": True, "reason": None,
                    "path": path.replace("/", "\\"), "text": _NOTES}
        elif path.endswith("long.py"):
            body = {**base, "available": True, "reason": None,
                    "path": path.replace("/", "\\"), "text": _CODE}
        else:
            body = {**base, "available": False, "reason": "deleted"}
        route.fulfill(status=200, content_type="application/json", body=_json.dumps(body))

    def _open(route):
        seen["open"].append(_json.loads(route.request.post_data or "{}"))
        route.fulfill(status=200, content_type="application/json", body=_json.dumps(
            {"available": True, "reason": None, "session_id": _SID, "opened": True, "with": "vscode"}))

    page.route(re.compile(r".*/api/claude-code/sessions/" + re.escape(_SID) + r"/changed-files/content\?.*"),
               _content)
    page.route(re.compile(r".*/api/claude-code/sessions/" + re.escape(_SID) + r"/changed-files/open$"), _open)
    return seen


def _as_phone(page: Page) -> None:
    """The page as the phone sees it: not the PC talking to itself."""
    def _status(route):
        resp = route.fetch()
        body = resp.json()
        body["terminal"] = {"reachable": True, "reason": None}
        route.fulfill(response=resp, body=_json.dumps(body), content_type="application/json")
    page.route(re.compile(r".*/api/status$"), _status)


def _open_file(page: Page, base_url: str, nth: int):
    _open_session(page, base_url)
    _menu(page).get_by_role("menuitem", name="Changed files").click()
    row = page.locator("#changesList .chg-file").nth(nth)
    row.locator("summary").click()
    expect(row.locator(".chg-tools")).to_be_visible()
    return row


# The tool row's targets: each segment's band and each icon button's invisible
# square reach 44px, probed with elementFromPoint just inside the floor, and
# nothing scrolls sideways.
_TOOLS_FIT = """
(row) => {
  const hit = (el, dx, dy) => {
    const r = el.getBoundingClientRect();
    const t = document.elementFromPoint(r.left + r.width / 2 + dx, r.top + r.height / 2 + dy);
    return !!t && el.contains(t);
  };
  const body = document.getElementById('changesBody');
  const segs = [...row.querySelectorAll('.chg-view .segmented-item')];
  const copy = row.querySelector('.chg-copy');
  return {
    segH: Math.min(...segs.map(s => s.getBoundingClientRect().height)),
    segBand: segs.every(s => hit(s, 0, 21) && hit(s, 0, -21)),
    copyBand: hit(copy, 21, 0) && hit(copy, -21, 0) && hit(copy, 0, 21) && hit(copy, 0, -21),
    body: body.scrollWidth - body.clientWidth,
    doc: document.documentElement.scrollWidth - document.documentElement.clientWidth,
  };
}
"""


@pytest.mark.iphone
def test_file_view_and_copy_path_on_the_phone(authed_page: Page, base_url: str) -> None:
    page = authed_page
    _mock(page)
    seen = _mock_file_routes(page)
    _as_phone(page)
    page.add_init_script(_CLIPBOARD_MOCK)
    row = _open_file(page, base_url, 0)

    segs = row.locator(".chg-view .segmented-item")
    expect(segs).to_have_text(["Diff", "File"])
    expect(segs.first).to_have_attribute("aria-pressed", "true")
    expect(row.locator(".chg-pane-diff .chg-step")).to_have_count(2)
    expect(row.locator(".chg-fullpath")).to_have_text("E:\\work\\proj\\docs\\notes.md")
    expect(row.locator(".chg-open")).to_have_count(0)
    assert seen["content"] == []

    segs.nth(1).click()
    expect(segs.nth(1)).to_have_attribute("aria-pressed", "true")
    expect(row.locator(".chg-pane-diff")).to_be_hidden()
    md = row.locator(".chg-pane-file .fv-md")
    expect(md.locator("h1")).to_have_text("Notes")
    expect(md.locator("strong")).to_have_text("bold")
    # The file's own <script> is text on the page, never run.
    expect(md).to_contain_text("<script>window.__ran = true</script>")
    assert page.evaluate("() => window.__ran === undefined")
    assert seen["content"] == ["E:/work/proj/docs/notes.md"], seen

    segs.first.click()
    expect(row.locator(".chg-pane-diff")).to_be_visible()
    expect(row.locator(".chg-pane-file")).to_be_hidden()
    segs.nth(1).click()
    expect(md.locator("h1")).to_be_visible()
    assert len(seen["content"]) == 1, f"switching back refetched the file: {seen}"

    row.locator(".chg-copy").click()
    expect(page.locator("#toast")).to_contain_text("Path copied")
    assert page.evaluate("() => window.__copied") == ["E:\\work\\proj\\docs\\notes.md"]

    for theme in ("light", "dark"):
        page.evaluate("t => document.documentElement.setAttribute('data-theme', t)", theme)
        fit = stable_eval(row, _TOOLS_FIT)
        assert fit["segBand"] and fit["copyBand"], f"a tool target is under 44px ({theme}): {fit}"
        assert fit["body"] <= 0 and fit["doc"] <= 0, f"sideways scroll at 390px ({theme}): {fit}"
    page.evaluate("document.documentElement.setAttribute('data-theme', 'light')")


def test_open_on_pc_code_gutter_and_a_deleted_file_on_the_pc(authed_page: Page, base_url: str) -> None:
    page = authed_page
    _mock(page)
    seen = _mock_file_routes(page)
    row = _open_file(page, base_url, 1)

    # This page talks to the webapp over loopback: it is the PC itself.
    row.locator(".chg-open").click()
    expect(page.locator("#toast")).to_contain_text("Opened in VS Code")
    assert seen["open"] == [{"path": "E:/work/proj/src/pkg/long.py"}], seen

    row.locator(".chg-view .segmented-item").nth(1).click()
    lines = row.locator(".chg-pane-file .fv-line")
    expect(lines).to_have_count(5)
    expect(lines.nth(1)).to_have_text("    return 1")
    expect(row.locator(".chg-pane-file .fv-key")).to_have_count(0)   # code is not tinted

    gone = page.locator("#changesList .chg-file").nth(2)
    gone.locator("summary").click()
    gone.locator(".chg-view .segmented-item").nth(1).click()
    expect(gone.locator(".chg-pane-file")).to_have_text("This file is no longer on disk")
