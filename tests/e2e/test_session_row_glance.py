"""The shared session row (issue #1433, step 1/7 of #1432), on both tabs.

The Code tab's session rows and the Board's session cards render one
anatomy: an avatar holding the agent's mark with a green alive badge (a
crown for the chief), a one-line title and a one-line meta line, chips only
for exceptions. "full control" is gone, "detached" is a neutral chip, a
stalled Board session carries a danger "stalled" chip, and nothing draws a
left-edge status border or dims an idle card.

Hermetic: the session list and the Board payload are route-mocked before
``goto()`` (#510), with fictional sessions.
"""

from __future__ import annotations

import json as _json
import re
import time

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import stable_eval

pytestmark = pytest.mark.smoke

_NOW = int(time.time())

_SESSIONS = [
    {"session_id": "s-chief", "kind": "pty", "agent": "claude", "label": "chief",
     "project_dir": "E:/work/control-room", "name": "chief", "flags": "",
     "started_at": _NOW - 7200, "alive": True, "rows": 40, "cols": 120,
     "live_title": "", "prompt_title": "", "manual_title": "chief", "output_chars": 10},
    {"session_id": "s-pty", "kind": "pty", "agent": "claude", "label": "",
     "project_dir": "E:/work/demo-garden", "name": "demo-garden", "flags": "",
     "started_at": _NOW - 600, "alive": True, "rows": 40, "cols": 120,
     "live_title": "",
     "prompt_title": "a deliberately long session title that cannot fit on one phone line",
     "manual_title": "", "output_chars": 10},
    {"session_id": "s-remote", "kind": "remote", "agent": "codex", "label": "",
     "project_dir": "E:/work/a-rather-long-project-folder-name-for-the-meta-line",
     "name": "remote", "flags": "", "started_at": _NOW - 60, "alive": True,
     "live_title": "", "prompt_title": "detached worker", "manual_title": "",
     "output_chars": 0},
]


def _card(sid: str, status: str, **extra) -> dict:
    card = {"session_id": sid, "kind": "pty", "agent": "claude", "alive": True,
            "project_dir": "E:/work/demo-garden", "name": "demo-garden",
            "live_title": "", "prompt_title": sid + " title", "project": "demo-garden",
            "status": status, "age_seconds": 300, "started_at": "2026-09-10T11:00:00Z"}
    card.update(extra)
    return card


_BOARD = {
    "generated_at": "2026-09-10T12:00:00Z",
    "columns": {
        "backlog": [],
        "claude_turn": [
            _card("b-chief", "working", label="chief", name="chief", manual_title="chief"),
            _card("b-idle", "idle"),
        ],
        "your_turn": [_card("b-stalled", "stalled"), _card("b-wait", "awaiting-input")],
        "other": [], "done": [],
    },
    "github": {"fetched_at": "2026-09-10T12:00:00Z", "error": None},
    "live_sessions": {"available": True, "error": None},
    "sessions_state": {"available": True, "stale": False, "updated_at": None},
    "active_issues": {"available": True, "updated_at": None, "count": 0},
}


def _mock(page: Page) -> None:
    def json_route(pattern: str, body: dict) -> None:
        page.route(re.compile(pattern), lambda route: route.fulfill(
            status=200, content_type="application/json", body=_json.dumps(body)))
    json_route(r".*/api/claude-code/sessions$", {"sessions": _SESSIONS})
    json_route(r".*/api/board(\?.*)?$", _BOARD)
    json_route(r".*/api/board/chief-plan$", {"state": "empty"})
    json_route(r".*/api/claude-code/git-status$", {"projects": []})
    json_route(r".*/api/board/github/refresh$", {"fetched_at": None, "error": None})
    json_route(r".*/api/rate-limits$", {"quota_lines": []})


# One read per row (#1346): the row, its title and meta, and every chip, so a
# rebuild between reads can never mix two renders.
_ROW_GEOMETRY = """el => {
  if (!el.isConnected) return null;
  const tap = el.querySelector('.session-open, .board-card');
  const r = tap.getBoundingClientRect();
  const title = el.querySelector('.srow-title');
  const meta = el.querySelector('.srow-meta');
  const line = (n) => parseFloat(getComputedStyle(n).lineHeight);
  const chips = Array.from(el.querySelectorAll('.srow-meta .chip')).map(c => {
    const b = c.getBoundingClientRect();
    return {text: c.textContent, inside: b.right <= r.right + 0.5,
            whole: c.scrollWidth <= c.clientWidth + 0.5};
  });
  const style = getComputedStyle(tap);
  return {
    height: r.height,
    titleOneLine: title.getBoundingClientRect().height <= line(title) + 1,
    metaOneLine: meta.getBoundingClientRect().height <= line(meta) + 1,
    avatar: el.querySelector('.avatar').getBoundingClientRect().width,
    chips,
    borderLeft: parseFloat(style.borderLeftWidth),
    opacity: parseFloat(style.opacity),
  };
}"""


def _assert_two_line_row(geom: dict, what: str) -> None:
    assert 60 <= geom["height"] <= 64, f"{what}: row is {geom['height']}px, not two lines"
    assert geom["titleOneLine"], f"{what}: title wraps"
    assert geom["metaOneLine"], f"{what}: meta wraps"
    assert geom["avatar"] == 36, f"{what}: avatar is {geom['avatar']}px"
    assert geom["borderLeft"] == 0, f"{what}: left-edge status border is back"
    assert geom["opacity"] == 1, f"{what}: the card is dimmed"
    for c in geom["chips"]:
        assert c["inside"] and c["whole"], f"{what}: chip {c['text']!r} is cut"


@pytest.mark.iphone
def test_code_rows_and_board_cards_share_the_session_row(
    authed_page: Page, base_url: str
) -> None:
    _mock(authed_page)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")

    # -- Code tab --
    def row(sid: str):
        return authed_page.locator(f'#sessionsList li[data-session-id="{sid}"]')

    expect(row("s-pty")).to_be_visible(timeout=10_000)
    expect(authed_page.locator("#sessionsList")).not_to_contain_text("full control")
    # Alive badge on a worker; the chief wears the crown instead.
    expect(row("s-pty").locator('.avatar-badge[data-badge="alive"]')).to_have_count(1)
    expect(row("s-pty").locator('.avatar .session-agent-icon use[href="#b-claude"]')).to_have_count(1)
    expect(row("s-chief").locator('.avatar-badge[data-badge="crown"].board-chief-crown')).to_have_count(1)
    expect(row("s-chief").locator('.avatar-badge[data-badge="alive"]')).to_have_count(0)
    # The normal case gets no chip; a detached row says so, neutrally.
    expect(row("s-pty").locator(".chip")).to_have_count(0)
    detached = row("s-remote").locator(".chip.session-detached")
    expect(detached).to_have_text("detached")
    expect(detached).to_have_attribute("data-tone", "neutral")
    # Meta is "folder · uptime" on Code.
    expect(row("s-pty").locator(".srow-meta-text")).to_have_text(
        re.compile(r"^demo-garden · up \d+m"))
    for sid in ("s-chief", "s-pty", "s-remote"):
        _assert_two_line_row(stable_eval(row(sid), _ROW_GEOMETRY), sid)

    # -- Board --
    authed_page.locator("#tabBoard").click()

    def card(sid: str):
        return authed_page.locator(f'#boardColumns li.board-item[data-session-id="{sid}"]')

    expect(card("b-stalled")).to_be_visible(timeout=10_000)
    stalled = card("b-stalled").locator(".chip.board-stalled-chip")
    expect(stalled).to_have_text("stalled")
    expect(stalled).to_have_attribute("data-tone", "danger")
    # Status is plain meta: "repo · status · age"; stalled says it once, as the chip.
    expect(card("b-wait").locator(".board-card-meta .srow-meta-text")).to_have_text(
        "demo-garden · needs you · 5m")
    expect(card("b-stalled").locator(".board-card-meta .srow-meta-text")).to_have_text(
        "demo-garden · 5m")
    expect(card("b-wait").locator(".chip")).to_have_count(0)
    expect(card("b-idle").locator('.avatar-badge[data-badge="alive"]')).to_have_count(1)
    expect(card("b-chief").locator('.avatar-badge[data-badge="crown"]')).to_have_count(1)
    for sid in ("b-chief", "b-idle", "b-stalled", "b-wait"):
        _assert_two_line_row(stable_eval(card(sid), _ROW_GEOMETRY), sid)

    # Tap behaviour is unchanged: the card still toggles its drawer.
    card("b-wait").locator("button.board-card").click()
    expect(card("b-wait").locator(".board-drawer")).to_be_visible()
