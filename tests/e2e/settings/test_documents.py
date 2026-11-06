"""Settings > Documents: the fleet's documents, read on the phone.

Hermetic: both document routes are mocked with invented documents, so no test
reads a real repo. The Mermaid build is the real vendored one, served by the
app, so a diagram that draws here draws on the phone. Covered: the group lists
the configured documents (and hides when none is configured, or says the list
could not be read, with Retry); a tap opens the reading sheet with the
Markdown rendered, a table in its own scroller and a Mermaid block drawn as an
SVG; a block that will not parse keeps its code listing and a one-line note;
the diagram follows the light and dark theme; nothing scrolls the page
sideways at 390px; a document that cannot be read says why and Retry works;
and the Changed files Preview draws the same diagrams through the same
renderer.
"""

from __future__ import annotations

import json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.fixtures.ui import open_settings, open_settings_sheet
from tests.e2e.session.test_changed_file_view import _as_phone, _mock_file_routes, _open_file
from tests.e2e.session.test_session_changed_files import _mock

pytestmark = pytest.mark.smoke

_DOC = """# Sample guide

An intro paragraph with `inline code`.

<!-- generated marker: not for readers -->

| Name | Role | Owner | Notes |
|---|---|---|---|
| alpha | web | team-a | an_unbreakable_cell_value_wide_enough_to_push_the_table_past_a_phone |
| beta | job | team-b | another_unbreakable_cell_value_so_the_table_needs_its_own_scroll_box |

```mermaid
flowchart LR
  A[Start] --> B[Middle]
  B --> C[End]
```

```mermaid
this is not a diagram ((
```

Closing paragraph.
"""
# Once the diagrams are drawn the document above is shorter than a phone-height
# reader, so the scroll test needs a body that overflows in its final state.
_DOC += "".join(f"\nFiller paragraph {n} so the reader has something to scroll.\n" for n in range(1, 41))

_GITHUB = "https://github.com/acme/alpha-repo/blob/HEAD/docs/guide.md"

_LIST = [
    {"id": "guide", "title": "Sample guide", "repo": "alpha-repo",
     "updated": "2020-01-01T00:00:00+00:00"},
    {"id": "gone", "title": "Missing file", "repo": "beta-repo", "updated": None},
]


def _json(route, status: int, body) -> None:
    route.fulfill(status=status, content_type="application/json", body=json.dumps(body))


def _mock_documents(page: Page, *, listing=None, doc_status: int = 200, doc_detail: str = "",
                    github_url: str | None = _GITHUB) -> list:
    """Route-mock the two document routes; returns the document ids requested."""
    asked: list = []
    rows = _LIST if listing is None else listing

    def _list(route):
        _json(route, 200, {"documents": rows})

    def _one(route):
        asked.append(route.request.url.rsplit("/", 1)[1])
        if doc_status != 200:
            _json(route, doc_status, {"detail": doc_detail})
            return
        _json(route, 200, {
            "id": "guide", "title": "Sample guide", "repo": "alpha-repo",
            "updated": "2020-01-01T00:00:00+00:00", "content": _DOC.replace("\n", "\r\n"), "bytes": len(_DOC),
            "github_url": github_url,
        })

    page.route(re.compile(r".*/api/documents$"), _list)
    page.route(re.compile(r".*/api/documents/[^/]+$"), _one)
    return asked


def _open_sheet(page: Page, base_url: str) -> None:
    page.goto(base_url, wait_until="domcontentloaded")
    open_settings(page)
    page.locator("#documentsGroup [data-doc-id='guide']").click()
    expect(page.locator("#documentSheet")).to_be_visible()


_SIDEWAYS = """
() => ({
  doc: document.documentElement.scrollWidth - document.documentElement.clientWidth,
  sheet: (b => b.scrollWidth - b.clientWidth)(document.querySelector('#documentSheet .settings-sheet-body')),
})
"""


@pytest.mark.iphone
def test_group_lists_the_documents_and_a_tap_opens_one_rendered(authed_page: Page, base_url: str) -> None:
    page = authed_page
    asked = _mock_documents(page)
    page.goto(base_url, wait_until="domcontentloaded")
    open_settings(page)

    rows = page.locator("#documentsGroup .settings-row")
    expect(rows).to_have_count(2)
    expect(rows.first.locator(".settings-row-label")).to_have_text("Sample guide")
    expect(rows.first.locator(".settings-row-sub")).to_contain_text("alpha-repo · Updated")
    expect(rows.nth(1).locator(".settings-row-sub")).to_have_text("beta-repo · Unavailable")
    # Every row carries the one constant tracking id, never one per document.
    expect(page.locator("#documentsGroup [data-track='settings.document-open']")).to_have_count(2)
    expect(page.locator("#paneSettings .settings-overline", has_text="Documents")).to_be_visible()

    rows.first.click()
    sheet = page.locator("#documentSheet")
    expect(sheet).to_be_visible()
    expect(page.locator("#documentSheetTitle")).to_have_text("Sample guide")
    body = page.locator("#documentBody")
    expect(body.locator("h1")).to_have_text("Sample guide")
    expect(body).not_to_contain_text("generated marker")
    expect(body.locator(".md-table-wrap table")).to_be_visible()
    # One block drew; the other kept its listing and gained a note.
    expect(body.locator(".md-diagram svg")).to_have_count(1)
    expect(body.locator(".md-diagram")).to_have_attribute("role", "img")
    expect(body.locator(".md-block")).to_have_count(1)
    expect(body.locator(".md-diagram-note")).to_have_text("Diagram couldn't be drawn")
    expect(page.locator("#documentUpdated")).to_contain_text("alpha-repo")
    assert asked == ["guide"]

    fit = page.evaluate(_SIDEWAYS)
    assert fit["doc"] <= 0 and fit["sheet"] <= 0, f"the page scrolls sideways at 390px: {fit}"
    # On the phone the wide table scrolls inside its own box (a desktop
    # window is wide enough that it needs none).
    if page.viewport_size["width"] <= 500:
        wrap = page.evaluate(
            "() => { const w = document.querySelector('#documentBody .md-table-wrap');"
            " return w.scrollWidth > w.clientWidth; }")
        assert wrap, "the table should need its own scroller at 390px"

    # Back returns to Settings; the next open fetches afresh.
    sheet.locator("[data-sheet-back]").click()
    expect(sheet).to_be_hidden()
    expect(page.locator("#documentBody")).to_be_empty()


@pytest.mark.iphone
def test_the_reader_pins_its_header_and_fills_the_phone(authed_page: Page, base_url: str) -> None:
    page = authed_page
    _mock_documents(page)
    _open_sheet(page, base_url)
    sheet = page.locator("#documentSheet")
    head = sheet.locator(".detail-header")
    # The sheet opens before its fetch lands and Mermaid redraws nodes after the
    # content does: scrolling earlier reads 0 because there is nothing to scroll.
    expect(page.locator("#documentBody h1")).to_have_text("Sample guide")
    expect(page.locator("#documentBody .md-diagram svg")).to_have_count(1)
    page.wait_for_function(
        "() => { const b = document.querySelector('#documentSheet .document-sheet-body');"
        " return b.scrollHeight > b.clientHeight; }")
    for theme in ("light", "dark"):
        page.evaluate("t => document.documentElement.setAttribute('data-theme', t)", theme)
        if page.viewport_size["width"] <= 520:
            box = sheet.bounding_box()
            assert box["width"] >= page.viewport_size["width"] - 1, f"the sheet is inset ({theme}): {box}"
            assert box["height"] >= page.viewport_size["height"] - 1, f"the sheet is inset ({theme}): {box}"
        # The body scrolls on its own and the header does not move with it.
        top = head.bounding_box()["y"]
        page.evaluate("document.querySelector('#documentSheet .document-sheet-body').scrollTop = 400")
        assert page.evaluate("document.querySelector('#documentSheet .document-sheet-body').scrollTop") > 0
        assert head.bounding_box()["y"] == top
        page.evaluate("document.querySelector('#documentSheet .document-sheet-body').scrollTop = 0")
        h1 = page.locator("#documentBody h1")
        assert h1.evaluate("e => getComputedStyle(e).borderBottomWidth") == "1px"
        expect(page.locator("#documentBody code").first).to_have_css("border-radius", "6px")
    page.evaluate("document.documentElement.setAttribute('data-theme', 'light')")


def test_open_on_github_opens_the_documents_page_and_hides_without_a_remote(
    authed_page: Page, base_url: str
) -> None:
    page = authed_page
    _mock_documents(page)
    _open_sheet(page, base_url)
    button = page.locator("#documentGithub")
    expect(button).to_be_visible()
    expect(button).to_have_attribute("data-track", "document.github")
    # The popup lands on a stub: a test never reaches the real github.com.
    page.context.route("https://github.com/**", lambda r: r.fulfill(status=200, body="stub"))
    with page.expect_popup() as popup:
        button.click()
    assert popup.value.url.startswith("https://github.com/acme/alpha-repo/blob/HEAD/docs/guide.md")
    popup.value.close()

    page.unroute_all()
    _mock_documents(page, github_url=None)
    page.locator("#documentSheet [data-sheet-back]").click()
    page.locator("#documentsGroup [data-doc-id='guide']").click()
    expect(page.locator("#documentBody h1")).to_have_text("Sample guide")
    expect(button).to_be_hidden()


@pytest.mark.iphone
def test_the_diagram_follows_the_theme(authed_page: Page, base_url: str) -> None:
    page = authed_page
    _mock_documents(page)
    _open_sheet(page, base_url)
    node = page.locator("#documentBody .md-diagram svg .node rect").first
    for theme, fill in (("light", "rgb(246, 248, 250)"), ("dark", "rgb(33, 38, 45)")):
        page.evaluate("t => document.documentElement.setAttribute('data-theme', t)", theme)
        expect(node).to_have_css("fill", fill)
    page.evaluate("document.documentElement.setAttribute('data-theme', 'light')")


def test_no_documents_hides_the_group_and_an_unread_list_says_so(authed_page: Page, base_url: str) -> None:
    page = authed_page
    _mock_documents(page, listing=[])
    page.goto(base_url, wait_until="domcontentloaded")
    open_settings(page)
    expect(page.locator("#paneSettings .settings-overline", has_text="Activity")).to_be_visible()
    expect(page.locator("#documentsSection")).to_be_hidden()

    page.unroute_all()
    fail = {"on": True}

    def _list(route):
        if fail["on"]:
            _json(route, 500, {"detail": "boom"})
        else:
            _json(route, 200, {"documents": _LIST})

    page.route(re.compile(r".*/api/documents$"), _list)
    page.reload(wait_until="domcontentloaded")
    open_settings(page)
    expect(page.locator("#documentsError")).to_be_visible()
    expect(page.locator("#documentsError")).to_contain_text("Couldn't read the documents list")
    fail["on"] = False
    page.locator("#documentsRetry").click()
    expect(page.locator("#documentsGroup .settings-row")).to_have_count(2)
    expect(page.locator("#documentsError")).to_be_hidden()


def test_a_document_that_cannot_be_read_says_why_and_retry_works(authed_page: Page, base_url: str) -> None:
    page = authed_page
    _mock_documents(page, doc_status=404, doc_detail="file not found")
    _open_sheet(page, base_url)
    state = page.locator("#documentState")
    expect(state).to_be_visible()
    expect(page.locator("#documentStateMessage")).to_have_text("Couldn't read this document: file not found")
    expect(page.locator("#documentBody")).to_be_empty()

    page.unroute_all()
    _mock_documents(page)
    page.locator("#documentRetry").click()
    expect(state).to_be_hidden()
    expect(page.locator("#documentBody h1")).to_have_text("Sample guide")


@pytest.mark.iphone
def test_changed_files_preview_draws_the_same_diagrams(authed_page: Page, base_url: str) -> None:
    page = authed_page
    _mock(page)
    _mock_file_routes(page)
    _as_phone(page)

    def _content(route):
        route.fulfill(status=200, content_type="application/json", body=json.dumps({
            "session_id": "x", "available": True, "reason": None,
            "path": "E:\\work\\proj\\docs\\notes.md", "text": _DOC,
        }))

    page.route(re.compile(r".*/changed-files/content\?.*"), _content)
    row = _open_file(page, base_url, 0)
    row.locator(".chg-kebab").click()
    page.locator("[data-item='chg-preview']").click()
    viewer = page.locator("#htmlViewer .html-viewer-md")
    expect(viewer.locator(".md-diagram svg")).to_have_count(1)
    expect(viewer.locator(".md-diagram-note")).to_have_text("Diagram couldn't be drawn")
    expect(viewer.locator("h1")).to_have_text("Sample guide")


def test_chief_sheet_opens_the_grok_bot_directive_and_copies_the_raw_file(
    authed_page: Page, base_url: str
) -> None:
    page = authed_page
    asked = _mock_documents(page)
    # iOS and Playwright's WebKit gate the real clipboard; the contract here is
    # the text handed to writeText, so capture it in both engines.
    page.add_init_script(
        "window.__copied = [];"
        "Object.defineProperty(navigator, 'clipboard', { configurable: true, value: {"
        " writeText: t => { window.__copied.push(t); return Promise.resolve(); } } });"
    )
    page.goto(base_url, wait_until="domcontentloaded")
    open_settings_sheet(page, "chiefSheet")

    opener = page.locator("#chiefSheet [data-open-document='grok-bot-skill-pack']")
    expect(opener).to_have_attribute("data-track", "chief.grokbot-directive")
    opener.click()
    sheet = page.locator("#documentSheet")
    expect(sheet).to_be_visible()
    expect(page.locator("#documentBody h1")).to_have_text("Sample guide")
    assert asked == ["grok-bot-skill-pack"]
    expect(page.locator("#documentUpdated")).to_contain_text("Updated")

    copy = page.locator("#documentCopy")
    expect(copy).to_be_visible()
    copy.click()
    expect(page.locator("#toast")).to_contain_text("Markdown copied")
    assert page.evaluate("window.__copied") == [_DOC.replace("\n", "\r\n")]

    # Back returns to the Chief sheet it was opened from, and the next open
    # starts without a stale copy button.
    sheet.locator("[data-sheet-back]").click()
    expect(sheet).to_be_hidden()
    expect(page.locator("#chiefSheet")).to_be_visible()
    expect(copy).to_be_hidden()
