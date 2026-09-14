"""Send a follow-up message to a detached session from the gear menu (#967).

A detached (``kind: remote``) row's ⚙️ menu gains **Send message** — for the
agents the console-input probe on the issue proved (``console_input`` on
``/api/claude-code/agents``) — opening a #545-contract dialog whose Send
POSTs ``{"data", "submit": true}`` to the existing ``/input`` route. The
verdict is ``delivered: "unconfirmed"`` and the toast says so. The menu
itself becomes a vertical icon + label list. Boot fetches are stubbed before
``goto()`` (#510) and every geometry read goes through ``stable_read`` (#680):
the sessions list re-renders on its poll, so a raw ``bounding_box()`` can
land across a rebuild.
"""

from __future__ import annotations

import json as _json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import stable_read

pytestmark = pytest.mark.smoke

_SID = "s-send-967"


def _mock_sessions_list(page: Page, *, kind: str = "remote", agent: str = "claude") -> None:
    page.route(
        re.compile(r".*/api/claude-code/sessions$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"sessions": [{
                "session_id": _SID,
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


def _mock_input(page: Page, captured: dict, *, status: int = 200, body: dict = None) -> None:
    if body is None:
        body = {
            "ok": True, "bytes": 8, "submit": True,
            "delivered": "unconfirmed", "reason": "unverified", "ingested": None,
            "submitted": True, "submit_confirmed": None, "submit_state": "unconfirmed",
            "waited_ms": 0, "deferred": False, "units": 8, "error": None,
        }

    def _handler(route):
        captured["method"] = route.request.method
        captured["body"] = route.request.post_data_json
        captured.setdefault("calls", 0)
        captured["calls"] += 1
        route.fulfill(status=status, content_type="application/json", body=_json.dumps(body))

    page.route(re.compile(r".*/api/claude-code/sessions/" + _SID + r"/input$"), _handler)


def _row(page: Page):
    return page.locator(f'#sessionsList li[data-session-id="{_SID}"]')


def _open_menu(page: Page):
    row = _row(page)
    expect(row.locator(".name")).to_have_text("Send demo", timeout=10_000)
    row.locator(".session-gear").click()
    menu = row.locator(".session-menu")
    expect(menu).to_be_visible()
    return row, menu


def test_detached_claude_send_message_posts_input_and_toasts_unconfirmed(
    authed_page: Page, base_url: str
) -> None:
    captured: dict = {}
    _mock_sessions_list(authed_page)
    _mock_input(authed_page, captured)

    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    _, menu = _open_menu(authed_page)
    menu.locator('button[aria-label="Send message"]').click()

    dialog = authed_page.locator("#sessionSendDialog")
    expect(dialog).to_be_visible()
    expect(authed_page.locator("#sessionSendHeading")).to_have_text("Send message")
    authed_page.locator("#sessionSendInput").fill("continue")
    authed_page.locator("#sessionSendForm button[type='submit']").click()

    authed_page.wait_for_function(
        "() => !document.getElementById('sessionSendDialog').open", timeout=5_000,
    )
    assert captured.get("method") == "POST"
    assert captured.get("body") == {"data": "continue", "submit": True}
    assert captured.get("calls") == 1
    toast = authed_page.locator("#toast")
    expect(toast).to_be_visible()
    expect(toast).to_contain_text("not confirmed")
    expect(toast).not_to_contain_text("delivered")
    expect(toast).not_to_have_class(re.compile(r"\berror\b"))


def test_send_failure_keeps_dialog_open_and_toasts_error(
    authed_page: Page, base_url: str
) -> None:
    # What the phone sees against a session-host that cannot type into the
    # console (502) — and, until :8446 restarts onto this build, the old
    # host's HTTP 500 takes the same path: an error toast, never "sent".
    captured: dict = {}
    _mock_sessions_list(authed_page)
    _mock_input(
        authed_page, captured, status=502,
        body={"detail": "session s-send-967 console input failed — NOT typed, no submit was sent"},
    )

    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    _, menu = _open_menu(authed_page)
    menu.locator('button[aria-label="Send message"]').click()
    authed_page.locator("#sessionSendInput").fill("continue")
    authed_page.locator("#sessionSendForm button[type='submit']").click()

    toast = authed_page.locator("#toast")
    expect(toast).to_have_class(re.compile(r"\berror\b"))
    expect(toast).to_contain_text("Send failed")
    expect(toast).to_contain_text("NOT typed")
    expect(toast).not_to_contain_text("Sent,")
    # The message is still there to retry; nothing was silently dropped.
    expect(authed_page.locator("#sessionSendDialog")).to_be_visible()
    expect(authed_page.locator("#sessionSendInput")).to_have_value("continue")
    expect(authed_page.locator("#sessionSendForm button[type='submit']")).to_be_enabled()


def test_full_control_row_has_no_send_message_item(authed_page: Page, base_url: str) -> None:
    # A PTY row already has the terminal compose bar.
    _mock_sessions_list(authed_page, kind="pty")
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    _, menu = _open_menu(authed_page)
    expect(menu.locator('button[aria-label="Send message"]')).to_have_count(0)
    expect(menu.locator("button")).to_have_count(3)


def test_detached_unprobed_agent_has_no_send_message_item(authed_page: Page, base_url: str) -> None:
    # Grok was *not probed* on the issue (console_input false in the
    # registry) — the menu never offers a dead end.
    _mock_sessions_list(authed_page, kind="remote", agent="grok")
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    _, menu = _open_menu(authed_page)
    expect(menu.locator('button[aria-label="Send message"]')).to_have_count(0)
    expect(menu.locator('button[aria-label="Rename session"]')).to_be_visible()
    expect(menu.locator('button[aria-label="Stop and kill session"]')).to_be_visible()


def test_gear_menu_is_a_vertical_icon_and_label_list(authed_page: Page, base_url: str) -> None:
    _mock_sessions_list(authed_page)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    _, menu = _open_menu(authed_page)

    buttons = menu.locator("button")
    expect(buttons).to_have_count(4)
    labels = menu.locator(".session-menu-label")
    expect(labels).to_have_count(4)
    expect(labels).to_have_text(["Transcript", "Send message", "Rename", "Stop"])
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
    # Existing hooks the #953 sites and the smoke test rely on survive.
    expect(menu.locator(".action-stop-close")).to_have_count(1)
    expect(menu.locator('button[aria-label="Stop and kill session"]')).to_have_class(
        re.compile(r"\baction-stop-close\b")
    )


def test_send_dialog_adopts_modal_contract(authed_page: Page, base_url: str) -> None:
    # #545: header × close + exactly one full-width primary; no footer Cancel.
    _mock_sessions_list(authed_page)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    _, menu = _open_menu(authed_page)
    menu.locator('button[aria-label="Send message"]').click()
    expect(authed_page.locator("#sessionSendDialog")).to_be_visible()

    close = authed_page.locator("#sessionSendCancel")
    expect(close).to_have_class(re.compile(r"\bdialog-close\b"))
    field = authed_page.locator("#sessionSendInput")
    expect(field).to_be_focused()
    footer_buttons = authed_page.locator("#sessionSendForm .dialog-actions button")
    expect(footer_buttons).to_have_count(1)
    expect(footer_buttons).to_have_text(re.compile(r"Send"))

    def _geom():
        c, f, s = close.bounding_box(), field.bounding_box(), footer_buttons.first.bounding_box()
        return (c, f, s) if (c and f and s) else None

    close_box, field_box, send_box = stable_read(_geom)
    assert close_box["width"] == 44 and close_box["height"] == 44
    assert close_box["y"] < field_box["y"], "× close must sit in the header, above the textarea"
    assert send_box["width"] >= field_box["width"] - 3, "Send is not full-width"

    # × closes without sending.
    close.click()
    authed_page.wait_for_function(
        "() => !document.getElementById('sessionSendDialog').open", timeout=3_000,
    )
