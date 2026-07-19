"""Manual session rename (issue #458) — Coding tab row + Board drawer.

A launcher-native rename that wins over every auto-derived title source
(see ``sessions.js::sessionTitle`` and ``test_session_title_naming.py``'s
pure-function pin). These tests drive the actual UI: click the pencil icon,
fill the shared ``#sessionRenameDialog``, submit, and confirm both the POST
body and the on-screen title update — the Coding tab via a re-fetch, the
Board drawer optimistically (its drawer stays open across a rename, so it
patches the card in place rather than re-fetching — see board.js).
"""

from __future__ import annotations

import json as _json
import re

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.smoke

_CODING_SID = "s-rename-coding"
_BOARD_SID = "s-rename-board"


def _mock_sessions_list(page: Page, state: dict) -> None:
    def _handler(route):
        route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"sessions": [{
                "session_id": _CODING_SID,
                "kind": "pty",
                "agent": "claude",
                "project_dir": "E:/automation/renameproj",
                "name": "renameproj",
                "alive": True,
                "started_at": "2026-07-08T11:30:00Z",
                "live_title": "",
                "prompt_title": "",
                "manual_title": state["manual_title"],
            }]}),
        )

    page.route(re.compile(r".*/api/claude-code/sessions$"), _handler)


def _mock_rename(page: Page, sid: str, state: dict, captured: dict) -> None:
    def _handler(route):
        captured["method"] = route.request.method
        captured["body"] = route.request.post_data_json
        state["manual_title"] = captured["body"].get("title", "")
        route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({
                "session_id": sid, "manual_title": state["manual_title"],
            }),
        )

    page.route(re.compile(r".*/api/claude-code/sessions/" + sid + r"/rename$"), _handler)


def _mock_board(page: Page) -> None:
    page.route(
        re.compile(r".*/api/board$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({
                "generated_at": "2026-07-08T12:00:00Z",
                "columns": {
                    "backlog": [], "your_turn": [], "other": [], "done": [],
                    "claude_turn": [{
                        "session_id": _BOARD_SID,
                        "kind": "pty",
                        "agent": "claude",
                        "project_dir": "E:/automation/boardrenameproj",
                        "name": "boardrenameproj",
                        "alive": True,
                        "started_at": "2026-07-08T11:30:00Z",
                        "live_title": "",
                        "prompt_title": "",
                        "manual_title": "",
                        "project": "boardrenameproj",
                        "status": "working",
                        "age_seconds": 60,
                    }],
                },
                "github": {"fetched_at": "2026-07-08T11:00:00Z", "error": None},
                "sessions_state": {
                    "available": True, "stale": False,
                    "updated_at": "2026-07-08T11:58:00Z",
                },
            }),
        ),
    )
    page.route(
        re.compile(r".*/api/board/sessions/" + _BOARD_SID + r"/exchange$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"available": False, "reason": "no_exchange"}),
        ),
    )


def test_coding_tab_rename_wins_over_launch_name(
    authed_page: Page, base_url: str
) -> None:
    state = {"manual_title": ""}
    captured: dict = {}
    _mock_sessions_list(authed_page, state)
    _mock_rename(authed_page, _CODING_SID, state, captured)

    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    row = authed_page.locator(f'#sessionsList li[data-session-id="{_CODING_SID}"]')
    expect(row.locator(".name")).to_have_text("renameproj", timeout=10_000)

    row.locator('button[aria-label="Rename session"]').click()
    dialog = authed_page.locator("#sessionRenameDialog")
    expect(dialog).to_be_visible()
    expect(authed_page.locator("#sessionRenameInput")).to_have_value("renameproj")

    authed_page.locator("#sessionRenameInput").fill("My custom title")
    authed_page.locator("#sessionRenameForm button[type='submit']").click()

    authed_page.wait_for_function(
        "() => !document.getElementById('sessionRenameDialog').open",
        timeout=3_000,
    )
    assert captured.get("method") == "POST"
    assert captured.get("body") == {"title": "My custom title"}
    expect(row.locator(".name")).to_have_text("My custom title", timeout=10_000)


def test_board_drawer_rename_patches_card_in_place(
    authed_page: Page, base_url: str
) -> None:
    captured: dict = {}
    _mock_board(authed_page)
    _mock_rename(authed_page, _BOARD_SID, {"manual_title": ""}, captured)
    # #510: the real /api/claude-code/git-status boot fetch is unmocked and
    # non-deterministic in timing; its completion calls renderBoard() whenever
    # the Board tab is active (apps.js), which rebuilds the open drawer's DOM
    # (including this test's rename button) out from under it if the response
    # lands mid-interaction — the same class of race test_board_tab.py's
    # test_backlog_cards_color_coded_from_shared_git_cache mocks around.
    # Mocked BEFORE navigation so the boot call resolves immediately, well
    # before the drawer is even opened.
    authed_page.route(
        re.compile(r".*/api/claude-code/git-status$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"projects": []}),
        ),
    )

    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    authed_page.locator("#tabBoard").click()
    expect(authed_page.locator("#paneBoard")).to_be_visible()

    card = authed_page.locator(
        '.board-list[data-col="claude_turn"] li.board-item'
    ).first.locator("button.board-card")
    expect(card.locator(".board-card-title")).to_have_text(
        "boardrenameproj", timeout=10_000
    )
    card.click()

    drawer = authed_page.locator(".board-drawer")
    expect(drawer).to_be_visible()
    drawer.locator("button.board-rename-btn").click()

    dialog = authed_page.locator("#sessionRenameDialog")
    expect(dialog).to_be_visible()
    authed_page.locator("#sessionRenameInput").fill("Board custom title")
    authed_page.locator("#sessionRenameForm button[type='submit']").click()

    authed_page.wait_for_function(
        "() => !document.getElementById('sessionRenameDialog').open",
        timeout=3_000,
    )
    assert captured.get("body") == {"title": "Board custom title"}
    # Optimistic in-place patch — no second /api/board fetch is needed (the
    # drawer stays open, which would make fetchBoard() a no-op anyway).
    expect(card.locator(".board-card-title")).to_have_text(
        "Board custom title", timeout=5_000
    )


def test_rename_dialog_adopts_modal_contract(
    authed_page: Page, base_url: str
) -> None:
    # #545 supersedes #484: the rename dialogs adopt the fleet modal contract
    # (design.md components.modal), replacing the two-button Cancel+Save footer
    # that #484 hand-tuned into an equal pair. The contract is a square × close
    # in the *header* + exactly one full-width primary Save in the footer;
    # dismissal is via the × / Escape / backdrop, never a footer Cancel. This
    # is what clears the /design-sync modal-header + modal-footer FAILs.
    state = {"manual_title": ""}
    _mock_sessions_list(authed_page, state)

    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    row = authed_page.locator(f'#sessionsList li[data-session-id="{_CODING_SID}"]')
    expect(row.locator(".name")).to_have_text("renameproj", timeout=10_000)

    row.locator('button[aria-label="Rename session"]').click()
    expect(authed_page.locator("#sessionRenameDialog")).to_be_visible()

    # Header × close: the compact modal.closeSize square (~34px) carrying the
    # `dialog-close` class, sitting in the header *above* the input — not a
    # footer button.
    close = authed_page.locator("#sessionRenameCancel")
    expect(close).to_have_class(re.compile(r"\bdialog-close\b"))
    close_box = close.bounding_box()
    field = authed_page.locator("#sessionRenameInput").bounding_box()
    save = authed_page.locator(
        "#sessionRenameForm button[type='submit']"
    ).bounding_box()
    assert close_box and field and save
    assert close_box["width"] <= 40 and close_box["height"] <= 40, (
        f"× close is not a compact square: {close_box['width']}x{close_box['height']}"
    )
    assert abs(close_box["width"] - close_box["height"]) <= 2
    assert close_box["y"] < field["y"], "× close must sit in the header, above the input"

    # Footer: exactly one action, and it is the full-width primary Save — its
    # width matches the input field (both stretch to the dialog content box),
    # never a half-row paired with a Cancel.
    footer_buttons = authed_page.locator("#sessionRenameForm .dialog-actions button")
    expect(footer_buttons).to_have_count(1)
    expect(footer_buttons).to_have_text(re.compile(r"Save"))
    assert save["width"] >= field["width"] - 3, (
        f"Save width {save['width']} is not full-width vs field {field['width']}"
    )
    assert save["width"] > close_box["width"] * 3, (
        "footer Save reads as a narrow paired button, not the full-width primary"
    )


@pytest.mark.parametrize("width", [700, 900, 1100])
def test_board_drawer_rename_btn_clickable_at_narrow_desktop_widths(
    authed_page: Page, base_url: str, width: int
) -> None:
    # Regression for #473: below ~1150px the desktop board grid
    # (styles.css `@media (min-width: 700px) and (pointer: fine)`) let the
    # drawer-actions row overflow past its own column — the empty "Your
    # turn" column (stretched tall by CSS Grid's default row `align-items:
    # stretch` once the drawer expands its sibling) then won by paint order
    # and swallowed the click. `flex-wrap` on `.board-drawer-actions` keeps
    # the row inside its own column at every width in this range.
    captured: dict = {}
    _mock_board(authed_page)
    _mock_rename(authed_page, _BOARD_SID, {"manual_title": ""}, captured)
    authed_page.set_viewport_size({"width": width, "height": 800})
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    authed_page.locator("#tabBoard").click()
    expect(authed_page.locator("#paneBoard")).to_be_visible()

    card = authed_page.locator(
        '.board-list[data-col="claude_turn"] li.board-item'
    ).first.locator("button.board-card")
    expect(card.locator(".board-card-title")).to_have_text(
        "boardrenameproj", timeout=10_000
    )
    card.click()

    drawer = authed_page.locator(".board-drawer")
    expect(drawer).to_be_visible()
    drawer.locator("button.board-rename-btn").click()
    expect(authed_page.locator("#sessionRenameDialog")).to_be_visible()
