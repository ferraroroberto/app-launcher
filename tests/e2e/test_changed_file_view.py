"""#1689, #1749 — a changed file's File view, Download and ⋮ menu.

In a session's Changed files panel, an opened file shows a Diff | File switch
(Diff the default), its absolute path as selectable text, a Download button
and a ⋮ menu (Copy content in File view, Copy diff in Diff view, Copy path
and, on the PC itself only, Open on PC). Pinned against stubbed routes (every
path and line synthetic):

* on the phone: File renders the file for its type (Markdown here) through a
  content request made once, Diff comes back without a refetch, the menu opens
  over the panel and its item set follows the view, Copy content / Copy diff
  put exactly the raw file / the patch on the clipboard, Copy path puts the
  Windows path there, and there is no Open on PC; the tool row meets the 44px
  floor and nothing scrolls sideways at 390px, light and dark;
* Download saves the file under its own name with its text, and a file the
  File view can't read is refused with the File view's sentence, with no
  second request for a file already shown;
* the File view's token colours (#1749): a Python file's keyword, string,
  number, comment and name take the fleet's text tokens in light and dark, its
  gutter still counts one per line, and a JSON key sits on the same row as its
  value (the gutter used to be a grid that put each span on its own row);
* on the PC (a loopback page): Open on PC, from the menu, posts the file's
  path and says what happened; a code file gets a line-number gutter, and a
  file gone from disk says so instead of an empty view.

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
# iOS lets a page write the clipboard only inside the user's tap. Every click
# listener runs inside a flag that is up only while its synchronous body runs,
# so a writeText that follows any `await` in the handler (the continuation runs
# after the listener returned) is recorded false.
_TAP_SYNC_MOCK = """
(() => {
  window.__tapSync = [];
  let inTap = false;
  const wrapped = new WeakMap();
  const add = EventTarget.prototype.addEventListener;
  EventTarget.prototype.addEventListener = function (type, fn, opts) {
    if (type === 'click' && typeof fn === 'function') {
      if (!wrapped.has(fn)) {
        wrapped.set(fn, function (ev) {
          const was = inTap;
          inTap = true;
          try { return fn.call(this, ev); } finally { inTap = was; }
        });
      }
      return add.call(this, type, wrapped.get(fn), opts);
    }
    return add.call(this, type, fn, opts);
  };
  const write = navigator.clipboard.writeText;
  navigator.clipboard.writeText = (t) => { window.__tapSync.push(inTap); return write(t); };
})()
"""
# What Copy diff puts on the clipboard for notes.md: both steps, as drawn.
_NOTES_DIFF = "@@ -0,0 +1,3 @@\n+# Notes\n+one\n+two\n@@ -3,1 +3,3 @@\n two\n+three\n+four"


_TINT_PY = '# say hi\ndef greet(name):\n    return "hi" + name  # tail\n'
_TINT_JSON = '{"a": 1, "b": "x", "ok": true}'
_TINT_FILES = {
    "available": True, "reason": None, "session_id": _SID, "source": "transcript",
    "partial": False, "project_exists": True, "counts": {"additions": 4, "deletions": 0},
    "files": [
        {"path": "tool.py", "key": "E:/work/proj/tool.py", "status": "M",
         "additions": 3, "deletions": 0, "steps": 1},
        {"path": "data.json", "key": "E:/work/proj/data.json", "status": "M",
         "additions": 1, "deletions": 0, "steps": 1},
    ],
}


def _mock_file_routes(page: Page) -> dict:
    seen: dict = {"content": [], "open": []}

    def _content(route):
        path = parse_qs(urlparse(route.request.url).query)["path"][0]
        seen["content"].append(path)
        base = {"session_id": _SID}
        if path.endswith("notes.md"):
            body = {**base, "available": True, "reason": None,
                    "path": path.replace("/", "\\"), "text": _NOTES}
        elif path.endswith("tool.py") or path.endswith("data.json"):
            text = _TINT_PY if path.endswith("tool.py") else _TINT_JSON
            body = {**base, "available": True, "reason": None,
                    "path": path.replace("/", "\\"), "text": text}
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
# square (Download, ⋮) reach 44px, probed with elementFromPoint just inside the
# floor, and nothing scrolls sideways.
_TOOLS_FIT = """
(row) => {
  const hit = (el, dx, dy) => {
    const r = el.getBoundingClientRect();
    const t = document.elementFromPoint(r.left + r.width / 2 + dx, r.top + r.height / 2 + dy);
    return !!t && el.contains(t);
  };
  const body = document.getElementById('changesBody');
  const segs = [...row.querySelectorAll('.chg-view .segmented-item')];
  const btns = [...row.querySelectorAll('.chg-acts .icon-button')];
  return {
    segH: Math.min(...segs.map(s => s.getBoundingClientRect().height)),
    segBand: segs.every(s => hit(s, 0, 21) && hit(s, 0, -21)),
    btns: btns.length,
    btnBand: btns.every(b => hit(b, 21, 0) && hit(b, -21, 0) && hit(b, 0, 21) && hit(b, 0, -21)),
    body: body.scrollWidth - body.clientWidth,
    doc: document.documentElement.scrollWidth - document.documentElement.clientWidth,
  };
}
"""


def _open_kebab(page: Page, row):
    """Open the file's ⋮ menu; return it."""
    row.locator(".chg-kebab").click()
    menu = page.locator(".row-menu.changes-menu")
    expect(menu).to_be_visible()
    return menu


def _items(menu) -> list:
    return menu.locator(".row-menu-item .row-menu-label").all_inner_texts()


# The menu paints over the panel and its first item is what a tap reaches.
_MENU_ON_TOP = """
() => {
  const menu = document.querySelector('.row-menu.changes-menu');
  const items = [...menu.querySelectorAll('.row-menu-item')];
  return items.map(it => {
    const r = it.getBoundingClientRect();
    const t = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    return {h: r.height, reached: !!t && it.contains(t)};
  });
}
"""


@pytest.mark.iphone
def test_file_view_download_and_menu_on_the_phone(authed_page: Page, base_url: str) -> None:
    page = authed_page
    _mock(page)
    seen = _mock_file_routes(page)
    _as_phone(page)
    page.add_init_script(_CLIPBOARD_MOCK)
    page.add_init_script(_TAP_SYNC_MOCK)
    row = _open_file(page, base_url, 0)

    # The row holds the switch, Download and ⋮, and nothing else.
    expect(row.locator(".chg-acts .icon-button")).to_have_count(2)
    expect(row.locator(".chg-download")).to_have_attribute("aria-label", "Download")
    expect(row.locator(".chg-kebab")).to_have_attribute("aria-label", "More actions")

    segs = row.locator(".chg-view .segmented-item")
    expect(segs).to_have_text(["Diff", "File"])
    expect(segs.first).to_have_attribute("aria-pressed", "true")
    expect(row.locator(".chg-pane-diff .chg-step")).to_have_count(2)
    expect(row.locator(".chg-fullpath")).to_have_text("E:\\work\\proj\\docs\\notes.md")
    assert seen["content"] == []

    # Diff view: Copy diff and Copy path, no Copy content, no Open on PC (phone).
    menu = _open_kebab(page, row)
    assert _items(menu) == ["Copy diff", "Copy path"], _items(menu)
    for theme in ("light", "dark"):
        page.evaluate("t => document.documentElement.setAttribute('data-theme', t)", theme)
        reach = page.evaluate(_MENU_ON_TOP)
        assert reach and all(i["reached"] and i["h"] >= 44 for i in reach), \
            f"a menu item is covered or under 44px ({theme}): {reach}"
    page.evaluate("document.documentElement.setAttribute('data-theme', 'light')")
    menu.locator("[data-item='chg-copy-diff']").click()
    expect(menu).to_have_count(0)
    expect(page.locator("#toast")).to_contain_text("Diff copied")
    assert page.evaluate("() => window.__copied") == [_NOTES_DIFF]

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

    # File view: Copy content (the raw text, not the rendered HTML) and Copy path.
    menu = _open_kebab(page, row)
    assert _items(menu) == ["Copy content", "Copy path"], _items(menu)
    menu.locator("[data-item='chg-copy-content']").click()
    expect(page.locator("#toast")).to_contain_text("Content copied")
    assert page.evaluate("() => window.__copied")[-1] == _NOTES

    _open_kebab(page, row).locator("[data-item='chg-copy-path']").click()
    expect(page.locator("#toast")).to_contain_text("Path copied")
    assert page.evaluate("() => window.__copied")[-1] == "E:\\work\\proj\\docs\\notes.md"
    assert len(seen["content"]) == 1, f"Copy content refetched the file: {seen}"
    # Every copy wrote the clipboard inside its tap, with no await before it.
    assert page.evaluate("() => window.__tapSync") == [True, True, True]

    for theme in ("light", "dark"):
        page.evaluate("t => document.documentElement.setAttribute('data-theme', t)", theme)
        fit = stable_eval(row, _TOOLS_FIT)
        assert fit["btns"] == 2 and fit["segBand"] and fit["btnBand"], \
            f"a tool target is under 44px ({theme}): {fit}"
        assert fit["body"] <= 0 and fit["doc"] <= 0, f"sideways scroll at 390px ({theme}): {fit}"
    page.evaluate("document.documentElement.setAttribute('data-theme', 'light')")


def test_download_saves_the_file_and_refuses_what_the_file_view_refuses(
    authed_page: Page, base_url: str
) -> None:
    page = authed_page
    _mock(page)
    seen = _mock_file_routes(page)
    row = _open_file(page, base_url, 1)

    with page.expect_download() as got:
        row.locator(".chg-download").click()
    download = got.value
    assert download.suggested_filename == "long.py"
    with open(download.path(), encoding="utf-8") as fh:
        assert fh.read() == _CODE
    assert seen["content"] == ["E:/work/proj/src/pkg/long.py"], seen

    # The File view shows the file already read: no second request.
    row.locator(".chg-view .segmented-item").nth(1).click()
    expect(row.locator(".chg-pane-file .fv-line")).to_have_count(5)
    assert len(seen["content"]) == 1, f"the File pane refetched a downloaded file: {seen}"

    gone = page.locator("#changesList .chg-file").nth(2)
    gone.locator("summary").click()
    gone.locator(".chg-download").click()
    expect(page.locator("#toast")).to_have_text("This file is no longer on disk")


def test_open_on_pc_code_gutter_and_a_deleted_file_on_the_pc(authed_page: Page, base_url: str) -> None:
    page = authed_page
    _mock(page)
    seen = _mock_file_routes(page)
    row = _open_file(page, base_url, 1)

    # This page talks to the webapp over loopback: it is the PC itself.
    menu = _open_kebab(page, row)
    assert "Open on PC" in _items(menu), _items(menu)
    menu.locator("[data-item='chg-open-pc']").click()
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


def _tinted_file_view(page: Page, base_url: str, nth: int):
    """Open the nth file of the tint fixture on its File view."""
    _mock(page, files=_TINT_FILES)
    _mock_file_routes(page)
    row = _open_file(page, base_url, nth)
    row.locator(".chg-view .segmented-item").nth(1).click()
    expect(row.locator(".chg-pane-file .fv-line").first).to_be_visible()
    return row


# What each class resolves to, per theme: the fleet's existing text tokens
# (accent-text / success-text / attention-text / muted / code-fg).
_TINT_COLOURS = {
    "light": {"fv-kw": "rgb(5, 80, 174)", "fv-str": "rgb(17, 99, 41)", "fv-num": "rgb(125, 78, 0)"},
    "dark": {"fv-kw": "rgb(88, 166, 255)", "fv-str": "rgb(86, 211, 100)", "fv-num": "rgb(227, 179, 65)"},
}


def test_code_file_takes_token_colours_in_both_themes(authed_page: Page, base_url: str) -> None:
    page = authed_page
    row = _tinted_file_view(page, base_url, 0)
    pane = row.locator(".chg-pane-file")

    expect(pane.locator(".fv-line")).to_have_count(3)
    expect(pane.locator(".fv-cmt").first).to_have_text("# say hi")
    expect(pane.locator(".fv-kw").first).to_have_text("def")
    expect(pane.locator(".fv-name").first).to_have_text("greet")
    expect(pane.locator(".fv-str")).to_have_text('"hi"')
    expect(pane.locator(".fv-cmt").nth(1)).to_have_text("# tail")
    # Only the colour classes carry markup: the listing's text is the source.
    expect(pane.locator(".fv-listing")).to_have_text("# say hi" + "def greet(name):" + '    return "hi" + name  # tail')

    for theme, colours in _TINT_COLOURS.items():
        page.evaluate("t => document.documentElement.setAttribute('data-theme', t)", theme)
        expect(pane.locator(".fv-kw").first).to_have_css("color", colours["fv-kw"])
        expect(pane.locator(".fv-str").first).to_have_css("color", colours["fv-str"])
        expect(pane.locator(".fv-cmt").first).to_have_css("font-style", "italic")
        expect(pane.locator(".fv-name").first).to_have_css("font-weight", "600")
        # A name has no hue of its own: it is the listing's own text colour.
        expect(pane.locator(".fv-name").first).to_have_css(
            "color", page.evaluate("() => getComputedStyle(document.querySelector('.fv-listing')).color"))
    page.evaluate("document.documentElement.setAttribute('data-theme', 'light')")

    # JSON keeps its tint; its number and literal take the attention colour.
    row2 = page.locator("#changesList .chg-file").nth(1)
    row2.locator("summary").click()
    row2.locator(".chg-view .segmented-item").nth(1).click()
    expect(row2.locator(".chg-pane-file .fv-num").first).to_have_text("1")
    for theme, colours in _TINT_COLOURS.items():
        page.evaluate("t => document.documentElement.setAttribute('data-theme', t)", theme)
        expect(row2.locator(".chg-pane-file .fv-num").first).to_have_css("color", colours["fv-num"])
    page.evaluate("document.documentElement.setAttribute('data-theme', 'light')")


_ROWS = """
(pane) => {
  const lines = [...pane.querySelectorAll('.fv-line')];
  const key = pane.querySelector('.fv-key');
  const after = key && key.nextElementSibling;
  const first = lines[0].getBoundingClientRect();
  const one = parseFloat(getComputedStyle(lines[0]).lineHeight);
  return {
    n: lines.length,
    tallest: Math.max(...lines.map(l => l.getBoundingClientRect().height)),
    one: one,
    sameRow: !!(key && after) && Math.abs(key.getBoundingClientRect().top - after.getBoundingClientRect().top) < 2,
    padLeft: parseFloat(getComputedStyle(lines[0]).paddingLeft),
    textLeft: lines[0].getBoundingClientRect().left,
    sweep: document.getElementById('changesBody').scrollWidth - document.getElementById('changesBody').clientWidth,
  };
}
"""


@pytest.mark.iphone
def test_file_view_lines_keep_one_row_per_line_with_a_hanging_gutter(
    authed_page: Page, base_url: str
) -> None:
    page = authed_page
    _tinted_file_view(page, base_url, 1)
    pane = page.locator("#changesList .chg-file").nth(1).locator(".chg-pane-file")
    # data.json pretty-printed is five short lines: each is one text row, a key
    # and its value on the same row (the gutter was a two-column grid, so every
    # token span became a grid item and wrapped onto its own row).
    got = stable_eval(pane, _ROWS)
    assert got["n"] == 5, got
    assert got["sameRow"], f"a key and its value are on different rows: {got}"
    assert got["tallest"] <= got["one"] * 1.1, f"a short line wrapped to several rows: {got}"
    assert got["padLeft"] > 40, f"the number has no room in the gutter: {got}"
    assert got["sweep"] <= 0, f"sideways scroll at 390px: {got}"
