"""The usage meter (issue #1433, step 1/7 of #1432), in the browser.

One component replaces the coloured quota sentences: the meter in the Code
tab's Usage card, fed from the ``/api/rate-limits`` poll, opening the Usage
sheet. The Board's compact line left with #1436 (usage stays on Code), so the
Board draws no meter at all. The colour rules themselves
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

# The app's own rate-limits poll never answers, so only a test's own renders paint.
_NEVER_ANSWER_RATE_LIMITS = """
(() => {
  const nativeFetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    const url = new URL(typeof input === 'string' ? input : input.url, location.href);
    if (url.pathname === '/api/rate-limits') return new Promise(() => {});
    return nativeFetch(input, init);
  };
})();
"""

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
    {harness: 'grok', provider: 'xai', label: 'Grok',
     state: 'unsupported', reason: 'limits_not_exposed', stale: false,
     five_hour: null, weekly: null,
     cost_today: {state: 'available', reason: 'grok_usage_ledger', usd: 15.663}},
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

# A Board payload with no quota at all (the Board draws no meter, #1436).
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
def test_usage_meter_on_code_opens_the_sheet_and_is_not_on_the_board(
    authed_page: Page, base_url: str
) -> None:
    """The meter renders the reading from /api/rate-limits, colour follows
    pace, and it opens the Usage sheet (✕ and Done close it). The Board has
    no usage line since #1436."""
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

    full.click()
    expect(sheet).to_be_visible()
    sheet.locator("#usageSheetDone").click()
    expect(sheet).to_be_hidden()

    # -- not on the Board (#1436): usage stays on Code --
    authed_page.locator("#tabBoard").click()
    expect(authed_page.locator("#paneBoard")).to_be_visible()
    expect(authed_page.locator("#paneBoard .usage-meter, #boardUsage")).to_have_count(0)


@pytest.mark.iphone
def test_usage_meter_dims_stale_and_keeps_unknown_text(
    authed_page: Page, base_url: str
) -> None:
    """A stale reading dims and carries a chip; nothing measured keeps the
    state word as text; 90%+ is danger; a reading that goes unknown keeps the
    last numbers, dimmed, chipped "unknown"."""
    # The app's own poll never answers, so only the renders below paint.
    authed_page.add_init_script(_NEVER_ANSWER_RATE_LIMITS)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    expect(authed_page.locator("#codingUsage")).to_be_attached()

    read = authed_page.evaluate("""async () => {
      const m = await import('/static/usage-meter.js');
      const nowS = Date.now() / 1000;
      const win = (pct) => ({used_percentage: pct, resets_at: nowS + 3600, duration_minutes: 300});
      const snap = () => {
        const full = document.querySelector('#codingUsage .usage-meter');
        return {
          tone: full.dataset.tone, stale: full.dataset.stale || '',
          chips: Array.from(full.querySelectorAll('.um-head .chip')).map(c => [c.textContent, c.dataset.tone]),
          note: (full.querySelector('.um-note') || {}).textContent || '',
          pcts: Array.from(full.querySelectorAll('.um-pct')).map(e => e.textContent),
          codex: full.querySelector('.um-codex-value').textContent,
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
      };
    }""")
    assert never == {"note": "Quota unknown", "codex": "unavailable", "bars": 0}, never


@pytest.mark.iphone
@pytest.mark.parametrize("shown", ["claude", "codex"])
def test_usage_meter_single_harness_keeps_its_chip_without_the_heading(
    authed_page: Page, base_url: str, shown: str
) -> None:
    """#1510: with one harness shown the heading row is gone, but a chip on
    that reading (stale) is still visible, ahead of the windows."""
    authed_page.add_init_script(_NEVER_ANSWER_RATE_LIMITS)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    expect(authed_page.locator("#codingUsage")).to_be_attached()

    authed_page.evaluate("""async (shown) => {
      const m = await import('/static/usage-meter.js');
      const nowS = Date.now() / 1000;
      const win = (pct) => ({used_percentage: pct, resets_at: nowS + 3600, duration_minutes: 300});
      m.setUsageShows([shown]);
      m.renderUsage([
        {harness: 'claude', label: 'Claude Code', state: 'stale', stale: true,
         five_hour: win(40), weekly: win(20)},
        {harness: 'codex', label: 'Codex', state: 'stale', stale: true,
         five_hour: win(40), weekly: win(20)},
      ]);
    }""", shown)

    full = authed_page.locator("#codingUsage .usage-meter-full")
    expect(full.locator(".um-head .chip")).to_have_text("stale")
    expect(full.locator(".um-name")).to_have_count(0)
    expect(full.locator(".um-mark")).to_have_count(0)
    expect(full.locator(".um-row")).to_have_count(2)
    _no_page_overflow(authed_page)


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


@pytest.mark.iphone
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_usage_rows_share_the_cards_left_and_right_edges(
    authed_page: Page, base_url: str, theme: str
) -> None:
    """#1541: the 5h / Week labels start on the card's content edge, level
    with the heading icon and the context-filter footer, and the reset column
    ends where the heading's right-hand text ends. The meter used to inset
    its rows 4px on both sides. Read in one stable_eval (#1346)."""
    authed_page.add_init_script(_QUOTA_SHIM)
    _mock_board(authed_page)
    authed_page.set_viewport_size({"width": 390, "height": 844})
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    authed_page.evaluate("t => document.documentElement.setAttribute('data-theme', t)", theme)
    card = authed_page.locator("#codingUsageCard")
    expect(card.locator("#codingUsage .um-row")).to_have_count(2, timeout=10_000)
    # The static placeholder has both rows but an empty heading until the first poll.
    expect(card.locator("#codingUsageMeta")).to_have_text(re.compile(r"\S"))
    expect(card.locator("#codingUsageFooter")).to_be_visible()
    m = stable_eval(card, """card => {
      if (!card.isConnected) return null;
      const text = (el) => {
        const range = document.createRange();
        range.selectNodeContents(el);
        return range.getBoundingClientRect();
      };
      const rows = Array.from(card.querySelectorAll('.um-row'));
      return {
        icon: card.querySelector('#codingUsageTitle .icon').getBoundingClientRect().left,
        footer: text(card.querySelector('#codingUsageFooter')).left,
        labels: rows.map(r => text(r.querySelector('.um-label')).left),
        meta: text(card.querySelector('#codingUsageMeta')).right,
        resets: rows.map(r => r.querySelector('.um-reset').getBoundingClientRect().right),
      };
    }""")
    for left in m["labels"]:
        assert abs(left - m["footer"]) <= 1, f"row label vs footer text ({theme}): {m}"
        assert abs(left - m["icon"]) <= 1, f"row label vs heading icon ({theme}): {m}"
    for right in m["resets"]:
        assert abs(right - m["meta"]) <= 1, f"reset column vs heading text ({theme}): {m}"


def _serve_usage_shows(page: Page, current: dict) -> list:
    """Serve /api/config with ``current["usage_providers"]`` and answer its
    POSTs from memory, so the page is driven by the setting without writing
    the box's real config. Returns the POST bodies seen."""
    posted: list = []

    def _config(route):
        if route.request.method == "POST":
            body = route.request.post_data_json
            posted.append(body)
            if "usage_providers" in body:
                current["usage_providers"] = body["usage_providers"]
            route.fulfill(status=200, content_type="application/json",
                          body=_json.dumps({"ok": True, "claude_flags": ""}))
            return
        resp = route.fetch()
        data = resp.json()
        data["usage_providers"] = current["usage_providers"]
        route.fulfill(response=resp, json=data)

    page.route(re.compile(r".*/api/config$"), _config)
    return posted


_NAMES = {"claude": "Claude Code", "codex": "Codex", "grok": "Grok"}


@pytest.mark.parametrize("providers", [
    ["claude"], ["codex"], ["claude", "codex"], [], ["claude", "codex", "grok"], ["grok"],
])
def test_usage_shows_draws_only_the_chosen_providers(
    authed_page: Page, base_url: str, providers: list
) -> None:
    """The "Usage shows" switches. A provider switched off leaves no row, mark
    or word in the card or its accessible name; all off hides the Code tab's
    Usage card, with the rest of the tab intact. Grok never draws a bar: its
    row is today's spend."""
    errors: list = []
    authed_page.on("pageerror", lambda exc: errors.append(str(exc)))
    authed_page.add_init_script(_QUOTA_SHIM)
    _mock_board(authed_page)
    _serve_usage_shows(authed_page, {"usage_providers": providers})
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")

    card = authed_page.locator("#codingUsageCard")
    full = authed_page.locator("#codingUsage .usage-meter-full")
    if not providers:
        # The poll lands, then the card stays hidden: wait on the poll.
        authed_page.wait_for_function("window.__rateLimitPolls >= 1")
        expect(card).to_be_hidden()
        expect(authed_page.locator("#codingUsage .usage-meter")).to_have_count(0)
        expect(authed_page.locator("#codingUsageMeta")).to_have_text("")
        # Nothing else on the tab breaks: the rest of the pane still renders.
        expect(authed_page.locator("#paneClaude")).to_be_visible()
        assert errors == [], errors
        return

    expect(full).to_be_visible(timeout=10_000)
    expect(card).to_be_visible()
    # The harness heading (mark + name) shows only when more than one
    # provider is shown; alone, the content sits straight under the card title.
    if len(providers) > 1:
        expect(full.locator(".um-name")).to_have_text([_NAMES[p] for p in providers])
        expect(full.locator(".um-mark")).to_have_count(len(providers))
    else:
        expect(full.locator(".um-head")).to_have_count(0)
        expect(full.locator(".um-name")).to_have_count(0)
        expect(full.locator(".um-mark")).to_have_count(0)
    label = full.get_attribute("aria-label")
    for provider in ("claude", "codex", "grok"):
        assert (_NAMES[provider] in label) == (provider in providers), label
        if provider not in providers:
            expect(full.locator(f'use[href="#b-{provider}"]')).to_have_count(0)
    if providers == ["claude"]:
        expect(full.locator(".um-row")).to_have_count(2)
        expect(full.locator(".um-row").nth(1).locator(".um-pct")).to_have_text("64%")
    elif providers == ["codex"]:
        # Codex leads: its own two windows, no Claude row or mark.
        rows = full.locator(".um-row")
        expect(rows).to_have_count(2)
        expect(rows.nth(0).locator(".um-pct")).to_have_text("0%")
        expect(rows.nth(1).locator(".um-pct")).to_have_text("36%")
    elif providers == ["grok"]:
        # Grok alone: no bar at all, just its spend.
        expect(full.locator(".um-bar")).to_have_count(0)
        expect(full.locator(".um-note")).to_have_text("$15.66 today")
    else:
        expect(full.locator(".um-codex .um-codex-value")).to_have_text("5h 0% · wk 36%")
    grok_row = full.locator(".um-grok")
    if "grok" in providers and len(providers) > 1:
        expect(grok_row.locator(".um-codex-value")).to_have_text("$15.66 today")
        expect(grok_row.locator(".um-bar")).to_have_count(0)
    else:
        expect(grok_row).to_have_count(0)

    # The sheet follows: a provider switched off has no rows in it.
    full.click()
    body = authed_page.locator("#usageSheetBody")
    expect(body.locator('[data-row^="claude-"]')).to_have_count(2 if "claude" in providers else 0)
    expect(body.locator('[data-row^="codex-"]')).to_have_count(2 if "codex" in providers else 0)
    expect(body.locator('[data-row^="grok-"]')).to_have_count(2 if "grok" in providers else 0)
    authed_page.locator("#usageSheetClose").click()
    assert errors == [], errors


def test_usage_shows_setting_saves_and_repaints_the_card(
    authed_page: Page, base_url: str
) -> None:
    """The Settings control is one switch per provider in its own sheet
    (#1435: Settings > Usage meter). Claude and Codex start on, Grok off; a
    tap saves the whole set at once as one POST, and the Usage card repaints
    without a reload (all off hides it, Grok alone brings it back)."""
    authed_page.add_init_script(_QUOTA_SHIM)
    _mock_board(authed_page)
    current = {"usage_providers": ["claude", "codex"]}
    posted = _serve_usage_shows(authed_page, current)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    expect(authed_page.locator("#codingUsage .usage-meter-full")).to_be_visible(timeout=10_000)

    open_settings_sheet(authed_page, "usageShowsSheet")
    switch = {
        p: authed_page.locator(f'#usageShows [data-provider="{p}"]')
        for p in ("claude", "codex", "grok")
    }
    expect(switch["claude"]).to_have_attribute("aria-checked", "true")
    expect(switch["codex"]).to_have_attribute("aria-checked", "true")
    expect(switch["grok"]).to_have_attribute("aria-checked", "false")

    switch["claude"].click()
    expect(switch["claude"]).to_have_attribute("aria-checked", "false")
    switch["codex"].click()
    expect(switch["codex"]).to_have_attribute("aria-checked", "false")
    assert [p for p in posted if "usage_providers" in p] == [
        {"usage_providers": ["codex"]}, {"usage_providers": []},
    ], posted
    # The Code pane is not showing, so read the card's own hidden state.
    card = authed_page.locator("#codingUsageCard")
    expect(card).to_have_attribute("hidden", "")

    switch["grok"].click()
    expect(switch["grok"]).to_have_attribute("aria-checked", "true")
    expect(card).not_to_have_attribute("hidden", "")
    # An open sheet makes the nav inert: close it before leaving Settings.
    close_settings_sheets(authed_page)
    authed_page.locator("#tabClaude").click()
    expect(card).to_be_visible()
    expect(authed_page.locator("#codingUsage .um-codex")).to_have_count(0)
    expect(authed_page.locator("#codingUsage .um-note")).to_have_text("$15.66 today")


# Holds the /api/rate-limits answer until the test releases it, so the Board
# is measured both before and after the meter lands. Installed after
# _QUOTA_SHIM, whose fetch it wraps.
_HOLD_RATE_LIMITS = """
(() => {
  const inner = window.fetch.bind(window);
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  window.__releaseRateLimits = () => release();
  window.fetch = function (input, init) {
    const url = new URL(typeof input === 'string' ? input : input.url, location.href);
    if (url.pathname === '/api/rate-limits') return gate.then(() => inner(input, init));
    return inner(input, init);
  };
})();
"""


@pytest.mark.iphone
def test_board_does_not_jump_when_the_usage_meter_lands(
    authed_page: Page, base_url: str
) -> None:
    """#1447: the first /api/rate-limits answer must not move the Board. It
    once filled an empty meter slot above the lanes and pushed every column,
    and an open drawer with it, down 44px mid-gesture, so a test reading a
    drawer's four buttons one after another straddled the jump and saw the
    fourth a row lower. Since #1436 the Board draws no meter at all; this
    pins that the reading landing still leaves the lanes where they were."""
    authed_page.add_init_script(_QUOTA_SHIM)
    authed_page.add_init_script(_HOLD_RATE_LIMITS)
    _mock_board(authed_page)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    authed_page.locator("#tabBoard").click()
    columns = authed_page.locator("#boardColumns")
    expect(columns).to_be_visible()
    top = "el => el.isConnected ? Math.round(el.getBoundingClientRect().top) : null"

    before = stable_eval(columns, top)
    # No reading yet: the slot holds the dash placeholder (#1641), not a meter.
    expect(authed_page.locator("#codingUsage .usage-meter:not([data-pending])")).to_have_count(0)

    authed_page.evaluate("window.__releaseRateLimits()")
    # The reading has landed and rendered (on the Code tab's card).
    expect(authed_page.locator("#codingUsage .usage-meter-full")).to_be_attached()
    expect(authed_page.locator("#paneBoard .usage-meter")).to_have_count(0)
    after = stable_eval(columns, top)
    assert after == before, (
        f"the Board jumped {after - before}px when the usage meter landed"
    )
