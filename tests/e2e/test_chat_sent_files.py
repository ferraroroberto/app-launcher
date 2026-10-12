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
             {"offset": 7, "n": 1, "name": "page.html", "kind": None},
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
                "message": "only images (png, jpg, gif, webp), PDF, text and markdown files can be sent",
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
        "Not shown: only images (png, jpg, gif, webp), PDF, text and markdown files can be sent"
    )
    assert authed_page.evaluate("window.__pwned === undefined")
