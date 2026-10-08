"""The usage meter (issue #1433, step 1/7 of #1432), in the browser.

One component in two sizes replaces the coloured quota sentences on both
tabs: the full meter on the Coding tab, the compact 44px line on the Board,
both fed from the one ``/api/rate-limits`` poll (the Board payload carries no
quota), and either opens the one Usage sheet. The colour rules themselves
(pace, 90%+, stale, unknown) are pinned without a browser in
``tests/js/usage_meter.test.mjs``; this file proves the rendered surfaces.

Hermetic: ``/api/rate-limits`` is answered by a fetch shim installed before
``goto()`` (#510), with resets stamped against the page's own clock so the
week reads exactly half elapsed on any day the suite runs.
"""

from __future__ import annotations

import json as _json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import close_settings_sheets, open_settings_sheet, stable_eval

pytestmark = pytest.mark.smoke

# Claude: 5h 39% (no pace, accent), week 64% at 50% elapsed (ahead of pace,
# attention). Codex: 5h 0%, week 36% (under pace, accent).
_QUOTA_SHIM = """
(() => {
  const nativeFetch = window.fetch.bind(window);
  const nowS = Date.now() / 1000;
  const halfWeek = 3.5 * 24 * 3600;
  const win = (pct, resetS, minutes) =>
    ({used_percentage: pct, resets_at: resetS, duration_minutes: minutes});
  const lines = [
    {harness: 'claude', provider: 'anthropic', label: 'Claude Code',
     state: 'available', reason: 'native_observation', stale: false,
     five_hour: win(39, nowS + 7200, 300), weekly: win(64, nowS + halfWeek, 10080)},
    {harness: 'codex', provider: 'openai', label: 'Codex',
     state: 'available', reason: 'native_observation', stale: false,
     five_hour: win(0, nowS + 9000, 300), weekly: win(36, nowS + halfWeek, 10080)},
  ];
  window.__rateLimitPolls = 0;
  window.fetch = function (input, init) {
    const url = new URL(typeof input === 'string' ? input : input.url, location.href);
    if (url.pathname === '/api/rate-limits') {
      window.__rateLimitPolls += 1;
      return Promise.resolve(new Response(JSON.stringify({quota_lines: lines}), {
        status: 200, headers: {'Content-Type': 'application/json'}}));
    }
    if (url.pathname === '/api/context-filter') {
      return Promise.resolve(new Response(JSON.stringify({
        mode: {available: true, mode: 'shadow'}, harnesses: [],
        stats: {available: true, totals: {tokens_saved: 90000, rows: 300},
                today: {tokens_saved: 12300, rows: 40},
                last_7_days: {tokens_saved: 51000, rows: 180}, per_agent: {}},
      }), {status: 200, headers: {'Content-Type': 'application/json'}}));
    }
    return nativeFetch(input, init);
  };
})();
"""

# A Board payload with no quota at all: the compact meter must still fill.
_BOARD = {
    "generated_at": "2026-09-10T12:00:00Z",
    "columns": {"backlog": [], "claude_turn": [], "your_turn": [], "other": [], "done": []},
    "github": {"fetched_at": "2026-09-10T12:00:00Z", "error": None},
    "live_sessions": {"available": True, "error": None},
    "sessions_state": {"available": True, "stale": False, "updated_at": None},
    "active_issues": {"available": True, "updated_at": None, "count": 0},
}


def _mock_board(page: Page) -> None:
    def json_route(pattern: str, body: dict) -> None:
        page.route(re.compile(pattern), lambda route: route.fulfill(
            status=200, content_type="application/json", body=_json.dumps(body)))
    json_route(r".*/api/board(\?.*)?$", _BOARD)
    json_route(r".*/api/board/chief-plan$", {"state": "empty"})
    json_route(r".*/api/claude-code/git-status$", {"projects": []})
    json_route(r".*/api/board/github/refresh$", {"fetched_at": None, "error": None})


def _no_page_overflow(page: Page) -> None:
    widths = stable_eval(
        page.locator("body"),
        "el => el.isConnected && el.clientWidth ? [el.clientWidth, el.scrollWidth] : null",
    )
    assert widths[1] <= widths[0], f"the meter widens the page: {widths}"


@pytest.mark.iphone
def test_usage_meter_full_on_code_compact_on_board_one_sheet(
    authed_page: Page, base_url: str
) -> None:
    """Both sizes render the same reading from /api/rate-limits, colour
    follows pace, and either opens the Usage sheet (✕ and Done close it)."""
    authed_page.add_init_script(_QUOTA_SHIM)
    _mock_board(authed_page)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")

    # -- full, on the Coding tab --
    full = authed_page.locator("#codingUsage .usage-meter-full")
    expect(full).to_be_visible(timeout=10_000)
    expect(full).to_have_attribute("data-tone", "attention")
    rows = full.locator(".um-row")
    expect(rows).to_have_count(2)
    expect(rows.nth(0).locator(".um-label")).to_have_text("5h")
    expect(rows.nth(0).locator(".um-pct")).to_have_text("39%")
    expect(rows.nth(0).locator(".um-fill")).to_have_attribute("data-tone", "accent")
    expect(rows.nth(1).locator(".um-label")).to_have_text("Week")
    expect(rows.nth(1).locator(".um-pct")).to_have_text("64%")
    expect(rows.nth(1).locator(".um-fill")).to_have_attribute("data-tone", "attention")
    # The pace tick rides the week bar only, at the week elapsed.
    expect(rows.nth(0).locator(".um-tick")).to_have_count(0)
    expect(rows.nth(1).locator(".um-tick")).to_have_attribute("style", re.compile(r"left: 50%"))
    expect(full.locator(".um-codex .um-codex-value")).to_have_text("5h 0% · wk 36%")
    expect(full.locator(".um-codex .um-codex-value")).to_have_attribute("data-tone", "accent")
    # The 6px bar, and a phone-width page the meter never widens.
    bar = stable_eval(rows.nth(1).locator(".um-bar"),
                      "el => el.isConnected ? el.getBoundingClientRect().height : null")
    assert bar == 6, f"week bar is {bar}px tall"
    _no_page_overflow(authed_page)

    # Tap -> the Usage sheet: exact resets, week elapsed, Codex, filter savings.
    full.click()
    sheet = authed_page.locator("#usageSheet")
    expect(sheet).to_be_visible()
    body = sheet.locator("#usageSheetBody")
    expect(body.locator('[data-row="claude-5h"] .usage-sheet-value')).to_contain_text("39% · resets ")
    expect(body.locator('[data-row="claude-week"] .usage-sheet-value')).to_contain_text("64% · resets ")
    expect(body.locator('[data-row="elapsed"] .usage-sheet-value')).to_have_text("50%")
    expect(body.locator('[data-row="claude-week"] > span')).to_have_text("Claude week")
    expect(body.locator('[data-row="codex-week"] .usage-sheet-value')).to_contain_text("36%")
    expect(body.locator('[data-row="filter-today"] .usage-sheet-value')).to_have_text(
        "12.3k saved · 40 calls")
    expect(body.locator('[data-row="filter-week"] .usage-sheet-value')).to_have_text(
        "51k saved · 180 calls")
    # The modal contract (#545): a header ✕ plus one full-width primary.
    expect(sheet.locator(".detail-header .detail-close")).to_have_count(1)
    expect(sheet.locator(".button-primary")).to_have_count(1)
    expect(sheet.locator(".button-primary")).to_have_text("Done")
    sheet.locator("#usageSheetClose").click()
    expect(sheet).to_be_hidden()

    # -- compact, on the Board, from the same poll --
    authed_page.locator("#tabBoard").click()
    compact = authed_page.locator("#boardUsage .usage-meter-compact")
    expect(compact).to_be_visible(timeout=10_000)
    expect(compact).to_have_attribute("data-tone", "attention")
    minis = compact.locator(".um-mini")
    expect(minis).to_have_count(2)
    expect(minis.nth(0)).to_have_text(re.compile(r"5h\s*39%"))
    expect(minis.nth(1)).to_have_text(re.compile(r"wk\s*64%"))
    expect(minis.nth(1).locator(".um-tick")).to_have_count(1)
    expect(compact.locator(".um-codex-value")).to_have_text("36%")
    expect(compact.locator(".um-divider")).to_have_count(1)
    expect(compact.locator('.um-chevron use[href="#i-chevron-right"]')).to_have_count(1)
    # One 44px line, every part inside the button.
    box = stable_eval(compact, """el => {
      if (!el.isConnected) return null;
      const r = el.getBoundingClientRect();
      const inside = Array.from(el.children).every(c => {
        const b = c.getBoundingClientRect();
        return b.left >= r.left - 0.5 && b.right <= r.right + 0.5;
      });
      return {height: r.height, inside};
    }""")
    assert box["height"] == 44, f"compact meter is {box['height']}px tall"
    assert box["inside"], "a part of the compact meter spills out of it"
    _no_page_overflow(authed_page)

    compact.click()
    expect(sheet).to_be_visible()
    sheet.locator("#usageSheetDone").click()
    expect(sheet).to_be_hidden()


@pytest.mark.iphone
def test_usage_meter_dims_stale_and_keeps_unknown_text(
    authed_page: Page, base_url: str
) -> None:
    """A stale reading dims and carries a chip; nothing measured keeps the
    state word as text (full) and "n/a" (compact); 90%+ is danger; a reading
    that goes unknown keeps the last numbers, dimmed, chipped "unknown"."""
    # The app's own poll never answers, so only the renders below paint.
    authed_page.add_init_script("""
      (() => {
        const nativeFetch = window.fetch.bind(window);
        window.fetch = function (input, init) {
          const url = new URL(typeof input === 'string' ? input : input.url, location.href);
          if (url.pathname === '/api/rate-limits') return new Promise(() => {});
          return nativeFetch(input, init);
        };
      })();
    """)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    expect(authed_page.locator("#codingUsage")).to_be_attached()

    read = authed_page.evaluate("""async () => {
      const m = await import('/static/usage-meter.js');
      const nowS = Date.now() / 1000;
      const win = (pct) => ({used_percentage: pct, resets_at: nowS + 3600, duration_minutes: 300});
      const snap = () => {
        const full = document.querySelector('#codingUsage .usage-meter');
        const compact = document.querySelector('#boardUsage .usage-meter');
        return {
          tone: full.dataset.tone, stale: full.dataset.stale || '',
          chips: Array.from(full.querySelectorAll('.um-head .chip')).map(c => [c.textContent, c.dataset.tone]),
          note: (full.querySelector('.um-note') || {}).textContent || '',
          pcts: Array.from(full.querySelectorAll('.um-pct')).map(e => e.textContent),
          codex: full.querySelector('.um-codex-value').textContent,
          compactCodex: compact.querySelector('.um-codex-value').textContent,
          compactNote: (compact.querySelector('.um-note') || {}).textContent || '',
          dim: getComputedStyle(full.querySelector('.um-windows') || full).opacity,
        };
      };
      const out = {};
      m.renderUsage([
        {harness: 'claude', label: 'Claude Code', state: 'stale', stale: true,
         five_hour: win(91), weekly: win(20)},
        {harness: 'codex', label: 'Codex', state: 'unsupported', five_hour: null, weekly: null},
      ]);
      out.stale = snap();
      m.renderUsage([
        {harness: 'claude', label: 'Claude Code', state: 'unknown', five_hour: null, weekly: null},
        {harness: 'codex', label: 'Codex', state: 'unsupported', five_hour: null, weekly: null},
      ]);
      out.fallback = snap();
      return out;
    }""")

    stale = read["stale"]
    assert stale["tone"] == "danger", stale            # 91% in the 5h window
    assert stale["stale"] == "true"
    assert stale["chips"] == [["stale", "neutral"]]
    assert float(stale["dim"]) < 1
    assert stale["pcts"] == ["91%", "20%"]
    assert stale["codex"] == "unsupported"
    assert stale["compactCodex"] == "n/a"

    # Unknown now, with the stale reading behind it: kept, dimmed, chipped.
    fallback = read["fallback"]
    assert fallback["pcts"] == ["91%", "20%"]
    assert fallback["chips"] == [["unknown", "neutral"]]
    assert fallback["stale"] == "true"
    assert fallback["note"] == ""

    # Never measured on this page: the state word stays as text. A second
    # module URL is a fresh instance, with no last reading to fall back to.
    never = authed_page.evaluate("""async () => {
      const m = await import('/static/usage-meter.js?fresh=1');
      m.renderUsage([
        {harness: 'claude', label: 'Claude Code', state: 'unknown', five_hour: null, weekly: null},
        {harness: 'codex', label: 'Codex', state: 'error', five_hour: null, weekly: null},
      ]);
      const full = document.querySelector('#codingUsage .usage-meter');
      return {
        note: full.querySelector('.um-note').textContent,
        codex: full.querySelector('.um-codex-value').textContent,
        bars: full.querySelectorAll('.um-bar').length,
        compactNote: document.querySelector('#boardUsage .um-note').textContent,
      };
    }""")
    assert never == {
        "note": "Quota unknown", "codex": "unavailable", "bars": 0, "compactNote": "unknown",
    }, never


def test_code_usage_card_leads_names_the_pace_and_opens_the_sheet(
    authed_page: Page, base_url: str
) -> None:
    """#1434: the full meter sits in the Code tab's first card. Its summary
    names the pace in its tone (the shim's week is ahead of pace), its
    footer carries the context filter's savings (the green badge is gone),
    and a tap anywhere on the card opens the Usage sheet."""
    authed_page.add_init_script(_QUOTA_SHIM)
    _mock_board(authed_page)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")

    card = authed_page.locator("#codingUsageCard")
    expect(card.locator("#codingUsage .usage-meter-full")).to_be_visible(timeout=10_000)
    meta = card.locator("#codingUsageMeta")
    expect(meta).to_have_text("ahead of pace")
    expect(meta).to_have_attribute("data-tone", "attention")
    expect(card.locator("#codingUsageFooter")).to_have_text(
        "Context filter saved 12.3k tokens today")
    expect(authed_page.locator("#paneClaude .usage-badge")).to_have_count(0)

    # The card's title is part of the tap, not only the meter.
    card.locator("#codingUsageTitle").click()
    sheet = authed_page.locator("#usageSheet")
    expect(sheet).to_be_visible()
    sheet.locator("#usageSheetDone").click()
    expect(sheet).to_be_hidden()


def _serve_usage_shows(page: Page, current: dict) -> list:
    """Serve /api/config with ``current["usage_shows"]`` and answer its POSTs
    from memory, so the page is driven by the setting without writing the
    box's real config. Returns the POST bodies seen."""
    posted: list = []

    def _config(route):
        if route.request.method == "POST":
            body = route.request.post_data_json
            posted.append(body)
            if "usage_shows" in body:
                current["usage_shows"] = body["usage_shows"]
            route.fulfill(status=200, content_type="application/json",
                          body=_json.dumps({"ok": True, "claude_flags": ""}))
            return
        resp = route.fetch()
        data = resp.json()
        data["usage_shows"] = current["usage_shows"]
        route.fulfill(response=resp, json=data)

    page.route(re.compile(r".*/api/config$"), _config)
    return posted


@pytest.mark.parametrize("mode", ["claude", "codex", "both", "none"])
def test_usage_shows_draws_only_the_chosen_providers(
    authed_page: Page, base_url: str, mode: str
) -> None:
    """#1451: the "Usage shows" setting. Claude / Codex draw only that
    provider (no row, mark, or word of the other, in the card, the Board line
    or the accessible name); both is today's meter; none hides the Code tab's
    Usage card and empties the Board line, with the rest of the tab intact."""
    errors: list = []
    authed_page.on("pageerror", lambda exc: errors.append(str(exc)))
    authed_page.add_init_script(_QUOTA_SHIM)
    _mock_board(authed_page)
    _serve_usage_shows(authed_page, {"usage_shows": mode})
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")

    card = authed_page.locator("#codingUsageCard")
    full = authed_page.locator("#codingUsage .usage-meter-full")
    if mode == "none":
        # The poll lands, then the card stays hidden: wait on the poll.
        authed_page.wait_for_function("window.__rateLimitPolls >= 1")
        expect(card).to_be_hidden()
        expect(authed_page.locator("#codingUsage .usage-meter")).to_have_count(0)
        expect(authed_page.locator("#codingUsageMeta")).to_have_text("")
        # Nothing else on the tab breaks: the rest of the pane still renders.
        expect(authed_page.locator("#paneClaude")).to_be_visible()
        authed_page.locator("#tabBoard").click()
        expect(authed_page.locator("#paneBoard")).to_be_visible()
        expect(authed_page.locator("#boardUsage .usage-meter")).to_have_count(0)
        assert errors == [], errors
        return

    expect(full).to_be_visible(timeout=10_000)
    expect(card).to_be_visible()
    names = {"claude": ["Claude Code"], "codex": ["Codex"],
             "both": ["Claude Code", "Codex"]}[mode]
    expect(full.locator(".um-name")).to_have_text(names)
    expect(full.locator(".um-mark")).to_have_count(len(names))
    label = full.get_attribute("aria-label")
    assert ("Claude" in label) == (mode in ("claude", "both")), label
    assert ("Codex" in label) == (mode in ("codex", "both")), label
    if mode == "claude":
        expect(full.locator(".um-codex")).to_have_count(0)
        expect(full.locator('use[href="#b-codex"]')).to_have_count(0)
        expect(full.locator(".um-row").nth(1).locator(".um-pct")).to_have_text("64%")
    elif mode == "codex":
        # Codex leads: its own two windows, no Claude row or mark.
        expect(full.locator(".um-codex")).to_have_count(0)
        expect(full.locator('use[href="#b-claude"]')).to_have_count(0)
        rows = full.locator(".um-row")
        expect(rows).to_have_count(2)
        expect(rows.nth(0).locator(".um-pct")).to_have_text("0%")
        expect(rows.nth(1).locator(".um-pct")).to_have_text("36%")
    else:
        expect(full.locator(".um-codex .um-codex-value")).to_have_text("5h 0% · wk 36%")

    # The sheet follows: the other provider's rows are not in it.
    full.click()
    body = authed_page.locator("#usageSheetBody")
    expect(body.locator('[data-row^="claude-"]')).to_have_count(2 if mode in ("claude", "both") else 0)
    expect(body.locator('[data-row^="codex-"]')).to_have_count(2 if mode in ("codex", "both") else 0)
    authed_page.locator("#usageSheetClose").click()

    authed_page.locator("#tabBoard").click()
    compact = authed_page.locator("#boardUsage .usage-meter-compact")
    expect(compact).to_be_visible(timeout=10_000)
    expect(compact.locator(".um-mark")).to_have_count(len(names))
    expect(compact.locator(".um-divider")).to_have_count(1 if mode == "both" else 0)
    expect(compact.locator(".um-codex-value")).to_have_count(1 if mode == "both" else 0)
    expect(compact.locator(".um-mini")).to_have_count(2)
    clabel = compact.get_attribute("aria-label")
    assert ("Claude" in clabel) == (mode in ("claude", "both")), clabel
    assert ("Codex" in clabel) == (mode in ("codex", "both")), clabel
    assert errors == [], errors


def test_usage_shows_setting_saves_and_repaints_the_card(
    authed_page: Page, base_url: str
) -> None:
    """#1451: the Settings control is the four-way pill row in its own sheet
    (#1435: Settings > Usage meter). It
    defaults to Both, a tap saves at once as one POST, and the Usage card
    repaints without a reload (None hides it, Both brings it back)."""
    authed_page.add_init_script(_QUOTA_SHIM)
    _mock_board(authed_page)
    current = {"usage_shows": "both"}
    posted = _serve_usage_shows(authed_page, current)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    expect(authed_page.locator("#codingUsage .usage-meter-full")).to_be_visible(timeout=10_000)

    open_settings_sheet(authed_page, "usageShowsSheet")
    tabs = authed_page.locator("#usageShows .range-tab")
    expect(tabs).to_have_text(["Claude", "Codex", "Both", "None"])
    expect(authed_page.locator("#usageShows .range-tab.active")).to_have_text("Both")

    tabs.nth(3).click()
    expect(authed_page.locator("#usageShows .range-tab.active")).to_have_text("None")
    assert [p for p in posted if "usage_shows" in p] == [{"usage_shows": "none"}], posted
    # The Code pane is not showing, so read the card's own hidden state.
    card = authed_page.locator("#codingUsageCard")
    expect(card).to_have_attribute("hidden", "")

    tabs.nth(0).click()
    expect(authed_page.locator("#usageShows .range-tab.active")).to_have_text("Claude")
    expect(card).not_to_have_attribute("hidden", "")
    # An open sheet makes the nav inert: close it before leaving Settings.
    close_settings_sheets(authed_page)
    authed_page.locator("#tabClaude").click()
    expect(card).to_be_visible()
    expect(authed_page.locator("#codingUsage .um-codex")).to_have_count(0)
