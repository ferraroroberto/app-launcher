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

from tests.e2e import _fake_clock
from tests.e2e.fixtures.ui import open_row_menu, open_session_row, stub_session_mirror
from tests.e2e.fixtures.waits import stable_eval, wait_until

pytestmark = pytest.mark.smoke

_CODING_SID = "s-rename-coding"
_BOARD_SID = "s-rename-board"

# Counts the /api/board bodies the page has parsed (jsonVerdict's
# `res.json()`), so a test knows when a held Board read has been acted on.
_COUNT_BOARD_BODIES = r"""
(() => {
  window.__boardBodies = 0;
  const json = Response.prototype.json;
  Response.prototype.json = function () {
    const board = /\/api\/board$/.test(new URL(this.url).pathname);
    return json.call(this).then(function (v) {
      if (board) window.__boardBodies += 1;
      return v;
    });
  };
})()
"""

_CLIPBOARD_MOCK = """
(() => {
  window.__copied = [];
  Object.defineProperty(navigator, 'clipboard', {
    configurable: true,
    value: {
      writeText: async (text) => { window.__copied.push(text); },
      readText: async () => '',
    },
  });
})()
"""


# The `web_url` default below carries a synthetic 24-char session token,
# never a captured one — same convention as test_terminal_session_menu.py
# (#1106). `kind`, `agent` and `web_url` are read from `state` on every
# request when set there, so one page can turn the row into another shape.
def _mock_sessions_list(
    page: Page,
    state: dict,
    *,
    kind: str = "pty",
    agent: str = "claude",
    web_url: str = "https://claude.ai/code/session_SYNTHETICRENAMEFIXTURE01",
) -> None:
    def _handler(route):
        route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"sessions": [{
                "session_id": _CODING_SID,
                "kind": state.get("kind", kind),
                "agent": state.get("agent", agent),
                "project_dir": "E:/automation/renameproj",
                "name": "renameproj",
                "alive": True,
                "started_at": "2026-07-08T11:30:00Z",
                "live_title": "",
                "prompt_title": "",
                "manual_title": state["manual_title"],
                "web_url": state.get("web_url", web_url),
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


def _board_body(manual_title: str) -> str:
    """One live Board card, carrying ``manual_title`` as the real
    ``/api/board`` does (``board_sessions.py`` reads the session-host's)."""
    return _json.dumps({
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
                "manual_title": manual_title,
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
    })


def _mock_board(page: Page, state: dict | None = None) -> None:
    """Serve the Board. `state`, when given, is the dict `_mock_rename`
    writes, read on every request, so a read after a rename returns the new
    title as the server does (#1573)."""
    state = state if state is not None else {"manual_title": ""}
    page.route(
        re.compile(r".*/api/board(?:\?.*)?$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_board_body(state["manual_title"]),
        ),
    )
    # The chief's plan rides every Board read (#1279): left to the real
    # server, its timing gates when a Board payload renders (#510, #1573).
    page.route(
        re.compile(r".*/api/board/chief-plan$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"state": "empty"}),
        ),
    )
    page.route(
        re.compile(r".*/api/board/sessions/" + _BOARD_SID + r"/exchange$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"available": False, "reason": "no_exchange"}),
        ),
    )
    # #510/#680: the real /api/claude-code/git-status boot fetch is
    # git-subprocess-backed and non-deterministic in timing; its completion
    # calls renderBoard() whenever the Board tab is active
    # (apps-coding.js::refreshGitStatus; an open drawer is kept since #958),
    # which rebuilds the card DOM out from under an in-progress interaction
    # before the drawer opens. Stubbed
    # in the shared helper so every test here gets it, not just the ones
    # that remembered to ask.
    page.route(
        re.compile(r".*/api/claude-code/git-status$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"projects": []}),
        ),
    )


def _open_rename(page: Page, row) -> None:
    """Open Rename for a running-sessions row.

    #1025 removed the row's actions gear, so Rename is reached through the
    session overlay the row tap opens: its ⋮ menu holds Rename (#981). The
    mirror stub keeps a full-control row's tap in-page on the Chromium
    projection (#282); a detached row never mirrors and ignores it.
    """
    stub_session_mirror(page)
    open_session_row(page, row)
    page.locator("#terminalMenu").click()
    menu = page.locator(".terminal-menu")
    expect(menu).to_be_visible()
    menu.locator('button[aria-label="Rename session"]').click()


def _open_board_rename_dialog(page: Page):
    """Board tab -> first card -> its drawer's kebab -> Rename. Returns the card."""
    card = page.locator(
        '.board-list[data-col="claude_turn"] li.board-item'
    ).first.locator("button.board-card")
    expect(card.locator(".board-card-title")).to_have_text(
        "boardrenameproj", timeout=10_000
    )
    card.click()

    drawer = page.locator(".board-drawer")
    expect(drawer).to_be_visible()
    # Rename lives in the drawer's kebab menu (#1502).
    drawer.locator(".board-drawer-kebab").click()
    open_row_menu(page).locator("[data-item=board-rename-btn]").click()
    return card


def _assert_modal_contract(page: Page) -> None:
    """The fleet modal contract (design.md components.modal): a square ×
    close in the *header* plus exactly one full-width primary Save in the
    footer; dismissal is via the × / Escape / backdrop, never a footer Cancel.
    This is what clears the /design-sync modal-header + modal-footer FAILs."""
    # Header × close: this dialog deliberately promotes the compact modal
    # close to the same real 44px geometry as its adjacent Link control while
    # retaining the vendored `detail-close` visual treatment. It still sits in the
    # header above the input, never in the footer.
    close = page.locator("#sessionRenameCancel")
    expect(close).to_have_class(re.compile(r"\bdetail-close\b"))
    close_box = close.bounding_box()
    field = page.locator("#sessionRenameInput").bounding_box()
    save = page.locator("#sessionRenameForm button[type='submit']").bounding_box()
    assert close_box and field and save
    assert close_box["width"] == 44 and close_box["height"] == 44, (
        f"× close does not match the 44px Link control: "
        f"{close_box['width']}x{close_box['height']}"
    )
    assert abs(close_box["width"] - close_box["height"]) <= 2
    assert close_box["y"] < field["y"], "× close must sit in the header, above the input"

    # Footer: exactly one action, and it is the full-width primary Save — its
    # width matches the input field (both stretch to the dialog content box),
    # never a half-row paired with a Cancel.
    footer_buttons = page.locator("#sessionRenameForm .detail-actions button")
    expect(footer_buttons).to_have_count(1)
    expect(footer_buttons).to_have_text(re.compile(r"Save"))
    assert save["width"] >= field["width"] - 3, (
        f"Save width {save['width']} is not full-width vs field {field['width']}"
    )
    assert save["width"] > close_box["width"] * 3, (
        "footer Save reads as a narrow paired button, not the full-width primary"
    )


def _assert_link_row_copies(page: Page) -> None:
    """A full-control row's dialog carries its session link, read-only, with
    a copy button the same height as the field and the close."""
    expect(page.locator("#sessionRenameHeading")).to_have_text("Rename / link")
    link = page.locator("#sessionLinkInput")
    expect(link).to_be_visible()
    expect(link).to_have_attribute("readonly", "")
    expected = "https://claude.ai/code/session_SYNTHETICRENAMEFIXTURE01"
    expect(link).to_have_value(expected)

    input_box = link.bounding_box()
    copy_box = page.locator("#sessionLinkCopy").bounding_box()
    close_box = page.locator("#sessionRenameCancel").bounding_box()
    assert input_box is not None and copy_box is not None and close_box is not None
    assert input_box["height"] == copy_box["height"]
    assert close_box["height"] == copy_box["height"]
    assert close_box["width"] == copy_box["width"]
    assert page.evaluate(
        "() => getComputedStyle(document.getElementById('sessionLinkInput')).color "
        "!== getComputedStyle(document.getElementById('sessionRenameInput')).color"
    )
    assert page.evaluate(
        "() => getComputedStyle(document.querySelector("
        "'#sessionRenameDialog .detail-actions')).borderTopWidth"
    ) == "0px"

    page.locator("#sessionLinkCopy").click()
    page.wait_for_function(
        "() => Array.isArray(window.__copied) && window.__copied.length === 1",
        timeout=3_000,
    )
    assert page.evaluate("() => window.__copied[0]") == expected
    expect(page.locator("#sessionLinkCopy")).to_have_class(re.compile(r"\bis-copied\b"))
    assert page.evaluate(
        "() => Number(getComputedStyle("
        "document.getElementById('sessionLinkCopy')).opacity) < 1"
    )
    expect(page.locator("#sessionLinkCopy")).not_to_have_class(
        re.compile(r"\bis-copied\b"), timeout=2_000,
    )
    expect(page.locator("#toast")).to_contain_text("Session link copied")


@pytest.mark.iphone
def test_full_control_rename_keeps_the_modal_contract_copies_the_link_and_wins(
    authed_page: Page, base_url: str
) -> None:
    """One dialog, three contracts: the modal shape (#545 supersedes #484),
    the session link row, and the rename itself winning over the launch
    name on the Coding row."""
    state = {"manual_title": ""}
    captured: dict = {}
    authed_page.add_init_script(_CLIPBOARD_MOCK)
    _mock_sessions_list(authed_page, state)
    _mock_rename(authed_page, _CODING_SID, state, captured)

    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    row = authed_page.locator(f'#sessionsList li[data-session-id="{_CODING_SID}"]')
    expect(row.locator(".name")).to_have_text("renameproj", timeout=10_000)

    _open_rename(authed_page, row)
    expect(authed_page.locator("#sessionRenameDialog")).to_be_visible()
    _assert_modal_contract(authed_page)
    _assert_link_row_copies(authed_page)

    expect(authed_page.locator("#sessionRenameInput")).to_have_value("renameproj")
    authed_page.locator("#sessionRenameInput").fill("My custom title")
    authed_page.locator("#sessionRenameForm button[type='submit']").click()
    authed_page.wait_for_function(
        "() => !document.getElementById('sessionRenameDialog').open",
        timeout=3_000,
    )
    assert captured.get("method") == "POST"
    assert captured.get("body") == {"title": "My custom title"}
    # The sessions poll is paused while the overlay is open (`main.js`:
    # `if (!state.sessionView) fetchSessions()`), and closing it refreshes the
    # list on the way out — so the row's new name is read after Back, not
    # under the overlay.
    authed_page.locator("#terminalBack").click()
    expect(authed_page.locator("#terminalOverlay")).to_be_hidden()
    expect(row.locator(".name")).to_have_text("My custom title", timeout=10_000)


def test_codex_link_is_unavailable_and_a_detached_row_offers_none(
    authed_page: Page, base_url: str
) -> None:
    """A Codex row's dialog has the link row but no link yet; a detached
    row's has no link row at all. One page: the row turns detached between
    the two (the list re-reads on the way back from the overlay)."""
    state = {"manual_title": "", "agent": "codex", "web_url": ""}
    _mock_sessions_list(authed_page, state)

    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    row = authed_page.locator(f'#sessionsList li[data-session-id="{_CODING_SID}"]')
    _open_rename(authed_page, row)

    expect(authed_page.locator("#sessionRenameHeading")).to_have_text("Rename / link")
    expect(authed_page.locator("#sessionLinkInput")).to_have_value("Not available yet")
    expect(authed_page.locator("#sessionLinkCopy")).to_be_disabled()

    authed_page.locator("#sessionRenameCancel").click()
    expect(authed_page.locator("#sessionRenameDialog")).to_be_hidden()
    state.update(kind="remote", agent="claude")
    authed_page.locator("#terminalBack").click()
    expect(authed_page.locator("#terminalOverlay")).to_be_hidden()
    expect(row.locator(".chip.session-detached")).to_be_visible()

    _open_rename(authed_page, row)
    expect(authed_page.locator("#sessionRenameDialog")).to_be_visible()
    expect(authed_page.locator("#sessionRenameHeading")).to_have_text("Rename session")
    expect(authed_page.locator("#sessionLinkRow")).to_be_hidden()


@pytest.mark.iphone
def test_board_drawer_rename_is_clickable_at_narrow_widths_and_patches_the_card(
    authed_page: Page, base_url: str
) -> None:
    # Regression for #473: below ~1150px the desktop board grid
    # (styles/board.css `@media (min-width: 700px) and (pointer: fine)`) let the
    # drawer-actions row overflow past its own column — the empty "Your
    # turn" column (stretched tall by CSS Grid's default row `align-items:
    # stretch` once the drawer expands its sibling) then won by paint order
    # and swallowed the click. The `minmax(0, 1fr)` grid on
    # `.board-drawer-actions` (#984, #1502) keeps the row and its kebab menu
    # inside its own column at every width in this range. One page walks the
    # widths, closing the dialog and the drawer between them, and renames at
    # the last. One state for both routes, so a Board poll after the rename
    # serves the new title as the real server does (#1573).
    state = {"manual_title": ""}
    captured: dict = {}
    _mock_board(authed_page, state)
    _mock_rename(authed_page, _BOARD_SID, state, captured)
    authed_page.set_viewport_size({"width": 700, "height": 800})
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    authed_page.locator("#tabBoard").click()
    expect(authed_page.locator("#paneBoard")).to_be_visible()

    dialog = authed_page.locator("#sessionRenameDialog")
    drawer = authed_page.locator(".board-drawer")
    for width in (700, 900, 1100):
        authed_page.set_viewport_size({"width": width, "height": 800})
        card = _open_board_rename_dialog(authed_page)
        expect(dialog).to_be_visible()
        if width == 1100:
            break
        authed_page.locator("#sessionRenameCancel").click()
        expect(dialog).to_be_hidden()
        card.click()
        expect(drawer).to_be_hidden()

    # The patch renders in the same task that closes the dialog, so the title
    # read when `close` fires is the patch's, not a later Board poll's: a
    # poll now serves the new title too, and the final check alone would
    # pass without any patch.
    authed_page.evaluate(
        """() => document.getElementById('sessionRenameDialog')
            .addEventListener('close', () => {
              const t = document.querySelector(
                '.board-list[data-col="claude_turn"] .board-card-title');
              window.__titleAtClose = t ? t.textContent : null;
            }, { once: true })"""
    )
    authed_page.locator("#sessionRenameInput").fill("Board custom title")
    authed_page.locator("#sessionRenameForm button[type='submit']").click()

    authed_page.wait_for_function(
        "() => window.__titleAtClose !== undefined", timeout=3_000,
    )
    assert captured.get("body") == {"title": "Board custom title"}
    # Optimistic in-place patch — the title updates without waiting for the
    # next /api/board poll…
    assert authed_page.evaluate("() => window.__titleAtClose") == "Board custom title"
    # …and stays, now that a poll serves it as well.
    expect(card.locator(".board-card-title")).to_have_text("Board custom title")


def test_board_read_in_flight_across_a_rename_keeps_the_patch(
    authed_page: Page, base_url: str
) -> None:
    """#1573: a Board poll sent before the rename and answered after it
    carries the old title. Rendered, it undid the optimistic patch until the
    next poll — the under-load red of the test above."""
    _fake_clock.install(authed_page)
    authed_page.add_init_script(_COUNT_BOARD_BODIES)
    state = {"manual_title": ""}
    captured: dict = {}
    _mock_board(authed_page, state)
    _mock_rename(authed_page, _BOARD_SID, state, captured)
    held: list = []
    hold = {"next": False}

    def _board(route):
        # Registered after _mock_board, so matched first: park the one read
        # asked for, pass every other through.
        if hold["next"]:
            hold["next"] = False
            held.append(route)
            return
        route.fallback()

    authed_page.route(re.compile(r".*/api/board(?:\?.*)?$"), _board)

    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    authed_page.locator("#tabBoard").click()
    card = _open_board_rename_dialog(authed_page)
    title = card.locator(".board-card-title")
    expect(authed_page.locator("#sessionRenameDialog")).to_be_visible()
    authed_page.locator("#sessionRenameInput").fill("Board custom title")

    # The poll goes out before the rename and is still unanswered after it.
    hold["next"] = True
    _fake_clock.jump_interval(authed_page, 5_000, "fetchBoard")
    wait_until(authed_page, lambda: bool(held), "a Board poll in flight")
    authed_page.locator("#sessionRenameForm button[type='submit']").click()
    expect(title).to_have_text("Board custom title")
    assert captured.get("body") == {"title": "Board custom title"}

    # Answered now, with what the server held when the poll reached it. The
    # page has acted on it once its body is parsed: fetchBoard() resumes in
    # the same microtask checkpoint as the counter below.
    bodies = authed_page.evaluate("() => window.__boardBodies")
    held[0].fulfill(
        status=200, content_type="application/json", body=_board_body(""),
    )
    authed_page.wait_for_function("n => window.__boardBodies > n", arg=bodies)
    # One read, not an auto-retrying expect: the next poll serves the new
    # title, so a retry would pass straight through a revert.
    assert stable_eval(title, "el => el.textContent") == "Board custom title"
