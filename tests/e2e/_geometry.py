"""Rendered-geometry design-conformance helper (issue #157).

The fleet design canon (`~/.claude/design.md`, codified in fleet-config#342)
carries contracts that are rendered-DOM facts no static CSS/HTML scan can
prove: effective >=44x44px touch targets (visual box + `::before`/`::after`
negative-inset hit-area expansion), pairwise non-overlap of expanded targets,
horizontal page overflow, and the live Chart.js tick/cue configuration.
home-automation#409 verified them with inline `page.evaluate` snippets; this
module is those snippets promoted to one canonical, app-agnostic helper.

Vendor-verbatim: consuming apps copy this file byte-identical into their own
`tests/e2e/` (like `app/tray/single_instance.py`) — every selector, budget,
and theme mechanism is a call-site argument, so the copy never forks. The
module imports only the stdlib and `playwright.sync_api`.

Deliberate altitude boundary: the chart checks read the live Chart.js config
(`Chart.getChart(canvas)` — the design contract the app authored), never
pixel-measure rendered label collision; config reads are deterministic where
pixel measurement flakes.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Literal, cast

from playwright.sync_api import Locator, Page

# ---------------------------------------------------------------------------
# Rectangles

# Visual box + per-side hit-area expansion. A pseudo-element counts as a
# hit-area only when it is a real box (`content` set) positioned over the
# element (`position: absolute`); each negative computed inset side grows the
# effective rect outward by its magnitude (the `.hit-target` pattern — e.g.
# a 34px visual control with `::before { inset: -5px }` hits 44px effective).
_EFFECTIVE_RECT_JS = """el => {
  const rect = el.getBoundingClientRect();
  const expansion = { left: 0, right: 0, top: 0, bottom: 0 };
  for (const which of ['::before', '::after']) {
    const pseudo = getComputedStyle(el, which);
    if (pseudo.content === 'none' || pseudo.position !== 'absolute') continue;
    for (const side of ['left', 'right', 'top', 'bottom']) {
      const parsed = parseFloat(pseudo[side]);
      const grow = Number.isFinite(parsed) && parsed < 0 ? -parsed : 0;
      expansion[side] = Math.max(expansion[side], grow);
    }
  }
  return {
    left: rect.left, top: rect.top, right: rect.right, bottom: rect.bottom,
    expandLeft: expansion.left, expandRight: expansion.right,
    expandTop: expansion.top, expandBottom: expansion.bottom,
  };
}"""


@dataclass(frozen=True)
class Rect:
    """An axis-aligned rectangle in CSS pixels (viewport coordinates)."""

    left: float
    top: float
    right: float
    bottom: float

    @property
    def width(self) -> float:
        return self.right - self.left

    @property
    def height(self) -> float:
        return self.bottom - self.top


@dataclass(frozen=True)
class EffectiveRect:
    """A target's visual box and its hit-area-expanded effective box."""

    visual: Rect
    effective: Rect


def _parse_effective(raw: dict[str, float]) -> EffectiveRect:
    visual = Rect(raw["left"], raw["top"], raw["right"], raw["bottom"])
    effective = Rect(
        raw["left"] - raw["expandLeft"],
        raw["top"] - raw["expandTop"],
        raw["right"] + raw["expandRight"],
        raw["bottom"] + raw["expandBottom"],
    )
    return EffectiveRect(visual=visual, effective=effective)


def effective_rect(locator: Locator) -> EffectiveRect:
    """Measure the single element matched by *locator*."""
    raw: dict[str, float] = locator.evaluate(_EFFECTIVE_RECT_JS)
    return _parse_effective(raw)


def effective_rects(locator: Locator) -> list[EffectiveRect]:
    """Measure every element matched by *locator*; zero matches fail loud.

    A selector typo silently matching nothing must never read as conformance
    (the harness "boot-or-adopt, fail loud" rule).
    """
    raws: list[dict[str, float]] = locator.evaluate_all(
        f"els => els.map({_EFFECTIVE_RECT_JS})"
    )
    if not raws:
        raise AssertionError(
            f"no elements match {locator} — zero matches must fail loud, "
            "not pass as conformant"
        )
    return [_parse_effective(raw) for raw in raws]


# ---------------------------------------------------------------------------
# Touch-target assertions

def assert_min_target(locator: Locator, min_px: float = 44.0) -> None:
    """Every match's *effective* width AND height meet the spec floor.

    `min_px` is the spec's `components.hit-target.min` (44px per design.md).
    """
    for index, target in enumerate(effective_rects(locator)):
        eff, vis = target.effective, target.visual
        if eff.width < min_px or eff.height < min_px:
            raise AssertionError(
                f"target #{index} of {locator}: effective "
                f"{eff.width:g}x{eff.height:g}px is below the {min_px:g}px "
                f"floor (visual {vis.width:g}x{vis.height:g}px)"
            )


def assert_no_overlap(targets: Locator | Iterable[Locator]) -> None:
    """No two *effective* rectangles overlap (O(n^2) pairwise sweep).

    Adjacent compact controls may each reach 44px via invisible expansion,
    but their expanded rectangles must never intersect — a tap in the shared
    zone would be ambiguous. Pass one locator (all its matches) or an
    iterable of locators.
    """
    if isinstance(targets, Locator):
        rects = [target.effective for target in effective_rects(targets)]
    else:
        rects = [effective_rect(target).effective for target in targets]
    for i, first in enumerate(rects):
        for j in range(i + 1, len(rects)):
            second = rects[j]
            separated = (
                first.right <= second.left
                or second.right <= first.left
                or first.bottom <= second.top
                or second.bottom <= first.top
            )
            if not separated:
                raise AssertionError(
                    f"effective rects overlap: #{i} {first} and #{j} {second}"
                )


# ---------------------------------------------------------------------------
# Page overflow

def assert_no_horizontal_overflow(page: Page) -> None:
    """Neither the document nor the body scrolls horizontally.

    `scrollWidth <= clientWidth` on `documentElement` (and body vs the same
    viewport width) — the measured form of "the layout is fluid at this
    width"; a full-page screenshot cannot catch this, only the numbers do.
    """
    metrics: dict[str, float] = page.evaluate(
        """() => ({
          doc: document.documentElement.scrollWidth,
          body: document.body.scrollWidth,
          client: document.documentElement.clientWidth,
        })"""
    )
    if metrics["doc"] > metrics["client"] or metrics["body"] > metrics["client"]:
        raise AssertionError(
            "horizontal overflow: documentElement.scrollWidth="
            f"{metrics['doc']:g} body.scrollWidth={metrics['body']:g} "
            f"exceed clientWidth={metrics['client']:g}"
        )


# ---------------------------------------------------------------------------
# Chart.js live-config reads (config altitude, never pixels)

_CHART_LOOKUP_JS_PREFIX = """selector => {
  if (typeof Chart === 'undefined') return { error: 'Chart.js is not loaded' };
  const canvas = document.querySelector(selector);
  if (!canvas) return { error: 'no canvas matches ' + selector };
  const chart = Chart.getChart(canvas);
  if (!chart) return { error: 'no Chart.js instance registered for ' + selector };
"""

_CHART_TICKS_JS = _CHART_LOOKUP_JS_PREFIX + """
  const ticks = (chart.options.scales && chart.options.scales.x
                 && chart.options.scales.x.ticks) || {};
  return {
    maxTicksLimit: ticks.maxTicksLimit ?? null,
    autoSkip: ticks.autoSkip ?? null,
    maxRotation: ticks.maxRotation ?? null,
  };
}"""

_CHART_CUES_JS = _CHART_LOOKUP_JS_PREFIX + """
  return chart.data.datasets.map(dataset => ({
    label: dataset.label ?? null,
    borderDash: dataset.borderDash ?? null,
    pointStyle: dataset.pointStyle ?? null,
    fill: dataset.fill ?? null,
  }));
}"""


@dataclass(frozen=True)
class ChartTicks:
    """The x-scale tick knobs the responsive-chart contract keys on."""

    max_ticks_limit: int | None
    auto_skip: bool | None
    max_rotation: float | None


@dataclass(frozen=True)
class DatasetCue:
    """One dataset's non-colour visual cues (dash pattern, point marker, fill)."""

    label: str | None
    border_dash: list[float] | None
    point_style: str | None
    fill: Any


def _chart_eval(page: Page, canvas_selector: str, js: str) -> Any:
    result = page.evaluate(js, canvas_selector)
    if isinstance(result, dict) and "error" in result:
        raise AssertionError(str(result["error"]))
    return result


def chart_tick_budget(page: Page, canvas_selector: str) -> ChartTicks:
    """Read the live x-scale tick config from the registered Chart.js chart."""
    raw = _chart_eval(page, canvas_selector, _CHART_TICKS_JS)
    return ChartTicks(
        max_ticks_limit=raw["maxTicksLimit"],
        auto_skip=raw["autoSkip"],
        max_rotation=raw["maxRotation"],
    )


def assert_chart_ticks(
    page: Page,
    canvas_selector: str,
    max_ticks: int,
    *,
    require_auto_skip: bool = True,
    max_rotation: float = 0.0,
) -> None:
    """The chart authors a tick budget within *max_ticks*, skips, and stays flat.

    `max_ticks` is the viewport's budget from the spec (~4 phone / 8 desktop).
    An absent `maxTicksLimit` fails: the budget must be authored, not left to
    label density.
    """
    ticks = chart_tick_budget(page, canvas_selector)
    if ticks.max_ticks_limit is None or ticks.max_ticks_limit > max_ticks:
        raise AssertionError(
            f"chart {canvas_selector}: maxTicksLimit={ticks.max_ticks_limit} "
            f"exceeds (or misses) the authored tick budget of {max_ticks}"
        )
    if require_auto_skip and ticks.auto_skip is not True:
        raise AssertionError(
            f"chart {canvas_selector}: autoSkip={ticks.auto_skip} — the tick "
            "budget needs autoSkip to actually drop labels"
        )
    if ticks.max_rotation is None or ticks.max_rotation > max_rotation:
        raise AssertionError(
            f"chart {canvas_selector}: maxRotation={ticks.max_rotation} "
            f"exceeds {max_rotation:g} — rotated labels collide on phones"
        )


def chart_dataset_cues(page: Page, canvas_selector: str) -> list[DatasetCue]:
    """Read every dataset's non-colour cues; the app asserts its own policy."""
    raw = _chart_eval(page, canvas_selector, _CHART_CUES_JS)
    return [
        DatasetCue(
            label=entry["label"],
            border_dash=entry["borderDash"],
            point_style=entry["pointStyle"],
            fill=entry["fill"],
        )
        for entry in raw
    ]


# ---------------------------------------------------------------------------
# Viewport x theme matrix

VIEWPORT_WIDTHS: tuple[int, ...] = (320, 390, 430, 772)
THEMES: tuple[str, ...] = ("light", "dark")
MATRIX: tuple[tuple[int, str], ...] = tuple(
    (width, theme) for width in VIEWPORT_WIDTHS for theme in THEMES
)

ThemeSetter = Callable[[Page, str], None]


def matrix_id(leg: tuple[int, str]) -> str:
    """Readable pytest parameter id for a matrix leg: ``390px-dark``."""
    width, theme = leg
    return f"{width}px-{theme}"


def apply_matrix_leg(
    page: Page,
    width: int,
    theme: str,
    *,
    set_theme: ThemeSetter | None = None,
    height: int = 844,
) -> None:
    """Put *page* on one matrix leg: viewport width + colour theme.

    Call after navigation — the default theme application stamps
    `document.documentElement.dataset.theme`, which a later `goto` would
    discard. It also emulates `prefers-color-scheme` so apps whose theme
    boot falls back to the media query resolve the same way. An app whose
    boot script reads its own persisted state (e.g. a localStorage key)
    passes `set_theme` to drive that mechanism instead.
    """
    if theme not in ("light", "dark"):
        raise ValueError(f"theme must be 'light' or 'dark', got {theme!r}")
    page.set_viewport_size({"width": width, "height": height})
    page.emulate_media(color_scheme=cast(Literal["light", "dark"], theme))
    if set_theme is not None:
        set_theme(page, theme)
    else:
        page.evaluate(
            "theme => { document.documentElement.dataset.theme = theme; }", theme
        )
