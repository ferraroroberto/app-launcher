"""Regression pin for #1134: a text-size control in Settings.

The viewport meta locks pinch-zoom (the app reads as a native shell), so
until now there was no way to make the text bigger (WCAG 1.4.4). Settings
now carries the vendored text-size segmented control: Small / Default / Large
scale the root font-size, so every rem size follows and px geometry (nav,
rows, hit targets) does not. Since #1506 the row is drawn as one segmented
track (design.md `segmented`), with the same hooks, key and boot.

The pre-paint boot script stamps html[data-textsize] from
``app-launcher.textsize`` before any stylesheet is parsed, so a reload or a
PWA relaunch never paints at the wrong size first.
"""
from __future__ import annotations

import json

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.fixtures.ui import close_settings_sheets, open_settings_sheet
from tests.e2e.fixtures.waits import stable_eval, stable_read

pytestmark = [pytest.mark.smoke, pytest.mark.iphone]

_TABS = ["#tabClaude", "#tabBoard", "#tabLifeOS", "#tabApps", "#tabJobs"]

# Settings sheets (#1435) are dialogs outside the pane, so the clip scan
# reads each while it is open; a light pass over all of them at Large/320px.
_SHEETS = [
    "screenTimeSheet", "usageShowsSheet", "launchDefaultsSheet", "chiefSheet",
    "channelsSheet", "contextFilterSheet", "tokensSheet", "foldersSheet",
    "passkeysSheet", "terminalSheet",
]

# Records, on the first data-textsize stamp, how many stylesheets the parser
# had already reached. The boot script sits above every <link>, so a no-flash
# boot stamps at zero.
_RECORD_FIRST_STAMP = """
(() => {
  if (!sessionStorage.getItem('ts-probe-armed')) return;
  new MutationObserver((records, obs) => {
    for (const r of records) {
      if (r.attributeName === 'data-textsize') {
        window.__firstStamp = {
          size: document.documentElement.dataset.textsize,
          sheets: document.querySelectorAll('link[rel="stylesheet"]').length,
        };
        obs.disconnect();
        return;
      }
    }
  }).observe(document, { subtree: true, attributes: true, attributeFilter: ['data-textsize'] });
})();
"""


# Every element in the open pane that clips its own content vertically
# (overflow-y hidden or clip) with text cut off at its inner edge: a row cut
# off at Large. It measures the text's own line boxes against the element's
# inner box, not scrollHeight, which also counts a ::before hit-area
# expansion (#1124's `inset: -5px 0` on the compact controls) that clips
# nothing. Only text whose nearest clipping ancestor is this element counts,
# so a scroller nested inside is not read as clipped. Intentional clamps are
# left out: a -webkit-line-clamp title (the Board's two-line cap) ends in an
# ellipsis by design, and a box with no height is a collapsed disclosure.
# xterm keeps its own font size (#1134) and is skipped. Returns how many
# clipping boxes were inspected and the offenders, named by tag, id, classes.
# The scope defaults to the open pane; a Settings sheet passes its own selector.
_CLIPPED_ROWS = """
(scope) => {
  const pane = document.querySelector(
    scope || 'main.app > section.pane:not([hidden])') || document.body;
  const name = (el) => el.tagName.toLowerCase() + (el.id ? '#' + el.id : '')
    + Array.from(el.classList).map((c) => '.' + c).join('');
  const clips = (el) => {
    const y = getComputedStyle(el).overflowY;
    return y !== 'visible';
  };
  let inspected = 0;
  const clipped = [];
  pane.querySelectorAll('*').forEach((el) => {
    if (el.closest('.xterm') || !el.getClientRects().length) return;
    const cs = getComputedStyle(el);
    if (cs.overflowY !== 'hidden' && cs.overflowY !== 'clip') return;
    if (cs.webkitLineClamp && cs.webkitLineClamp !== 'none') return;
    if (el.clientHeight === 0) return;
    const box = el.getBoundingClientRect();
    const top = box.top + el.clientTop;
    const bottom = top + el.clientHeight;
    let over = 0;
    let texts = 0;
    const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      if (!node.textContent.trim()) continue;
      let owner = node.parentElement;
      while (owner !== el && !clips(owner)) owner = owner.parentElement;
      if (owner !== el) continue;
      const range = document.createRange();
      range.selectNodeContents(node);
      for (const r of range.getClientRects()) {
        if (!r.width || !r.height) continue;
        texts++;
        over = Math.max(over, top - r.top, r.bottom - bottom);
      }
    }
    if (!texts) return;
    inspected++;
    if (over > 1) clipped.push(name(el) + ' +' + Math.round(over) + 'px');
  });
  return { inspected, clipped };
}
"""


def _open_settings(page: Page) -> None:
    page.locator(".settings-open-btn:visible").first.click()
    expect(page.locator("#paneSettings")).to_be_visible()


# The control's look (#1506, design.md `segmented`): one track with a 3px
# inset, borderless segments touching inside it, every label in fg, and only
# the selected segment filled (the card surface). Each segment is the control
# height with a 44px tap band.
_SEGMENTED = """
(nav) => {
  const cs = getComputedStyle(nav);
  const segs = Array.from(nav.querySelectorAll('.segmented-item'));
  const on = nav.querySelector('.segmented-item[aria-pressed=true]');
  const fg = getComputedStyle(document.body).color;
  const band = getComputedStyle(segs[0], '::before');
  return {
    track: cs.backgroundColor,
    inset: cs.paddingTop,
    gap: cs.columnGap,
    borders: segs.map((s) => getComputedStyle(s).borderTopWidth),
    labelsFg: segs.every((s) => getComputedStyle(s).color === fg),
    filled: segs.filter((s) => getComputedStyle(s).backgroundColor !== 'rgba(0, 0, 0, 0)')
      .map((s) => s.dataset.textsize),
    selected: on ? on.dataset.textsize : null,
    height: segs[0].getBoundingClientRect().height,
    tapBand: segs[0].getBoundingClientRect().height
      - parseFloat(band.top) - parseFloat(band.bottom),
  };
}
"""


def _assert_one_segmented_track(control) -> None:
    geo = stable_eval(control, _SEGMENTED)
    assert geo["track"] not in ("rgba(0, 0, 0, 0)", "transparent"), geo
    assert geo["inset"] == "3px" and geo["gap"] in ("0px", "normal"), geo
    assert set(geo["borders"]) == {"0px"}, geo
    assert geo["labelsFg"], geo
    assert geo["filled"] == [geo["selected"]], geo
    assert geo["height"] == 30 and geo["tapBand"] >= 44, geo


def _assert_small_and_default_steps(page: Page) -> None:
    """Formerly ``test_small_and_default_steps``."""
    control = page.locator("#textSizeControl")
    control.locator("[data-textsize='small']").click()
    expect(page.locator("body")).to_have_css("font-size", "15px")
    control.locator("[data-textsize='default']").click()
    expect(page.locator("body")).to_have_css("font-size", "16px")
    assert page.evaluate("localStorage.getItem('app-launcher.textsize')") == "default"


def _assert_large_at_320px_never_scrolls_or_clips(page: Page) -> None:
    """Formerly ``test_large_at_320px_never_scrolls_sideways``, which seeded
    Large through an init script; here the stored step is the Large this
    test just chose, and it reloads at 320px. Acceptance criterion 1 in
    full: no sideways scroll, and no row cut off vertically, on any tab or
    in Settings."""
    page.set_viewport_size({"width": 320, "height": 700})
    page.reload(wait_until="domcontentloaded")
    expect(page.locator("body")).to_have_css("font-size", "18px")

    def overflow() -> object:
        return page.evaluate(
            "() => { const el = document.scrollingElement;"
            " return el.scrollWidth > 0 ? el.scrollWidth - el.clientWidth : null; }"
        )

    def clipped(where: str, scope: object = None) -> int:
        found = stable_read(lambda: page.evaluate(_CLIPPED_ROWS, scope))
        assert not found["clipped"], (
            f"{where} at Large/320px cuts off rows: {found['clipped']}")
        return found["inspected"]

    inspected = 0
    for tab in _TABS:
        page.locator(tab).click()
        page.wait_for_timeout(300)
        spill = stable_read(overflow)
        assert spill <= 0, f"{tab} at Large/320px scrolls sideways by {spill}px"
        inspected += clipped(tab)

    _open_settings(page)
    spill = stable_read(overflow)
    assert spill <= 0, f"Settings at Large/320px scrolls sideways by {spill}px"
    inspected += clipped("Settings")

    # Each sheet is a closed dialog until its row is tapped: open it, check
    # the page and the sheet's own card for sideways spill and clipped rows.
    # The card's sideways spill, and what reaches past its right edge.
    def card_spill() -> object:
        return page.evaluate(
            """() => {
              const c = document.querySelector('dialog.settings-sheet[open] .detail-card');
              if (!c) return null;
              const edge = c.getBoundingClientRect().right;
              const past = Array.from(c.querySelectorAll('*'))
                .filter((el) => el.getBoundingClientRect().right > edge + 0.5)
                .map((el) => el.tagName.toLowerCase() + '.' + el.className);
              return { spill: c.scrollWidth - c.clientWidth, past: past };
            }"""
        )

    for sheet_id in _SHEETS:
        open_settings_sheet(page, sheet_id)
        sel = f"#{sheet_id}"
        spill = stable_read(overflow)
        assert spill <= 0, f"{sheet_id} at Large/320px scrolls the page sideways by {spill}px"
        card = stable_read(card_spill)
        # scrollWidth is an integer rounded up from fractional boxes, so a
        # 1px spill with nothing past the edge is rounding, not overflow.
        assert card is not None and card["spill"] <= 1 and not card["past"], (
            f"{sheet_id} at Large/320px scrolls its card sideways by "
            f"{card['spill']}px: {card['past']}")
        inspected += clipped(sheet_id, sel)
        close_settings_sheets(page)
    # The scan found real clipping boxes to check, so a pass means something.
    assert inspected > 0, "no clipping boxes found on any tab: the scan checked nothing"


def test_large_sets_18px_body_and_survives_reload(
    authed_page: Page, base_url: str
) -> None:
    """The whole #1134 control on one page load (merged by #1215): the
    default step, Small and Default, Large surviving a reload with a no-flash
    first stamp, and Large at 320px never scrolling sideways or clipping a
    row."""
    page = authed_page
    page.add_init_script(_RECORD_FIRST_STAMP)
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    _open_settings(page)

    control = page.locator("#textSizeControl")
    expect(control.locator(".segmented-item")).to_have_count(3)
    expect(control.locator("[data-textsize='default']")).to_have_attribute(
        "aria-pressed", "true"
    )
    _assert_one_segmented_track(control)

    _assert_small_and_default_steps(page)

    control.locator("[data-textsize='large']").click()
    expect(page.locator("html")).to_have_attribute("data-textsize", "large")
    expect(page.locator("body")).to_have_css("font-size", "18px")
    expect(control.locator("[data-textsize='large']")).to_have_attribute(
        "aria-pressed", "true"
    )

    page.evaluate("sessionStorage.setItem('ts-probe-armed', '1')")
    page.reload(wait_until="domcontentloaded")
    first = page.evaluate("window.__firstStamp || null")
    assert first == {"size": "large", "sheets": 0}, (
        f"first data-textsize stamp was {first!r}; the boot script must stamp "
        "the stored step before any stylesheet is parsed"
    )
    expect(page.locator("body")).to_have_css("font-size", "18px")

    # Last: resizes and reloads the page.
    _assert_large_at_320px_never_scrolls_or_clips(page)


# Rendered lightness of the selected segment and of the track as the eye sees
# it: each fill is painted on a 1px canvas (so `color-mix()` and translucent
# tints resolve to real sRGB), the track over the card it sits on, the
# selection over that track.
_SEGMENT_LIGHTNESS = """
(nav) => {
  const ctx = document.createElement('canvas').getContext('2d', { willReadFrequently: true });
  const lum = (...fills) => {
    ctx.clearRect(0, 0, 1, 1);
    for (const f of fills) { ctx.fillStyle = f; ctx.fillRect(0, 0, 1, 1); }
    const [r, g, b] = ctx.getImageData(0, 0, 1, 1).data;
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  };
  const bg = (el) => getComputedStyle(el).backgroundColor;
  const card = bg(nav.closest('.card'));
  const track = lum(card, bg(nav));
  return {
    theme: document.documentElement.dataset.theme,
    track,
    selected: lum(card, bg(nav), bg(nav.querySelector('.segmented-item[aria-pressed=true]'))),
    card: lum(card),
  };
}
"""


@pytest.mark.parametrize("scheme", ["dark", "light"])
def test_selected_segment_is_raised_above_the_track(
    authed_page: Page, base_url: str, scheme: str
) -> None:
    """#1575: the selected text-size segment reads raised, not pressed in.

    With no stored theme the boot follows ``prefers-color-scheme`` (the
    "system" path) and still stamps ``data-theme``, so the dark rule applies.
    In dark the selection must be lighter than the track it sits in; in light
    it stays the card surface, which is lighter too.
    """
    page = authed_page
    page.emulate_media(color_scheme=scheme)
    page.add_init_script("try { localStorage.removeItem('app-launcher.theme'); } catch (e) {}")
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    _open_settings(page)

    expect(page.locator("html")).to_have_attribute("data-theme", scheme)
    control = page.locator("#textSizeControl")
    expect(control.locator("[data-textsize='default']")).to_have_attribute(
        "aria-pressed", "true"
    )
    got = stable_eval(control, _SEGMENT_LIGHTNESS)
    assert got["selected"] > got["track"], got
    if scheme == "light":
        assert got["selected"] == got["card"], got


def test_channels_setup_hint_wraps_at_large_320px(
    authed_page: Page, base_url: str
) -> None:
    """A missing profiles file shows its fix-it hint, whose file path is one
    unbreakable word: it must wrap inside its column, not push the card
    sideways. The channels payload is stubbed so the hint shows on any host."""
    page = authed_page
    page.add_init_script("localStorage.setItem('app-launcher.textsize', 'large')")
    page.route(
        "**/api/life-os/channels",
        lambda route: route.fulfill(
            content_type="application/json",
            body=json.dumps({
                "profiles": [], "problems": [],
                "setup": {"file_present": False, "life_os_found": True,
                          "bun": True, "plugin": True, "skills": []},
            }),
        ) if route.request.method == "GET" else route.fallback(),
    )
    page.set_viewport_size({"width": 320, "height": 700})
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    open_settings_sheet(page, "channelsSheet")
    hint = page.locator("#channelChecks .channel-check-meta .muted").first
    expect(hint).to_contain_text("channel_profiles.sample.json")
    got = stable_eval(hint, "el => ({sw: el.scrollWidth, cw: el.clientWidth})")
    assert got["sw"] <= got["cw"], got
