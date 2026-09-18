"""Regression pins for #982 — Terminal ⇄ Chat modes of one session overlay
(Step 3 of #979).

The terminal overlay and the transcript overlay used to be two full-screen
views of the same session with no way between them. #982 makes them two
panes of ``#terminalOverlay`` (``data-mode="terminal" | "chat"``) behind an
icon-only segmented toggle in the bar, and a row tap reopening the
last-viewed mode. (The row gear that also offered Terminal · Chat · Rename
· Stop was removed in #1025; the toggle and the bar's ⋮ menu are the only
ways in now.)

The one behaviour that must not regress is #444's shape: **switching to Chat
and back must not reconnect the PTY or touch its scrollback.** Every PTY
WebSocket connect opens with a clear frame that erases scrollback (``ESC[3J``,
server.py ``_CLEAR_FRAME``), so a reconnect would wipe lines written into
xterm, and a repaint nudge would replay the agent's transcript on top of
them. The first test writes local marker lines *after* the first frame lands
(the #981 gotcha), toggles Terminal → Chat → Terminal, and asserts the marker
is still there exactly once on the same socket.

Availability rules (issue text): a detached session opens in Chat with the
Terminal segment aria-disabled and an inline "Detached session — no
terminal" line; a full-control session of an agent with no transcript
reader opens in Terminal with Chat aria-disabled; a tap on a disabled
segment toasts the reason.

Boot fetches that could rebuild the row under a click are stubbed before
``goto()`` (#510); state assertions use auto-retrying ``expect()`` (#680).
Row taps of a full-control row run under the iPhone projection only — the
Chromium desktop projection mirrors them to a PC window (#282), which
``test_desktop_session_mirror.py`` pins; the reader-less case opens through
the ``?session=`` deep link instead, so it runs on both projections. (A test
that needs a full-control row tap on *both* projections stubs the mirror
route instead — ``conftest.stub_session_mirror``.)
"""

from __future__ import annotations

import json as _json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import OVERLAY_OPEN_MS

pytestmark = pytest.mark.smoke

_MARKER = "mode-toggle-982"
_DETACHED_SID = "sid-detached-982"

# Count WebSocket constructions and delivered frames: a reconnect shows up
# as a second instance, and lines may only be written once a frame landed.
_WS_PROBE = """
(() => {
  const Orig = window.WebSocket;
  window.__wsInstances = [];
  window.__wsFrames = 0;
  function Wrapped(...args) {
    const ws = new Orig(...args);
    window.__wsInstances.push(ws);
    ws.addEventListener('message', () => { window.__wsFrames += 1; });
    return ws;
  }
  Wrapped.prototype = Orig.prototype;
  for (const k of ['CONNECTING', 'OPEN', 'CLOSING', 'CLOSED']) Wrapped[k] = Orig[k];
  window.WebSocket = Wrapped;
})();
"""

# The SPA loads its modules cache-busted (`state.js?v=<asset_hash>`); a bare
# import would evaluate a second, empty module instance. Resolve the page's
# real module URL so the import shares the live state (same pattern as
# test_warm_terminal_reopen.py / test_terminal_session_menu.py).
_LIVE_STATE_SETUP = """
async () => {
  const hit = performance.getEntriesByType('resource')
    .map((r) => r.name)
    .find((n) => n.includes('/static/state.js?v='));
  const { state } = await import(hit || '/static/state.js');
  window.__term = () => state.terminal && state.terminal.term;
}
"""

_COUNT_LINES = """
(needle) => {
  const term = window.__term();
  if (!term) return -1;
  const b = term.buffer.active;
  let n = 0;
  for (let i = 0; i < b.length; i++) {
    const line = b.getLine(i);
    if (line && line.translateToString(true).includes(needle)) n += 1;
  }
  return n;
}
"""

_TWO_FRAMES = (
    "() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)))"
)


def _transcript_body(sid: str) -> dict:
    return {
        "available": True, "source": "native", "reason": None, "session_id": sid,
        "next_cursor": None,
        "entries": [
            {"kind": "user", "timestamp": "2026-09-16T09:00:00Z",
             "text": "toggle test prompt", "truncated": False, "sidechain": False},
            {"kind": "assistant", "timestamp": "2026-09-16T09:00:05Z",
             "text": "toggle test reply", "truncated": False, "sidechain": False},
        ],
    }


def _mock_transcript(page: Page, sid: str, calls: list) -> None:
    def _handler(route):
        calls.append(route.request.url)
        route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps(_transcript_body(sid)),
        )

    page.route(re.compile(r".*/api/claude-code/sessions/" + re.escape(sid) + r"/transcript(\?.*)?$"), _handler)


def _mock_git_status(page: Page) -> None:
    # #510: a late git-status response rebuilds every Coding row under a click.
    page.route(
        re.compile(r".*/api/claude-code/git-status$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"projects": []}),
        ),
    )


def _session_row(sid: str, *, kind: str, agent: str, title: str) -> dict:
    return {
        "session_id": sid, "kind": kind, "agent": agent,
        "project_dir": "E:/automation/modeproj", "name": "modeproj",
        "alive": True, "started_at": "2026-09-16T08:00:00Z",
        "live_title": "", "prompt_title": "", "manual_title": title,
    }


def _mock_sessions_list(page: Page, rows: list) -> None:
    page.route(
        re.compile(r".*/api/claude-code/sessions$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"sessions": rows}),
        ),
    )


def _row(page: Page, sid: str):
    return page.locator(f'#sessionsList li.session-item[data-session-id="{sid}"]')


def _skip_unless_phone(browser_name: str) -> None:
    if browser_name != "webkit":
        pytest.skip(
            "row taps of a full-control row open the terminal in-page only under the "
            "iPhone projection; the Chromium desktop projection mirrors them (#282)"
        )


def test_toggle_keeps_terminal_scrollback_and_socket(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    sid = launched_pty_session
    calls: list = []
    _mock_transcript(authed_page, sid, calls)
    authed_page.add_init_script(_WS_PROBE)
    authed_page.goto(f"{base_url}/?session={sid}", wait_until="domcontentloaded")
    overlay = authed_page.locator("#terminalOverlay")
    authed_page.wait_for_selector("#terminalOverlay:not([hidden])", timeout=OVERLAY_OPEN_MS)
    expect(overlay).to_have_attribute("data-mode", "terminal")
    expect(authed_page.locator("#sessionModeTerminal")).to_have_attribute("aria-pressed", "true")
    expect(authed_page.locator("#chatPane")).to_be_hidden()

    authed_page.evaluate(_LIVE_STATE_SETUP)
    authed_page.wait_for_function("() => !!window.__term()", timeout=OVERLAY_OPEN_MS)
    # Only write once the connect's clear + replay frame is in (#981 gotcha).
    authed_page.wait_for_function("() => window.__wsFrames > 0", timeout=OVERLAY_OPEN_MS)
    authed_page.evaluate(_TWO_FRAMES)
    authed_page.evaluate(
        "(m) => new Promise((r) => window.__term().write("
        "Array.from({length: 300}, (_, i) => m + ' L' + String(i).padStart(4, '0')).join('\\r\\n')"
        " + '\\r\\n', r))",
        _MARKER,
    )
    needle = f"{_MARKER} L0007"
    assert authed_page.evaluate(_COUNT_LINES, needle) == 1
    sockets_before = authed_page.evaluate("() => window.__wsInstances.length")
    assert sockets_before == 1, f"expected one socket after the deep-link open, got {sockets_before}"

    # → Chat: the pane shows the (mocked) transcript, the terminal host and
    # its Latest pill are hidden, and no new socket was opened.
    authed_page.locator("#sessionModeChat").click()
    expect(overlay).to_have_attribute("data-mode", "chat")
    expect(authed_page.locator("#sessionModeChat")).to_have_attribute("aria-pressed", "true")
    expect(authed_page.locator("#chatPane")).to_be_visible()
    expect(authed_page.locator("#terminalHost")).to_be_hidden()
    expect(authed_page.locator("#terminalLatest")).to_be_hidden()
    expect(authed_page.locator("#transcriptList .tr-user").first).to_contain_text("toggle test prompt")
    # A viewport event while the terminal host is display:none must not reach
    # the PTY as a resize (the applySize guard, #930's class of bug).
    authed_page.evaluate("() => window.dispatchEvent(new Event('resize'))")
    authed_page.evaluate(_TWO_FRAMES)

    # → Terminal: same socket, same scrollback — the marker is there exactly
    # once (a reconnect's clear frame would drop it to 0, a repaint replay
    # would add to it), and the chat pane kept its page (one fetch in all).
    authed_page.locator("#sessionModeTerminal").click()
    expect(overlay).to_have_attribute("data-mode", "terminal")
    expect(authed_page.locator("#terminalHost")).to_be_visible()
    expect(authed_page.locator("#chatPane")).to_be_hidden()
    authed_page.evaluate(_TWO_FRAMES)
    assert authed_page.evaluate("() => window.__wsInstances.length") == sockets_before, (
        "switching Chat → Terminal opened a new WebSocket (reconnect)"
    )
    assert authed_page.evaluate(_COUNT_LINES, needle) == 1, (
        "terminal scrollback changed across a Chat round trip"
    )
    assert len(calls) == 1, f"the transcript was reloaded by a mode switch: {calls}"


def test_row_tap_reopens_last_mode(
    authed_page: Page, base_url: str, launched_pty_session: str, browser_name: str
) -> None:
    _skip_unless_phone(browser_name)
    sid = launched_pty_session
    calls: list = []
    _mock_git_status(authed_page)
    _mock_transcript(authed_page, sid, calls)
    authed_page.goto(base_url, wait_until="domcontentloaded")
    row = _row(authed_page, sid)
    overlay = authed_page.locator("#terminalOverlay")

    # First open: a full-control row starts in Terminal.
    row.locator(".session-open").click()
    authed_page.wait_for_selector("#terminalOverlay:not([hidden])", timeout=OVERLAY_OPEN_MS)
    expect(overlay).to_have_attribute("data-mode", "terminal")
    authed_page.locator("#sessionModeChat").click()
    expect(overlay).to_have_attribute("data-mode", "chat")
    authed_page.locator("#terminalBack").click()
    expect(overlay).to_be_hidden()

    # Back returns to the list; the next row tap reopens in Chat.
    row.locator(".session-open").click()
    expect(overlay).to_be_visible()
    expect(overlay).to_have_attribute("data-mode", "chat")
    expect(authed_page.locator("#chatPane")).to_be_visible()
    authed_page.locator("#terminalBack").click()
    expect(overlay).to_be_hidden()

    # The bar's Terminal segment forces the mode regardless of the memory,
    # and the memory follows: the gear item that used to do this went with
    # the gear (#1025), so the toggle is the only forcing path left.
    row.locator(".session-open").click()
    expect(overlay).to_be_visible()
    expect(overlay).to_have_attribute("data-mode", "chat")
    authed_page.locator("#sessionModeTerminal").click()
    expect(overlay).to_have_attribute("data-mode", "terminal")
    authed_page.locator("#terminalBack").click()
    expect(overlay).to_be_hidden()
    row.locator(".session-open").click()
    expect(overlay).to_have_attribute("data-mode", "terminal")


def test_detached_session_opens_in_chat_with_terminal_off(
    authed_page: Page, base_url: str
) -> None:
    calls: list = []
    _mock_git_status(authed_page)
    _mock_sessions_list(authed_page, [
        _session_row(_DETACHED_SID, kind="remote", agent="claude", title="Detached demo"),
    ])
    _mock_transcript(authed_page, _DETACHED_SID, calls)
    authed_page.goto(base_url, wait_until="domcontentloaded")
    row = _row(authed_page, _DETACHED_SID)
    overlay = authed_page.locator("#terminalOverlay")

    # A detached Claude row is tappable now (it has a chat) and opens in Chat.
    expect(row.locator(".session-open.inert")).to_have_count(0)
    row.locator(".session-open").click()
    expect(overlay).to_be_visible()
    expect(overlay).to_have_attribute("data-mode", "chat")
    expect(authed_page.locator("#chatNote")).to_be_visible()
    expect(authed_page.locator("#chatNote")).to_contain_text("Detached session — no terminal")
    term_seg = authed_page.locator("#sessionModeTerminal")
    expect(term_seg).to_have_attribute("aria-disabled", "true")
    expect(authed_page.locator("#sessionModeChat")).not_to_have_attribute("aria-disabled", "true")
    # A tap on the disabled segment explains itself and changes nothing
    # (forced: Playwright's actionability treats aria-disabled as not
    # enabled, but a finger doesn't — the click still dispatches).
    term_seg.click(force=True)
    expect(authed_page.locator("#toast")).to_contain_text("Detached session — no terminal")
    expect(overlay).to_have_attribute("data-mode", "chat")
    # The row keeps its own action menu behind the kebab, and shows no
    # chevron and no gear (#1025).
    authed_page.locator("#terminalBack").click()
    expect(overlay).to_be_hidden()
    expect(row.locator(".session-gear")).to_have_count(0)
    expect(row.locator(".session-chevron")).to_have_count(0)
    expect(row.locator(".session-kebab")).to_be_visible()


def test_reader_less_agent_opens_in_terminal_with_chat_off(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    # The list says this real PTY session is an SSH one — an agent with no
    # harness history at all: the deep link opens Terminal, Chat is off.
    # (`ssh` on purpose rather than a coding agent: Pi gained a reader in
    # #1013 and Antigravity/Copilot are next, so any of those would make
    # this test assert something that is about to stop being true.)
    sid = launched_pty_session
    _mock_git_status(authed_page)
    _mock_sessions_list(authed_page, [
        _session_row(sid, kind="pty", agent="ssh", title="SSH demo"),
    ])
    authed_page.goto(f"{base_url}/?session={sid}", wait_until="domcontentloaded")
    overlay = authed_page.locator("#terminalOverlay")
    authed_page.wait_for_selector("#terminalOverlay:not([hidden])", timeout=OVERLAY_OPEN_MS)
    expect(overlay).to_have_attribute("data-mode", "terminal")
    chat_seg = authed_page.locator("#sessionModeChat")
    expect(chat_seg).to_have_attribute("aria-disabled", "true")
    expect(authed_page.locator("#sessionModeTerminal")).not_to_have_attribute("aria-disabled", "true")
    chat_seg.click(force=True)  # see the detached test: a tap on aria-disabled
    # The reason, not the agent's display name: `agentLabel` reads
    # `state.agents`, whose conservative fallback in `state.js` lists only the
    # coding agents — so an `ssh` row renders "SSH" once `/api/agents` lands
    # and the raw "ssh" before it. Asserting the label would make this test
    # race that boot fetch (#510); the subject here is that Chat is off and
    # says why.
    expect(authed_page.locator("#toast")).to_contain_text("No transcript reader for")
    expect(overlay).to_have_attribute("data-mode", "terminal")
    # The row keeps its own action menu behind the kebab, and shows no
    # chevron and no gear (#1025).
    authed_page.locator("#terminalBack").click()
    expect(overlay).to_be_hidden()
    row = _row(authed_page, sid)
    expect(row.locator(".session-gear")).to_have_count(0)
    expect(row.locator(".session-chevron")).to_have_count(0)
    expect(row.locator(".session-kebab")).to_be_visible()
