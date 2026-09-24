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
    captured: dict = {}
    _mock_input(authed_page, captured, [(200, {
        "ok": True, "delivered": True, "submit_state": "confirmed",
    })])
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    menu = _open_session_menu(authed_page, mode="chat")
    expect(menu.locator('button[aria-label="Send message"]')).to_have_count(0)
    # Chat mode's full set (#982): the two chat-only rows sit between Copy
    # link and Stop, and nothing named Send appears for either kind. Compact
    # (#1218) sits last in the safe group, above the destructive divider.
    expect(menu.locator(".row-menu-label")).to_have_text(
        ["Rename", "Copy link", "Show tool calls", "Reload", "Compact", "Stop and kill"]
    )
    expect(authed_page.locator("#sessionSendDialog")).to_have_count(0)

    # ⋮ Compact sends /compact through the same /input route Chat's Send
    # uses, for either kind, and the toast names the verdict (#1218).
    menu.locator('button[aria-label="Compact conversation"]').click()
    expect(authed_page.locator("#toast")).to_contain_text("Compact: Sent")
    assert captured["body"] == {"data": "/compact", "submit": True}
    assert captured["calls"] == 1


def test_session_menu_is_a_vertical_icon_and_label_list(authed_page: Page, base_url: str) -> None:
    # #967's shape contract for the shared row-menu component. Pinned on the
    # overlay's ⋮ menu since #1025 retired the row gear that first grew it —
    # same component (row-menu.js), so the contract is unchanged.
    _mock_sessions_list(authed_page, kind="pty")
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    menu = _open_session_menu(authed_page, mode="terminal")

    # Terminal mode: Rename · Copy link · Compact · Stop and kill (the
    # chat-only rows are detached from the DOM, not hidden — #982; Compact
    # shows in both modes for a Claude session — #1218).
    buttons = menu.locator("button")
    expect(buttons).to_have_count(4)
    labels = menu.locator(".row-menu-label")
    expect(labels).to_have_text(["Rename", "Copy link", "Compact", "Stop and kill"])
    for i in range(4):
        expect(buttons.nth(i).locator("svg.icon")).to_have_count(1)
        expect(labels.nth(i)).to_be_visible()

    def _boxes():
        bs = [buttons.nth(i).bounding_box() for i in range(4)]
        return bs if all(bs) else None

    boxes = stable_read(_boxes)
    assert boxes is not None
    xs = {round(b["x"]) for b in boxes}
    assert len(xs) == 1, f"menu rows are not stacked in one column: {boxes}"
    ys = [b["y"] for b in boxes]
    assert ys == sorted(ys) and len(set(round(y) for y in ys)) == 4, f"rows are not vertical: {ys}"
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
    # #1050: the sent turn arrives through live refresh — a forward-cursor
    # tick — rather than the full page reload this used to do, so the pane
    # keeps its scroll position and open cards. Pinned as "exactly one page
    # load, and at least one tick after it" rather than a call count, which
    # a live view no longer has a fixed one of.
    pages = [u for u in calls if "after=" not in u]
    assert len(pages) == 1, f"the send reloaded the whole transcript: {calls}"
    assert any("after=" in u for u in calls), f"no live tick after the send: {calls}"
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
    # #1050: the sent turn arrives through live refresh — a forward-cursor
    # tick — rather than the full page reload this used to do, so the pane
    # keeps its scroll position and open cards. Pinned as "exactly one page
    # load, and at least one tick after it" rather than a call count, which
    # a live view no longer has a fixed one of.
    pages = [u for u in calls if "after=" not in u]
    assert len(pages) == 1, f"the send reloaded the whole transcript: {calls}"
    assert any("after=" in u for u in calls), f"no live tick after the send: {calls}"


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
        # aria-disabled, never the `disabled` attribute (#1069): the tap has
        # to keep landing so the reason can be toasted — pinned by
        # test_a_refused_send_answers_a_tap_instead_of_going_dead below.
        expect(send).to_have_attribute("aria-disabled", "true")
        expect(send).not_to_have_attribute("disabled", re.compile(r".*"))
        expect(send).to_have_attribute("title", re.compile(r"No console input"))
    else:
        expect(send).to_be_enabled()
    expect(field).to_be_editable()
    field.fill("draft survives")
    expect(field).to_have_value("draft survives")


def test_a_refused_send_answers_a_tap_instead_of_going_dead(
    authed_page: Page, base_url: str
) -> None:
    # #1069: the refusal used to be a real `disabled` attribute. On iOS a
    # disabled button fires no tap event at all, so the reason could only
    # ever surface through `title` — a hover a phone does not have — and
    # Send read as simply broken: nothing sent, nothing said. It is
    # aria-disabled now — the convention the Board drawer's own actions
    # already state for an action a session can't take — so the tap lands
    # and says why. What this pins: the toast carries the reason, the draft
    # survives the tap, and nothing is POSTed to /input.
    def _agents(route):
        resp = route.fetch()
        data = resp.json()
        for a in data.get("agents", []):
            if a.get("id") == "claude":
                a["console_input"] = False
        route.fulfill(response=resp, json=data)

    authed_page.route(re.compile(r".*/api/agents$"), _agents)
    posts: list = []
    authed_page.on(
        "request",
        lambda req: posts.append(req.url)
        if req.method == "POST" and "/input" in req.url
        else None,
    )
    calls: list = []
    _mock_sessions_list(authed_page)
    _mock_transcript(authed_page, calls)
    # Open only once the registry answered, so the gate reads the flag.
    with authed_page.expect_response(re.compile(r".*/api/agents$")):
        authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    _open_chat(authed_page)

    send = authed_page.locator(SEND)
    field = authed_page.locator(INPUT)
    field.fill("why will you not send this")
    # force=True: Playwright's own actionability treats aria-disabled as not
    # enabled, but a finger has no actionability check — the same pattern
    # test_session_mode_toggle.py uses for the bar's disabled segments. On
    # pre-fix code the forced tap still delivers nothing, because a genuinely
    # `disabled` button suppresses activation in the browser itself: which is
    # the defect, not a quirk of the harness.
    send.click(force=True)

    expect(authed_page.locator("#toast")).to_have_text(re.compile(r"No console input"))
    expect(field).to_have_value("why will you not send this")
    assert posts == [], f"a refused Send still posted to /input: {posts}"

    # And it drops its accent fill for the shared button-disabled recipe — it
    # used to render identical to a working Send. Asserted against the ⌨ key,
    # disabled in the same grid for the same reason, so it holds either theme.
    expect(send).to_have_css(
        "background-color",
        stable_read(lambda: authed_page.locator(KEYS).evaluate(
            "el => getComputedStyle(el).backgroundColor"
        )),
    )


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

    # #1206: a pasted image and a dropped file take the same path in Chat,
    # which has no terminal host to catch them. A paste that also carries
    # plain text stays a text paste: nothing is uploaded.
    assert not _paste_or_drop(authed_page, "paste", with_text=True)
    authed_page.wait_for_timeout(300)
    assert len(uploads) == 1 and field.input_value() == "", uploads
    assert _paste_or_drop(authed_page, "paste")
    expect(field).to_have_value(_PATH_RE, timeout=10_000)
    field.fill("")
    assert _paste_or_drop(authed_page, "drop")
    expect(field).to_have_value(_PATH_RE, timeout=10_000)
    assert len(uploads) == 3 and all("inline=1" in u for u in uploads), uploads


def _paste_or_drop(page: Page, kind: str, *, with_text: bool = False) -> bool:
    """Fire a synthetic paste on the Chat composer's textarea, or a drop on
    the composer, carrying the 1x1 PNG; return whether the page took it
    (defaultPrevented). The event's DataTransfer is defined on the event,
    since neither engine lets a page build a trusted clipboard event."""
    return page.evaluate(
        """([b64, kind, withText, composer]) => {
          const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
          const dt = new DataTransfer();
          dt.items.add(new File([bytes], 'e2e-stub-' + kind + '.png', {type: 'image/png'}));
          if (withText) dt.setData('text/plain', 'copied text');
          const ev = new Event(kind, {bubbles: true, cancelable: true});
          Object.defineProperty(ev, kind === 'paste' ? 'clipboardData' : 'dataTransfer', {value: dt});
          const target = document.querySelector(
            kind === 'paste' ? composer + ' .composer-input' : composer);
          target.dispatchEvent(ev);
          return ev.defaultPrevented;
        }""",
        [base64.b64encode(_PNG_1x1).decode(), kind, with_text, COMPOSER],
    )


# --- #1072: Ctrl/Cmd+Enter sends from a desktop keyboard --------------------
#
# The composer had no key handler at all, so ➤ was the only way to deliver and
# the button's title ("Send (with Enter)") promised a shortcut nothing
# implemented. These four pin the shape the issue asked for: a modifier gesture
# sends, the bare return key is untouched, and the shortcut is a second way to
# reach `submit()` rather than a second send path that could skip its gates.


@pytest.mark.parametrize("modifier", ["Control", "Meta"], ids=["ctrl", "cmd"])
def test_mod_enter_sends_from_the_chat_composer(
    authed_page: Page, base_url: str, modifier: str
) -> None:
    # Same modifier gesture, two keyboards: Ctrl on a PC, Cmd on a Mac.
    captured: dict = {}
    calls: list = []
    _mock_sessions_list(authed_page)
    _mock_transcript(authed_page, calls)
    _mock_input(authed_page, captured, [(200, _UNCONFIRMED_DETACHED)])

    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    _open_chat(authed_page)
    field = authed_page.locator(INPUT)
    field.fill("sent from the keyboard")
    field.press(f"{modifier}+Enter")

    # Delivered through the same /input route as a tap, and the draft clears.
    expect(field).to_have_value("")
    assert captured["body"] == {"data": "sent from the keyboard", "submit": True}
    expect(authed_page.locator("#toast")).to_contain_text("Sent, not confirmed")


def test_plain_enter_still_inserts_a_newline_and_sends_nothing(
    authed_page: Page, base_url: str
) -> None:
    # The half of #1072 that must NOT change. Multi-line prompts are the normal
    # case here, so the return key stays a newline — and on the WebKit/iPhone
    # projection this is the proof that the phone's on-screen keyboard is
    # untouched: the binding needs a Ctrl/Cmd the soft keyboard has no key for,
    # so its return key can never match the guard. That is why the
    # implementation is a plain modifier check and not a touch-device sniff.
    captured: dict = {}
    calls: list = []
    posts: list = []
    _mock_sessions_list(authed_page)
    _mock_transcript(authed_page, calls)
    _mock_input(authed_page, captured, [(200, _UNCONFIRMED_DETACHED)])
    authed_page.on(
        "request",
        lambda req: posts.append(req.url)
        if req.method == "POST" and "/input" in req.url
        else None,
    )

    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    _open_chat(authed_page)
    field = authed_page.locator(INPUT)
    field.fill("first line")
    field.press("Enter")
    field.type("second line")

    expect(field).to_have_value("first line\nsecond line")
    assert posts == [], f"a bare Enter sent the draft: {posts}"


def test_mod_enter_does_not_bypass_a_refused_send(
    authed_page: Page, base_url: str
) -> None:
    # The shortcut routes through submit(), so the console-input gate (#1069)
    # holds it exactly as it holds a tap: the reason is toasted, the draft
    # survives, and nothing reaches /input. A shortcut wired straight to the
    # send would have sailed past all three.
    def _agents(route):
        resp = route.fetch()
        data = resp.json()
        for a in data.get("agents", []):
            if a.get("id") == "claude":
                a["console_input"] = False
        route.fulfill(response=resp, json=data)

    authed_page.route(re.compile(r".*/api/agents$"), _agents)
    posts: list = []
    authed_page.on(
        "request",
        lambda req: posts.append(req.url)
        if req.method == "POST" and "/input" in req.url
        else None,
    )
    calls: list = []
    _mock_sessions_list(authed_page)
    _mock_transcript(authed_page, calls)
    # Open only once the registry answered, so the gate reads the flag.
    with authed_page.expect_response(re.compile(r".*/api/agents$")):
        authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    _open_chat(authed_page)

    field = authed_page.locator(INPUT)
    expect(authed_page.locator(SEND)).to_have_attribute("aria-disabled", "true")
    field.fill("the shortcut must not sneak this through")
    field.press("Control+Enter")

    expect(authed_page.locator("#toast")).to_have_text(re.compile(r"No console input"))
    expect(field).to_have_value("the shortcut must not sneak this through")
    assert posts == [], f"the shortcut bypassed the send gate: {posts}"


def test_send_title_names_the_binding_that_exists(
    authed_page: Page, base_url: str
) -> None:
    # The tooltip discrepancy #1072 found: 'Send (with Enter)' described
    # behaviour no code had, on every surface. Chat opts into the shortcut, so
    # its title names it. The other half — a non-opted surface must not claim a
    # shortcut it doesn't have — is pinned on the terminal composer in
    # test_compose_bar.py, which already has that mount's harness.
    calls: list = []
    _mock_sessions_list(authed_page)
    _mock_transcript(authed_page, calls)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    _open_chat(authed_page)
    expect(authed_page.locator(SEND)).to_have_attribute("title", "Send (Ctrl/Cmd+Enter)")
