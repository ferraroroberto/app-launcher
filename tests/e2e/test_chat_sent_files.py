"""Issue #1530 — files an agent sends show up in the Chat pane.

Two sources, one card: an outbox entry (any harness posts a file to its
session's outbox, and the transcript page carries the list) and a Claude
``SendUserFile`` call (the transcript's own tool call, served through the
same guards). Every non-deterministic boot fetch is stubbed before
``goto()`` (#510); the file routes answer with a synthetic PNG, or with the
server's refusal, so the test pins what the pane does with each.
"""

from __future__ import annotations

import base64
import json as _json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import open_session_row, stub_session_mirror

pytestmark = pytest.mark.smoke

_SID = "sid-sent-files-1530"

# A real 1x1 PNG: synthetic image bytes only.
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)
_CAPTION = "<img src=x onerror=window.__pwned=1> the chart"

_PAGE = {
    "available": True, "source": "codex", "reason": None, "session_id": _SID,
    "next_cursor": None, "tail": None, "size": 10,
    "entries": [
        {"kind": "user", "timestamp": "2026-10-12T10:00:00Z", "offset": 0,
         "text": "send me the chart", "truncated": False, "sidechain": False},
        {"kind": "assistant", "timestamp": "2026-10-12T10:00:10Z", "offset": 5,
         "text": "Sent it.", "truncated": False, "sidechain": False},
        {"kind": "tool_call", "timestamp": "2026-10-12T10:00:20Z", "name": "SendUserFile",
         "summary": "chart.png", "result": "sent", "result_truncated": False,
         "sidechain": False, "caption": "",
         "sent_files": [
             {"offset": 7, "n": 0, "name": "diagram.png", "kind": "image"},
             {"offset": 7, "n": 1, "name": "logo.svg", "kind": None},
         ]},
    ],
    "outbox_rev": 1,
    "outbox": [{
        "id": "a" * 32, "name": "chart.png", "caption": _CAPTION, "kind": "image",
        "media": "image/png", "size": len(_PNG), "timestamp": "2026-10-12T10:00:05Z",
    }],
}


def _stub(page: Page, outbox_calls: list) -> None:
    def sessions(route):
        route.fulfill(status=200, content_type="application/json", body=_json.dumps({"sessions": [{
            "session_id": _SID, "kind": "pty", "agent": "codex",
            "project_dir": "E:/invented/sentfiles", "name": "sentfiles", "alive": True,
            "started_at": "2026-10-12T10:00:00Z", "live_title": "", "prompt_title": "",
            "manual_title": "Sent files demo",
        }]}))

    def transcript(route):
        route.fulfill(status=200, content_type="application/json", body=_json.dumps(_PAGE))

    def outbox_file(route):
        outbox_calls.append(route.request.url)
        route.fulfill(status=200, content_type="image/png", body=_PNG)

    def sent_file(route):
        if "n=0" in route.request.url:
            route.fulfill(status=200, content_type="image/png", body=_PNG)
        else:
            route.fulfill(status=415, content_type="application/json", body=_json.dumps({"detail": {
                "reason": "type_not_allowed",
                "message": "only images (png, jpg, gif, webp), PDF, text, markdown, HTML and zip/gzip/7z archive files can be sent",
            }}))

    base = r".*/api/claude-code/sessions/" + _SID
    page.route(re.compile(r".*/api/claude-code/sessions$"), sessions)
    page.route(re.compile(base + r"/transcript(\?.*)?$"), transcript)
    page.route(re.compile(base + r"/outbox/[a-f0-9]+$"), outbox_file)
    page.route(re.compile(base + r"/transcript/file\?.*"), sent_file)


def test_outbox_and_send_user_file_render_in_the_chat(authed_page: Page, base_url: str) -> None:
    outbox_calls: list = []
    _stub(authed_page, outbox_calls)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    stub_session_mirror(authed_page)
    open_session_row(authed_page, authed_page.locator(f'#sessionsList li[data-session-id="{_SID}"]'), mode="chat")

    items = authed_page.locator("#transcriptList .tr-file-item")
    expect(items).to_have_count(2)
    # The outbox file sits between the prompt and the reply, by its time.
    outbox = items.first
    parts = authed_page.locator("#transcriptList .tr-turn-parts > li")
    expect(parts.first).to_have_class(re.compile(r"\btr-file-item\b"))
    # The caption is text, never markup.
    expect(outbox.locator(".tr-file-caption")).to_have_text(_CAPTION)
    expect(outbox.locator(".tr-file-caption img")).to_have_count(0)
    expect(outbox.locator(".tr-file-name")).to_have_text("chart.png")

    # The outbox image renders inline from the authenticated fetch, and Open
    # carries the same bytes.
    img = outbox.locator(".tr-file-image img")
    expect(img).to_have_attribute("src", re.compile(r"^blob:"))
    authed_page.wait_for_function(
        "el => el.complete && el.naturalWidth > 0", arg=img.element_handle(),
    )
    expect(outbox.locator(".tr-file-open")).to_have_attribute("href", re.compile(r"^blob:"))
    assert len(outbox_calls) == 1 and outbox_calls[0].endswith("/outbox/" + "a" * 32)

    # SendUserFile: the allowed file renders the same way; the refused one
    # says why, in the server's words.
    sent = items.nth(1).locator(".tr-file-card")
    expect(sent).to_have_count(2)
    expect(sent.first.locator(".tr-file-image img")).to_have_attribute("src", re.compile(r"^blob:"))
    expect(sent.nth(1)).to_have_class(re.compile(r"\btr-file-failed\b"))
    expect(sent.nth(1).locator(".tr-file-note")).to_have_text(
        "Not shown: only images (png, jpg, gif, webp), PDF, text, markdown, HTML and zip/gzip/7z archive files can be sent"
    )
    assert authed_page.evaluate("window.__pwned === undefined")


_ZIP = b"PK\x03\x04" + b"synthetic archive bytes"
_ZIP_ID = "b" * 32
_HOSTILE_ZIP_ID = "c" * 32
_HOSTILE_NAME = "<img src=x onerror=window.__pwned=1>.zip"


@pytest.mark.iphone
def test_an_archive_card_is_a_ready_download_link_with_no_preview(authed_page: Page, base_url: str) -> None:
    """An archive is download-only: the card carries a Download link whose
    blob: href is ready before the tap (iOS drops a download started after an
    await), a `download` name set as text, and no element that renders the
    bytes."""
    page_body = dict(_PAGE)
    page_body["entries"] = _PAGE["entries"][:2]
    page_body["outbox"] = [
        {"id": _ZIP_ID, "name": "backup.zip", "caption": "", "kind": "archive",
         "media": "application/octet-stream", "size": len(_ZIP), "timestamp": "2026-10-12T10:00:05Z"},
        {"id": _HOSTILE_ZIP_ID, "name": _HOSTILE_NAME, "caption": "", "kind": "archive",
         "media": "application/octet-stream", "size": len(_ZIP), "timestamp": "2026-10-12T10:00:06Z"},
    ]
    fetched: list = []

    def sessions(route):
        route.fulfill(status=200, content_type="application/json", body=_json.dumps({"sessions": [{
            "session_id": _SID, "kind": "pty", "agent": "codex",
            "project_dir": "E:/invented/sentfiles", "name": "sentfiles", "alive": True,
            "started_at": "2026-10-12T10:00:00Z", "live_title": "", "prompt_title": "",
            "manual_title": "Sent files demo",
        }]}))

    def transcript(route):
        route.fulfill(status=200, content_type="application/json", body=_json.dumps(page_body))

    def outbox_file(route):
        fetched.append(route.request.url)
        route.fulfill(
            status=200, content_type="application/octet-stream", body=_ZIP,
            headers={"Content-Disposition": "attachment; filename*=UTF-8''backup.zip"},
        )

    base = r".*/api/claude-code/sessions/" + _SID
    authed_page.route(re.compile(r".*/api/claude-code/sessions$"), sessions)
    authed_page.route(re.compile(base + r"/transcript(\?.*)?$"), transcript)
    authed_page.route(re.compile(base + r"/outbox/[a-f0-9]+$"), outbox_file)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    stub_session_mirror(authed_page)
    open_session_row(authed_page, authed_page.locator(f'#sessionsList li[data-session-id="{_SID}"]'), mode="chat")

    cards = authed_page.locator("#transcriptList .tr-file-card[data-kind='archive']")
    expect(cards).to_have_count(2)
    card = cards.first
    expect(card.locator(".tr-file-name")).to_have_text("backup.zip")
    expect(card.locator(".tr-file-size")).to_have_text(f"{len(_ZIP)} B")
    link = card.locator("a.tr-file-open")
    expect(link).to_have_text("Download")
    expect(link).to_have_attribute("href", re.compile(r"^blob:"))
    expect(link).to_have_attribute("download", "backup.zip")
    # Prefetched: the bytes were already fetched, so the tap only follows a link.
    assert any(url.endswith("/outbox/" + _ZIP_ID) for url in fetched)
    # The blob a tap follows is opaque bytes, never a type a browser renders.
    assert link.evaluate("a => fetch(a.href).then(r => r.blob()).then(b => b.type)") == "application/octet-stream"
    # Never rendered: nothing in the card can show the bytes.
    expect(card.locator("img, iframe, pre, button")).to_have_count(0)
    box = link.bounding_box()
    assert box is not None and box["height"] >= 44 and box["width"] >= 44

    # A name that is markup stays text, in the head and in the download name.
    hostile = cards.nth(1)
    expect(hostile.locator(".tr-file-name")).to_have_text(_HOSTILE_NAME)
    expect(hostile.locator(".tr-file-name img")).to_have_count(0)
    expect(hostile.locator("a.tr-file-open")).to_have_attribute("download", _HOSTILE_NAME)
    assert authed_page.evaluate("window.__pwned === undefined")


# --------------------------------------------------------------- HTML documents

_TRACKER = "https://tracker.example.invalid/"
_HOSTILE_HTML = (
    "<!doctype html><html><head>"
    f'<link rel="stylesheet" href="{_TRACKER}sheet.css"></head><body>'
    '<h1 id="h">Quarterly report</h1>'
    f'<img id="pixel" src="{_TRACKER}pixel.png" alt="">'
    "<script>"
    "try { parent.document.body.dataset.pwned = 'parent'; } catch (e) {}"
    "try { top.__pwned = 1; } catch (e) {}"
    "fetch('/api/board').then(function () { try { parent.__pwned = 2; } catch (e) {} });"
    "document.getElementById('h').textContent = 'script ran';"
    "</script></body></html>"
)
_HTML_ID = "d" * 32
_MB5 = 5 * 1024 * 1024


def _open_chat_with(page: Page, base_url: str, outbox: list, bodies: dict) -> list:
    """Open the Chat of a session whose outbox holds ``outbox``; each entry's
    bytes are ``bodies[id]``. Returns the URLs any frame but the app's own
    loaded, plus every request that reached the tracker: an iframe that
    fetches, loads or navigates shows up here. A request CSP blocks never
    finishes, so it is not one."""
    page_body = dict(_PAGE, entries=_PAGE["entries"][:2], outbox=outbox)
    frame_requests: list = []
    page.on("requestfinished", lambda req: frame_requests.append(req.url) if req.frame != page.main_frame else None)

    def tracker(route):
        frame_requests.append(route.request.url)
        route.fulfill(status=200, body=_PNG)


    def transcript(route):
        route.fulfill(status=200, content_type="application/json", body=_json.dumps(page_body))

    def outbox_file(route):
        route.fulfill(status=200, content_type="text/html; charset=utf-8",
                      body=bodies[route.request.url.rsplit("/", 1)[1]])

    _stub(page, [])
    base = r".*/api/claude-code/sessions/" + _SID
    page.route(re.compile(base + r"/transcript(\?.*)?$"), transcript)
    page.route(re.compile(base + r"/outbox/[a-f0-9]+$"), outbox_file)
    page.route(re.compile(r"https://tracker\.example\.invalid/.*"), tracker)
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    stub_session_mirror(page)
    open_session_row(page, page.locator(f'#sessionsList li[data-session-id="{_SID}"]'), mode="chat")
    return frame_requests


def _html_entry(entry_id: str, name: str, size: int) -> dict:
    return {"id": entry_id, "name": name, "caption": "", "kind": "html",
            "media": "text/html; charset=utf-8", "size": size, "timestamp": "2026-10-12T10:00:05Z"}


def _assert_inert(page: Page, frame, frame_requests: list) -> None:
    """The sandboxed document rendered, ran no script, reached nothing of the
    app's and loaded nothing from outside itself."""
    body = frame.content_frame
    expect(body.locator("#h")).to_have_text("Quarterly report")
    # A CSP-blocked image settles as broken; a loaded one would be a request.
    pixel = body.locator("#pixel")
    pixel.evaluate("img => img.complete || new Promise(r => img.onerror = img.onload = r)")
    assert pixel.evaluate("img => img.naturalWidth") == 0
    assert frame_requests == []
    assert page.evaluate("document.body.dataset.pwned === undefined && window.__pwned === undefined")
    assert frame.get_attribute("sandbox") == ""


@pytest.mark.iphone
def test_a_sent_html_document_runs_no_script_and_loads_nothing(authed_page: Page, base_url: str) -> None:
    """A document that tries to read the app, call its API and phone home
    does none of it, in the card or in the viewer."""
    frame_requests = _open_chat_with(
        authed_page, base_url, [_html_entry(_HTML_ID, "report.html", len(_HOSTILE_HTML))],
        {_HTML_ID: _HOSTILE_HTML},
    )
    card = authed_page.locator("#transcriptList .tr-file-card[data-kind='html']")
    expect(card.locator(".tr-file-note")).to_have_text("Shown without scripts")
    _assert_inert(authed_page, card.locator("iframe.tr-file-html-frame"), frame_requests)
    # No same-origin page exists for it: the Download blob is opaque bytes.
    card.locator(".tr-file-html-open").click()
    viewer = authed_page.locator("#htmlViewer")
    expect(viewer).to_be_visible()
    download = viewer.locator("#htmlViewerDownload")
    expect(download).to_have_attribute("download", "report.html")
    assert download.evaluate("a => fetch(a.href).then(r => r.blob()).then(b => b.type)") == "application/octet-stream"
    _assert_inert(authed_page, viewer.locator("iframe"), frame_requests)


@pytest.mark.iphone
def test_the_html_viewer_closes_on_esc_backdrop_and_back_and_returns_focus(
    authed_page: Page, base_url: str, browser_name: str
) -> None:
    doc = "<!doctype html><h1 id='h'>Quarterly report</h1>"
    _open_chat_with(authed_page, base_url, [_html_entry(_HTML_ID, "report.html", len(doc))], {_HTML_ID: doc})
    card = authed_page.locator("#transcriptList .tr-file-card[data-kind='html']")
    preview = card.locator(".tr-file-html-open")
    expect(preview).to_have_attribute("aria-label", "Open report.html")
    viewer = authed_page.locator("#htmlViewer")
    vw = authed_page.viewport_size["width"]

    def open_it(opener) -> None:
        opener.click()
        expect(viewer).to_be_visible()
        expect(viewer.locator("#htmlViewerName")).to_have_text("report.html")
        expect(viewer.locator("iframe").content_frame.locator("#h")).to_have_text("Quarterly report")

    def closed() -> None:
        expect(viewer).to_be_hidden()
        expect(preview).to_be_focused()
        expect(viewer.locator("iframe")).to_have_count(0)
        expect(authed_page.locator("#terminalOverlay")).to_be_visible()

    open_it(card.locator("button.tr-file-open"))
    box = viewer.bounding_box()
    if vw <= 520:  # the phone sheet fills the screen
        assert box["x"] == 0 and box["width"] == vw
    else:
        assert box["width"] <= 1100 and box["x"] > 0
    authed_page.keyboard.press("Escape")
    closed()
    open_it(preview)
    authed_page.evaluate("history.back()")  # what the back gesture does
    closed()
    if vw > 520:  # only the desktop dialog leaves a backdrop to tap
        open_it(preview)
        authed_page.mouse.click(5, 5)
        closed()
    # Every close took its own history marker with it.
    assert authed_page.evaluate("history.state === null || !history.state.htmlViewer")


def test_a_document_past_5_mb_is_a_tile_in_the_card_and_renders_in_the_viewer(
    authed_page: Page, base_url: str
) -> None:
    def doc(size: int) -> str:
        head = "<!doctype html><h1 id='h'>Big report</h1>"
        return head + " " * (size - len(head))

    small, big = "e" * 32, "f" * 32
    _open_chat_with(authed_page, base_url, [
        _html_entry(small, "at-the-line.html", _MB5), _html_entry(big, "past-the-line.html", _MB5 + 1),
    ], {small: doc(_MB5), big: doc(_MB5 + 1)})
    cards = authed_page.locator("#transcriptList .tr-file-card[data-kind='html']")
    expect(cards.first.locator("iframe.tr-file-html-frame")).to_have_count(1)
    expect(cards.first.locator(".tr-file-html-tile")).to_have_count(0)
    expect(cards.nth(1).locator(".tr-file-html-tile")).to_have_text("HTML document")
    expect(cards.nth(1).locator("iframe")).to_have_count(0)
    cards.nth(1).locator(".tr-file-html-open").click()
    expect(authed_page.locator("#htmlViewer iframe").content_frame.locator("#h")).to_have_text("Big report")
