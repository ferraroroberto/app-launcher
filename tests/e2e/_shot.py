"""Deterministic screenshot capture for the screenshot baseline.

`scripts/shot_baseline.py` compares the screenshots an e2e run wrote with the
committed baseline, so two runs of the same commit must write the same pixels.
Three things move between runs if nothing pins them: an animation still in
flight (a nav's delayed reveal is the usual one), a pane whose rows have not
been fetched or painted, and anything the page reads from the clock, the time
zone or the locale. This module pins all three and captures every screen at
phone and desktop size, in light and dark, by default:

    from tests.e2e._shot import capture_screen

    def test_home_shots(browser, app_url):
        capture_screen(browser, f"{app_url}/", "home", SHOTS_DIR)

That writes `home-phone-light.png`, `home-phone-dark.png`,
`home-desktop-light.png` and `home-desktop-dark.png`. A screen that needs
driving first (a sheet open, a row expanded) takes a `prepare(page, variant)`
callback, or opens its own pinned page with `variant_page()` and calls `shot()`:

    for variant in variants():
        with variant_page(browser, variant) as page:
            page.goto(url)
            page.get_by_role("button", name="Sort").click()
            shot(page, SHOTS_DIR / f"sort-sheet-{variant.slug}.png")

The pins, all applied by `variant_page()`:

  * the page's `Date` is frozen at `PINNED_NOW` (timers keep running, so CSS
    animations and app code still behave), the time zone is `PINNED_TIMEZONE`
    and the locale `PINNED_LOCALE`;
  * the colour scheme is emulated per variant; an app whose theme is an
    explicit toggle also sets it in `prepare` (a `data-theme` attribute, a
    stored setting);
  * `shot()` waits for the network, fonts, images and every finite animation to
    finish, then for three frames in which neither the DOM size nor any scroll
    offset changes, and captures with animations disabled and the caret hidden.

`shot()` refuses a page whose clock is not pinned (a popup, a context opened
some other way) and a page whose rendered text contains this machine's home
folder: the baseline is committed, often to a public repo.

Chromium rasterises a changed region of a tile over the pixels already there
unless partial raster is off; add `CHROMIUM_CAPTURE_ARGS` to the suite's
`browser_type_launch_args` so one DOM always paints one set of pixels.

Vendor-verbatim: copied byte-for-byte into adopters (manifest key
`shot_capture`); stdlib and Playwright only. Contract:
docs/playwright-ui-testing.md ("Screenshot baseline").
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, UTC
from pathlib import Path

from playwright.sync_api import Browser, Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

#: The suite's one instant: every run, on every day, renders this date.
PINNED_NOW = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)
PINNED_TIMEZONE = "UTC"
PINNED_LOCALE = "en-US"
#: Rasterise each frame whole: a partial re-raster over old pixels shades edges differently.
CHROMIUM_CAPTURE_ARGS = ("--disable-partial-raster",)
#: How long `shot()` waits for the network to go idle before capturing anyway; a
#: page that deliberately shows a request in flight must still be capturable.
SETTLE_NETWORK_MS = 5000
#: A page whose clock reads further than this from `PINNED_NOW` is not pinned.
CLOCK_TOLERANCE_MS = 1000

_NAME = re.compile(r"^[a-z0-9]+(?:[.-][a-z0-9]+)*$")


@dataclass(frozen=True)
class Viewport:
    name: str
    width: int
    height: int
    scale: float
    mobile: bool


#: An iPhone-class phone, at its real device scale so glyph edges match the device.
PHONE = Viewport("phone", 390, 844, 3, True)
DESKTOP = Viewport("desktop", 1280, 900, 1, False)
VIEWPORTS = (PHONE, DESKTOP)
THEMES = ("light", "dark")


@dataclass(frozen=True)
class Variant:
    viewport: Viewport
    theme: str

    @property
    def slug(self) -> str:
        return f"{self.viewport.name}-{self.theme}"


def variants(viewports: Sequence[Viewport] = VIEWPORTS, themes: Sequence[str] = THEMES) -> list[Variant]:
    """Every viewport in every theme: phone and desktop, light and dark by default."""
    return [Variant(v, t) for v in viewports for t in themes]


_SETTLE_JS = """
async () => {
  const raf = () => new Promise(r => requestAnimationFrame(() => r()));
  try { if (document.fonts) await document.fonts.ready; } catch (e) { /* no fonts API */ }
  const pending = Array.from(document.images).filter(i => !i.complete);
  await Promise.all(pending.map(i => new Promise(r => {
    i.addEventListener('load', r, { once: true });
    i.addEventListener('error', r, { once: true });
  })));
  // Let every finite animation end rather than leaning on Playwright's
  // `animations="disabled"` to seek it there: a still-running animation keeps
  // its element on a composited layer, which rasterises a shade differently.
  // Infinite ones (a spinner) never end and are left to Playwright.
  const running = document.getAnimations().filter(a => {
    const t = a.effect && a.effect.getComputedTiming ? a.effect.getComputedTiming() : null;
    return t && t.iterations !== Infinity && t.endTime !== Infinity;
  });
  if (running.length) {
    await Promise.race([
      Promise.all(running.map(a => a.finished.catch(() => {}))),
      new Promise(r => setTimeout(r, 4000)),
    ]);
  }
  // One string standing for what the next frame would paint: how much DOM
  // there is, how tall it is, and where every scroller sits.
  const sample = () => {
    const parts = [window.scrollX, window.scrollY,
                   document.documentElement.scrollHeight,
                   document.getElementsByTagName('*').length];
    for (const el of document.querySelectorAll('*')) {
      if (el.scrollTop || el.scrollLeft) parts.push(el.scrollTop, el.scrollLeft);
    }
    return parts.join('|');
  };
  let previous = null, stable = 0;
  for (let i = 0; i < 120 && stable < 3; i++) {
    await raf();
    const current = sample();
    stable = current === previous ? stable + 1 : 0;
    previous = current;
  }
  await raf();
  await raf();
}
"""


def settle(page: Page) -> None:
    """Block until the page has stopped moving: network, fonts, images, animations, layout."""
    try:
        page.wait_for_load_state("networkidle", timeout=SETTLE_NETWORK_MS)
    except PlaywrightTimeoutError:
        pass
    page.evaluate(_SETTLE_JS)


def _assert_pinned_clock(page: Page) -> None:
    now_ms = page.evaluate("() => Date.now()")
    pinned_ms = int(PINNED_NOW.timestamp() * 1000)
    if abs(now_ms - pinned_ms) > CLOCK_TOLERANCE_MS:
        raise AssertionError(
            f"this page's clock is not pinned: it reads {now_ms} ms, the capture runs at {pinned_ms} ms "
            f"({PINNED_NOW.isoformat()}). Open it with variant_page(), or the baseline dates itself to "
            "whichever day the suite ran."
        )


def _assert_no_host_identity(page: Page) -> None:
    home = str(Path.home())
    text = page.evaluate("() => document.documentElement.innerText || ''")
    for spelling in {home, home.replace("\\", "/"), home.replace("/", "\\")}:
        if spelling and spelling in text:
            raise AssertionError(
                "this page shows the home folder of the machine running the suite; a committed "
                "screenshot of it would publish the account name. Render a fixture path instead."
            )


def shot(page: Page, path: Path, *, full_page: bool = False) -> None:
    """Save one screenshot of *page* to *path*, deterministically."""
    settle(page)
    _assert_pinned_clock(page)
    _assert_no_host_identity(page)
    path.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(path), full_page=full_page, animations="disabled", caret="hide")


@contextmanager
def variant_page(browser: Browser, variant: Variant) -> Iterator[Page]:
    """A fresh page in its own context, sized, themed and pinned for *variant*; closed afterwards."""
    vp = variant.viewport
    context = browser.new_context(
        viewport={"width": vp.width, "height": vp.height},
        device_scale_factor=vp.scale,
        is_mobile=vp.mobile,
        has_touch=vp.mobile,
        color_scheme="dark" if variant.theme == "dark" else "light",
        timezone_id=PINNED_TIMEZONE,
        locale=PINNED_LOCALE,
    )
    try:
        context.clock.set_fixed_time(PINNED_NOW)
        yield context.new_page()
    finally:
        context.close()


def capture_screen(
    browser: Browser,
    url: str,
    name: str,
    shots_dir: Path,
    *,
    prepare: Callable[[Page, Variant], None] | None = None,
    only: Sequence[Variant] | None = None,
    full_page: bool = False,
) -> list[Path]:
    """Capture *url* in every variant as `<name>-<viewport>-<theme>.png`; return the paths."""
    if not _NAME.match(name):
        raise ValueError(f"shot name {name!r} must be lowercase words joined by '.' or '-'")
    written = []
    for variant in only if only is not None else variants():
        with variant_page(browser, variant) as page:
            page.goto(url)
            if prepare is not None:
                prepare(page, variant)
            path = shots_dir / f"{name}-{variant.slug}.png"
            shot(page, path, full_page=full_page)
            written.append(path)
    return written
