"""Send from a session's Chat mode, for both session kinds (#967 → #975 → #983).

The shared composer (``composer.js``, #980) is mounted under the chat pane
(``#chatComposeBar``) for every session kind. ➤ Send POSTs
``{"data", "submit": true}`` to the kind-agnostic ``/input`` route and toasts
the route's own verdict in its own words — ``confirmed`` / ``unconfirmed`` /
``pending`` (202) — with a detached session's ``delivered: "unconfirmed"``
reading *Sent, not confirmed* and never as a failure; a failed send keeps the
draft. Attach uploads ``?inline=1`` and appends the stored path, which a
detached session can take too; the ⌨ keys are disabled for a detached
session; an agent never probed for console input keeps the composer but not
➤ Send. The gear menu's Send message item and its dialog are gone.

Tests key on class hooks scoped to the mount (``#chatComposeBar
.composer-*``), never ids (#980's decision). Boot fetches are stubbed before
``goto()`` (#510) and every geometry read goes through ``stable_read``
(#680): the sessions list re-renders on its poll.
"""

from __future__ import annotations

import base64
import json as _json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import open_session_row, stable_read, stub_session_mirror

pytestmark = pytest.mark.smoke

_SID = "s-send-967"

COMPOSER = "#chatComposeBar"
INPUT = f"{COMPOSER} .composer-input"
SEND = f"{COMPOSER} .composer-send"
KEYS = f"{COMPOSER} .composer-keys"
ATTACH_INPUT = f"{COMPOSER} .composer-attach-input"

# 1x1 transparent PNG — smallest valid image the session-host will accept.
# Named `e2e-stub-…` on purpose: the marker conftest's upload-leak check
# scans for (issue #922).
_PNG_1x1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mNk"
    "YAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)
_PATH_RE = re.compile(r"\.launcher-tmp.*\.png$")

_UNCONFIRMED_DETACHED = {
    "ok": True, "bytes": 8, "submit": True,
    "delivered": "unconfirmed", "reason": "unverified", "ingested": None,
    "submitted": True, "submit_confirmed": None, "submit_state": "unconfirmed",
    "waited_ms": 0, "deferred": False, "units": 8, "error": None,
}


def _pty_verdict(reason: str, submit_state: str, *, delivered: bool, deferred: bool = False) -> dict:
    return {
        "ok": True, "bytes": 8, "submit": True, "delivered": delivered,
        "reason": reason, "ingested": True, "submitted": submit_state in ("confirmed", "unconfirmed"),
        "submit_confirmed": True if submit_state == "confirmed" else None,
        "submit_state": submit_state, "waited_ms": 40, "deferred": deferred,
    }


def _mock_sessions_list(page: Page, *, sid: str = _SID, kind: str = "remote", agent: str = "claude") -> None:
    page.route(
        re.compile(r".*/api/claude-code/sessions$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"sessions": [{
                "session_id": sid,
                "kind": kind,
                "agent": agent,
                "project_dir": "E:/automation/sendproj",
                "name": "sendproj",
                "alive": True,
                "started_at": "2026-09-14T12:00:00Z",
                "live_title": "",
                "prompt_title": "",
                "manual_title": "Send demo",
                "web_url": "",
            }]}),
        ),
    )
    # #510: git-status is a git-subprocess-backed boot fetch; stub it so a
    # late response can't rebuild the DOM under a click.
    page.route(
        re.compile(r".*/api/claude-code/git-status$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"projects": []}),
        ),
    )


def _transcript(sid: str) -> dict:
    return {
        "available": True, "source": "native", "reason": None, "session_id": sid,
        "next_cursor": None,
        "entries": [
            {"kind": "user", "timestamp": "2026-09-15T06:20:00Z",
             "text": "this is a test", "truncated": False, "sidechain": False},
            {"kind": "assistant", "timestamp": "2026-09-15T06:20:05Z",
             "text": "Got it.", "truncated": False, "sidechain": False},
        ],
    }


def _mock_transcript(page: Page, calls: list, *, sid: str = _SID) -> None:
    def _handler(route):
        calls.append(route.request.url)
        route.fulfill(status=200, content_type="application/json", body=_json.dumps(_transcript(sid)))

    page.route(re.compile(r".*/api/claude-code/sessions/" + sid + r"/transcript(\?.*)?$"), _handler)


def _mock_input(page: Page, captured: dict, responses: list, *, sid: str = _SID) -> None:
    """Serve ``responses`` — ``(status, body)`` pairs — one per call, in order."""
    def _handler(route):
        captured["body"] = route.request.post_data_json
        captured["calls"] = captured.get("calls", 0) + 1
        status, body = responses.pop(0)
        route.fulfill(status=status, content_type="application/json", body=_json.dumps(body))

    page.route(re.compile(r".*/api/claude-code/sessions/" + sid + r"/input$"), _handler)


def _open_row(page: Page, sid: str = _SID):
    row = page.locator(f'#sessionsList li[data-session-id="{sid}"]')
    expect(row.locator(".name")).to_have_text("Send demo", timeout=10_000)
    return row


def _open_session_menu(page: Page, sid: str = _SID, mode: str | None = None):
    """Open the session overlay's ⋮ menu — since #1025 the only per-session
    menu there is (the row's own gear menu is gone; its items live here and,
    for Chat/Terminal, in the bar's segmented toggle)."""
    stub_session_mirror(page)
    open_session_row(page, _open_row(page, sid), mode=mode)
    page.locator("#terminalMenu").click()
    menu = page.locator("#terminalOverlay .terminal-menu")
    expect(menu).to_be_visible()
    return menu


def _open_chat(page: Page, sid: str = _SID) -> None:
    # #982 Chat mode, reached the way a finger reaches it since #1025: tap the
    # row, then the bar's Chat segment.
    stub_session_mirror(page)
    open_session_row(page, _open_row(page, sid), mode="chat")
    expect(page.locator("#transcriptList .tr-user").first).to_contain_text("this is a test")


def _wait_for_calls(page: Page, calls: list, n: int) -> None:
    # Route handlers run while Playwright waits, so poll by waiting.
    for _ in range(40):
        if len(calls) >= n:
            return
        page.wait_for_timeout(250)


@pytest.mark.parametrize("kind", ["pty", "remote"])
def test_session_menu_has_no_send_message_item(authed_page: Page, base_url: str, kind: str) -> None:
    # #983: the chat composer covers both kinds, so no menu offers Send. Since
    # #1025 the row has no menu at all, so the surface to check is the
    # overlay's ⋮ — the only per-session menu left.
    _mock_sessions_list(authed_page, kind=kind)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    menu = _open_session_menu(authed_page, mode="chat")
    expect(menu.locator('button[aria-label="Send message"]')).to_have_count(0)
    # Chat mode's full set (#982): the two chat-only rows sit between Copy
    # link and Stop, and nothing named Send appears for either kind.
    expect(menu.locator(".row-menu-label")).to_have_text(
        ["Rename", "Copy link", "Show tool calls", "Reload", "Stop and kill"]
    )
    expect(authed_page.locator("#sessionSendDialog")).to_have_count(0)


def test_session_menu_is_a_vertical_icon_and_label_list(authed_page: Page, base_url: str) -> None:
    # #967's shape contract for the shared row-menu component. Pinned on the
    # overlay's ⋮ menu since #1025 retired the row gear that first grew it —
    # same component (row-menu.js), so the contract is unchanged.
    _mock_sessions_list(authed_page, kind="pty")
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    menu = _open_session_menu(authed_page, mode="terminal")

    # Terminal mode: Rename · Copy link · Stop and kill (the chat-only rows
    # are detached from the DOM, not hidden — #982).
    buttons = menu.locator("button")
    expect(buttons).to_have_count(3)
    labels = menu.locator(".row-menu-label")
    expect(labels).to_have_count(3)
    for i in range(3):
        expect(buttons.nth(i).locator("svg.icon")).to_have_count(1)
        expect(labels.nth(i)).to_be_visible()

    def _boxes():
        bs = [buttons.nth(i).bounding_box() for i in range(3)]
        return bs if all(bs) else None

    boxes = stable_read(_boxes)
    assert boxes is not None
    xs = {round(b["x"]) for b in boxes}
    assert len(xs) == 1, f"menu rows are not stacked in one column: {boxes}"
    ys = [b["y"] for b in boxes]
    assert ys == sorted(ys) and len(set(round(y) for y in ys)) == 3, f"rows are not vertical: {ys}"
    for b in boxes:
        assert b["height"] >= 44 - 1, f"row under the 44px hit-target floor: {b}"
        assert b["width"] >= 150, f"row too narrow for icon + label: {b}"
    # Existing hooks the smoke test relies on survive.
    expect(menu.locator(".action-stop-close")).to_have_count(1)
    expect(menu.locator('button[aria-label="Stop and kill session"]')).to_have_class(
        re.compile(r"\baction-stop-close\b")
    )


def test_chat_composer_sends_to_detached_session_and_refreshes(
    authed_page: Page, base_url: str, browser_name: str
) -> None:
    captured: dict = {}
    calls: list = []
    _mock_sessions_list(authed_page)
    _mock_transcript(authed_page, calls)
    _mock_input(authed_page, captured, [
        (502, {"detail": "console input failed — NOT typed, no submit was sent"}),
        (200, _UNCONFIRMED_DETACHED),
    ])

    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    _open_chat(authed_page)
    field = authed_page.locator(INPUT)
    send = authed_page.locator(SEND)
    expect(authed_page.locator(COMPOSER)).to_be_visible()
    expect(field).to_have_attribute("placeholder", "Message for the agent")
    expect(authed_page.locator("#chatNote")).to_contain_text("not confirmed")
    # A detached session has no PTY: the keys render disabled, in place.
    expect(authed_page.locator(KEYS)).to_be_disabled()
    expect(send).to_be_enabled()
    if browser_name == "webkit":
        # The phone's floating nav pill hides under the overlay, so it can't
        # sit on top of the composer.
        expect(authed_page.locator("nav.tabs")).to_be_hidden()

    box = stable_read(lambda: send.bounding_box())
    viewport = authed_page.viewport_size
    assert box is not None and box["y"] + box["height"] <= viewport["height"], box
    assert box["width"] >= 44 - 1 and box["height"] >= 44 - 1, box

    # A failed send keeps the text to retry and never says "sent".
    field.fill("continue")
    send.click()
    toast = authed_page.locator("#toast")
    expect(toast).to_have_class(re.compile(r"\berror\b"))
    expect(toast).to_contain_text("Send failed")
    expect(toast).not_to_contain_text("Sent")
    expect(field).to_have_value("continue")
    expect(send).to_be_enabled()

    # The retry lands: box cleared, the unconfirmed wording (not a failure),
    # then one reload of the newest page.
    send.click()
    expect(field).to_have_value("")
    assert captured["body"] == {"data": "continue", "submit": True}
    assert captured["calls"] == 2
    expect(toast).to_contain_text("Sent, not confirmed")
    expect(toast).not_to_have_class(re.compile(r"\berror\b"))
    _wait_for_calls(authed_page, calls, 2)
    assert len(calls) == 2, f"transcript not reloaded once after the send: {calls}"
    expect(authed_page.locator("#transcriptList .tr-user")).to_have_count(1)


@pytest.mark.parametrize(
    "status, verdict, wording, is_error",
    [
        (200, _pty_verdict("ok", "confirmed", delivered=True), r"^\W*Sent$", False),
        (200, _pty_verdict("unverified", "unconfirmed", delivered=True), r"Sent, not confirmed", False),
        (202, _pty_verdict("deferred", "pending", delivered=False, deferred=True), r"Queued", False),
        (200, _pty_verdict("noop", "not_submitted", delivered=False), r"Not submitted", True),
    ],
    ids=["confirmed", "unconfirmed", "pending-202", "not-submitted"],
)
def test_chat_composer_sends_to_full_control_session_with_honest_wording(
    authed_page: Page, base_url: str, status: int, verdict: dict, wording: str, is_error: bool
) -> None:
    # #983: a full-control session sends from Chat through /input too, so the
    # verdict — the only feedback Chat has — reaches the toast verbatim in
    # meaning: a submit that was not sent never reads as a success.
    captured: dict = {}
    calls: list = []
    _mock_sessions_list(authed_page, kind="pty")
    _mock_transcript(authed_page, calls)
    _mock_input(authed_page, captured, [(status, verdict)])

    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    _open_chat(authed_page)
    field = authed_page.locator(INPUT)
    expect(field).to_have_attribute("placeholder", "Message")
    expect(authed_page.locator("#chatNote")).to_be_hidden()
    expect(authed_page.locator(KEYS)).to_be_enabled()

    field.fill("run the gate")
    authed_page.locator(SEND).click()
    expect(field).to_have_value("")
    assert captured["body"] == {"data": "run the gate", "submit": True}
    toast = authed_page.locator("#toast")
    expect(toast).to_have_text(re.compile(wording))
    if is_error:
        expect(toast).to_have_class(re.compile(r"\berror\b"))
    else:
        expect(toast).not_to_have_class(re.compile(r"\berror\b"))
    _wait_for_calls(authed_page, calls, 2)
    assert len(calls) == 2, f"transcript not reloaded once after the send: {calls}"


@pytest.mark.parametrize("registry", ["refused", "unknown"])
def test_chat_send_gate_follows_the_console_input_flag(
    authed_page: Page, base_url: str, registry: str
) -> None:
    # The registry's console_input flag gates ➤ Send alone (disabled with its
    # reason), never the whole composer. Only an explicit false refuses: when
    # /api/agents never lands the boot fallback carries no flag, and unknown
    # must not grey Send out — the session-host still refuses an unprobed
    # agent with its own 502.
    def _agents(route):
        if registry == "unknown":
            route.abort()
            return
        resp = route.fetch()
        data = resp.json()
        for a in data.get("agents", []):
            if a.get("id") == "claude":
                a["console_input"] = False
        route.fulfill(response=resp, json=data)

    authed_page.route(re.compile(r".*/api/agents$"), _agents)
    calls: list = []
    _mock_sessions_list(authed_page)
    _mock_transcript(authed_page, calls)
    if registry == "unknown":
        authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    else:
        # Open only once the registry answered, so the gate reads the flag.
        with authed_page.expect_response(re.compile(r".*/api/agents$")):
            authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    _open_chat(authed_page)

    send = authed_page.locator(SEND)
    field = authed_page.locator(INPUT)
    if registry == "refused":
        expect(send).to_be_disabled()
        expect(send).to_have_attribute("title", re.compile(r"No console input"))
    else:
        expect(send).to_be_enabled()
    expect(field).to_be_editable()
    field.fill("draft survives")
    expect(field).to_have_value("draft survives")


def test_detached_chat_attach_uploads_inline_and_sends_the_path(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    # The upload goes to the real (disposable) session-host's inline route;
    # the list presents the session as detached, and /input is stubbed so
    # nothing is typed into the live PTY. Inline storage never writes to the
    # session, which is what makes attach work for a detached one.
    sid = launched_pty_session
    captured: dict = {}
    calls: list = []
    uploads: list = []
    _mock_sessions_list(authed_page, sid=sid, kind="remote")
    _mock_transcript(authed_page, calls, sid=sid)
    _mock_input(authed_page, captured, [(200, _UNCONFIRMED_DETACHED)], sid=sid)
    authed_page.on(
        "request",
        lambda req: uploads.append(req.url) if "/image" in req.url else None,
    )

    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    _open_chat(authed_page, sid)
    authed_page.locator(ATTACH_INPUT).set_input_files(
        files=[{"name": "e2e-stub-detached.png", "mimeType": "image/png", "buffer": _PNG_1x1}]
    )
    field = authed_page.locator(INPUT)
    expect(field).to_have_value(_PATH_RE, timeout=10_000)
    assert uploads and all("inline=1" in u for u in uploads), uploads

    path = field.input_value()
    authed_page.locator(SEND).click()
    expect(field).to_have_value("")
    assert captured["body"] == {"data": path, "submit": True}
    expect(authed_page.locator("#toast")).to_contain_text("Sent, not confirmed")
