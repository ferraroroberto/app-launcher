"""Capture the states a UI change creates and lint their rendered layout.

A behaviour test proves a sheet opens; it does not prove the sheet fits the
phone. This module closes that gap with numbers, not eyeballs: each *state* (a
named setup function the e2e test already has: navigate, open the sheet, stub
the data) is rendered at phone and desktop size, in light and dark, screenshotted
to a local proof folder, and measured in the page.

    from tests.e2e._layout_lint import capture_states

    def test_reason_sheet_layout(browser, app_url):
        def reason_sheet(page):
            page.goto(f"{app_url}/jobs")
            page.get_by_role("button", name="Why?").click()

        capture_states(browser, {"jobs.reason-sheet": reason_sheet})

`capture_states` fails the test on any error finding and returns the manifest
(every state x viewport x theme, its screenshot path, its findings, its cost).
`lint_layout(page)` / `assert_layout_clean(page)` run the same lint on a page a
test already drives, without capturing.

What the lint reports (`Finding.kind`):

  * `page-overflow`: the document is wider than the viewport, scrollable or
    hidden behind `overflow-x: hidden` on the root.
  * `overflow`: an element's content spills past its own box (scrollWidth >
    clientWidth + 1) and the element does not scroll by design
    (`overflow-x: auto | scroll`). Only the innermost spilling element is
    named: its ancestors spill because it does.
  * `clipped`: an ellipsis, a hidden overflow or a line clamp cuts text and
    nothing offers the full text (`title` on the element or an ancestor, or an
    `aria-label`).
  * `overlap`: two interactive elements' boxes intersect by more than 1px.
    Nested controls, and a control under a fixed or sticky layer it scrolls
    beneath (the floating nav pill), are not overlaps.
  * `offscreen`: an interactive element sits partly outside the viewport
    horizontally (outside a horizontal scroller), or, in a fixed layer, outside
    it vertically.
  * `tap-target`: on a phone viewport, an interactive element's effective box
    (visual box plus `::before`/`::after` hit-area expansion, the `geometry`
    component's rule) is under 44x44px. Inline links inside running text are
    exempt (WCAG 2.5.8).
  * `contrast`: text below WCAG AA against its composited background (4.5:1,
    3:1 for large text). A **warning**: reported in the manifest, never fails.
    Text over a background image is not measured.

Every kind except `contrast` is an **error** and fails `capture_states`, unless
the element matches an entry of the per-call `allow` map, `{css selector:
reason}`; an allowed finding stays in the manifest with its reason. An empty
reason or a selector the page cannot parse raises `ValueError`.

Screenshots never land in a git work tree: the proof folder is `UI_PROOF_DIR`
if set, else `<temp>/ui-proof`, and a folder inside any checkout raises
`ValueError`. The manifest (`manifest.json` there) is what a PR may quote:
`Finding.line()` is the public-safe form (kind, element, measurement; never the
element's text). Screenshots stay local.

Vendor-verbatim (manifest key `layout_lint`), stdlib and Playwright only, and it
needs its two siblings beside it in `tests/e2e/`: `_geometry.py` (`geometry`)
for the effective-rect rule and `_shot.py` (`shot_capture`) for the pinned,
settled capture. Contract: docs/playwright-ui-testing.md ("Layout lint").
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from playwright.sync_api import Browser, Page

from tests.e2e._geometry import EFFECTIVE_RECT_JS
from tests.e2e._shot import THEMES, VIEWPORTS, Viewport, shot, variant_page, variants

#: The touch-target floor, design.md's `components.hit-target.min`.
MIN_TARGET_PX = 44.0
#: A viewport at most this wide is a phone: tap-target sizes are checked there.
PHONE_MAX_WIDTH = 480
PROOF_DIR_ENV = "UI_PROOF_DIR"
#: Kinds that fail a capture; everything else is reported only.
ERROR_KINDS = frozenset({"page-overflow", "overflow", "clipped", "overlap", "offscreen", "tap-target"})

State = Callable[[Page], None]

_STATE_NAME = re.compile(r"^[a-z0-9]+(?:[.-][a-z0-9]+)*$")

_LINT_BODY_JS = r"""
  const findings = [];
  const badSelectors = [];
  for (const [sel] of opts.allow) {
    try { document.querySelector(sel); } catch (e) { badSelectors.push(sel); }
  }
  if (badSelectors.length) return { findings, badSelectors };
  const allowedFor = el => {
    for (const [sel, reason] of opts.allow) if (el.matches(sel)) return reason;
    return null;
  };
  const describe = el => {
    const parts = [];
    for (let node = el; node && node.nodeType === 1 && parts.length < 3; node = node.parentElement) {
      let part = node.tagName.toLowerCase();
      if (node.id) { parts.unshift(part + '#' + node.id); break; }
      const classes = Array.from(node.classList).slice(0, 2);
      if (classes.length) part += '.' + classes.join('.');
      parts.unshift(part);
      if (node === document.body) break;
    }
    return parts.join(' > ');
  };
  const snippet = el => (el.innerText || el.value || '').trim().replace(/\s+/g, ' ').slice(0, 40);
  const add = (kind, el, detail, other = null) => findings.push({
    kind, element: describe(el), other: other ? describe(other) : null, text: snippet(el), detail,
    allowed: allowedFor(el) || (other ? allowedFor(other) : null),
  });
  const isVisible = el => {
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility !== 'visible' || parseFloat(cs.opacity) === 0) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const vw = document.documentElement.clientWidth;
  const vh = window.innerHeight;

  const doc = document.documentElement;
  const pageWidth = Math.max(doc.scrollWidth, document.body.scrollWidth);
  if (pageWidth > vw + 1) add('page-overflow', doc, `document ${pageWidth}px wide in a ${vw}px viewport`);

  const REPLACED = new Set(['INPUT', 'TEXTAREA', 'SELECT', 'IMG', 'SVG', 'CANVAS', 'VIDEO', 'IFRAME', 'OBJECT']);
  const scrollsX = cs => cs.overflowX === 'auto' || cs.overflowX === 'scroll';
  const readable = el => Boolean(el.closest('[title]:not([title=""])') || el.getAttribute('aria-label'));
  // scrollWidth also counts an absolutely positioned ::before/::after hit-area
  // expansion, which is invisible by design; the content range does not.
  const range = document.createRange();
  const contentSpills = el => {
    range.selectNodeContents(el);
    const content = range.getBoundingClientRect();
    const box = el.getBoundingClientRect();
    return content.width > 0 && (content.left < box.left - 1 || content.right > box.right + 1);
  };
  const boxed = [];
  for (const el of document.body.querySelectorAll('*')) {
    if (REPLACED.has(el.tagName.toUpperCase()) || el.closest('svg') || !isVisible(el)) continue;
    const cs = getComputedStyle(el);
    const overX = el.scrollWidth > el.clientWidth + 1 && !scrollsX(cs);
    const clamped = cs.webkitLineClamp && cs.webkitLineClamp !== 'none' && el.scrollHeight > el.clientHeight + 1;
    if (!overX && !clamped) continue;
    // A 1px box is the visually-hidden (screen-reader-only) pattern, not a clip.
    const srOnly = el.clientWidth <= 1 || el.clientHeight <= 1;
    const hasText = (el.innerText || '').trim().length > 0;
    if (overX && cs.overflowX === 'visible') {
      if (contentSpills(el)) boxed.push(['overflow', el, `content ${el.scrollWidth}px in a ${el.clientWidth}px box`]);
    } else if (hasText && !srOnly && !readable(el)) {
      const how = clamped ? 'line clamp' : (cs.textOverflow === 'ellipsis' ? 'ellipsis' : 'hidden overflow');
      const cut = clamped ? `${el.scrollHeight}px of text to ${el.clientHeight}px tall`
        : `${el.scrollWidth}px of text to ${el.clientWidth}px`;
      boxed.push(['clipped', el, `${how} cuts ${cut}`]);
    }
  }
  for (const [kind, el, detail] of boxed) {
    if (!boxed.some(([, other]) => other !== el && el.contains(other))) add(kind, el, detail);
  }

  const CONTROL = 'a[href], button, input:not([type="hidden"]), select, textarea, summary, '
    + '[role="button"], [role="link"], [role="tab"], [role="switch"], [role="checkbox"], [role="menuitem"]';
  const INTERACTIVE = CONTROL + ', [tabindex]:not([tabindex="-1"])';
  // A focusable region that wraps controls (a scroll pane, Streamlit's main
  // section) is a landmark, not a target: its box overlaps everything it holds.
  const isTarget = el => el.matches(CONTROL) || !el.querySelector(CONTROL);
  const layerOf = el => {
    for (let node = el; node && node !== document.body; node = node.parentElement) {
      const pos = getComputedStyle(node).position;
      if (pos === 'fixed' || pos === 'sticky') return node;
    }
    return null;
  };
  const inScrollerX = el => {
    for (let node = el.parentElement; node && node !== document.body; node = node.parentElement) {
      if (scrollsX(getComputedStyle(node)) && node.scrollWidth > node.clientWidth + 1) return true;
    }
    return false;
  };
  const controls = Array.from(document.body.querySelectorAll(INTERACTIVE)).filter(el => isTarget(el) && isVisible(el))
    .map(el => ({ el, rect: el.getBoundingClientRect(), layer: layerOf(el) }));
  for (let i = 0; i < controls.length; i++) {
    const a = controls[i];
    for (let j = i + 1; j < controls.length; j++) {
      const b = controls[j];
      if (a.layer !== b.layer || a.el.contains(b.el) || b.el.contains(a.el)) continue;
      const w = Math.min(a.rect.right, b.rect.right) - Math.max(a.rect.left, b.rect.left);
      const h = Math.min(a.rect.bottom, b.rect.bottom) - Math.max(a.rect.top, b.rect.top);
      if (w > 1 && h > 1) add('overlap', a.el, `boxes intersect by ${Math.round(w)}x${Math.round(h)}px`, b.el);
    }
  }
  for (const { el, rect, layer } of controls) {
    if (rect.width <= 1 && rect.height <= 1) continue;
    const outX = rect.left < -1 || rect.right > vw + 1;
    const outY = layer && (rect.top < -1 || rect.bottom > vh + 1);
    if ((outX && !inScrollerX(el)) || outY) {
      add('offscreen', el, `box ${Math.round(rect.left)},${Math.round(rect.top)} to `
        + `${Math.round(rect.right)},${Math.round(rect.bottom)} in a ${vw}x${vh}px viewport`);
    }
  }
  if (opts.phone) {
    for (const { el } of controls) {
      if (getComputedStyle(el).display === 'inline' && el.closest('p, li, td, dd, blockquote')) continue;
      const target = el.matches('input') && el.closest('label') ? el.closest('label') : el;
      const r = effectiveRect(target);
      const w = r.right + r.expandRight - r.left + r.expandLeft;
      const h = r.bottom + r.expandBottom - r.top + r.expandTop;
      if (w < opts.minTarget || h < opts.minTarget) {
        add('tap-target', el, `effective ${Math.round(w)}x${Math.round(h)}px, under ${opts.minTarget}px`);
      }
    }
  }

  const canvas = document.createElement('canvas');
  canvas.width = canvas.height = 1;
  const ctx = canvas.getContext('2d', { willReadFrequently: true });
  const rgba = color => {
    ctx.clearRect(0, 0, 1, 1);
    ctx.fillStyle = color;
    ctx.fillRect(0, 0, 1, 1);
    const [r, g, b, a] = ctx.getImageData(0, 0, 1, 1).data;
    return [r, g, b, a / 255];
  };
  const over = (top, bottom) => top.slice(0, 3).map((c, k) => c * top[3] + bottom[k] * (1 - top[3])).concat(1);
  const background = el => {
    const layers = [];
    for (let node = el; node; node = node.parentElement) {
      const cs = getComputedStyle(node);
      if (cs.backgroundImage !== 'none') return null;
      const color = rgba(cs.backgroundColor);
      if (color[3] > 0) layers.push(color);
      if (color[3] >= 1) break;
    }
    return layers.reduceRight((acc, layer) => over(layer, acc), [255, 255, 255, 1]);
  };
  const luminance = rgb => {
    const [r, g, b] = rgb.map(c => { c /= 255; return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4; });
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  };
  for (const el of document.body.querySelectorAll('*')) {
    const ownText = Array.from(el.childNodes).some(n => n.nodeType === 3 && n.textContent.trim());
    if (!ownText || !isVisible(el) || el.closest(':disabled, [aria-disabled="true"]')) continue;
    const bg = background(el);
    if (!bg) continue;
    const cs = getComputedStyle(el);
    const fg = over(rgba(cs.color), bg);
    const [hi, lo] = [luminance(fg), luminance(bg)].sort((x, y) => y - x);
    const ratio = (hi + 0.05) / (lo + 0.05);
    const size = parseFloat(cs.fontSize);
    const large = size >= 24 || (size >= 18.66 && parseInt(cs.fontWeight, 10) >= 700);
    const floor = large ? 3 : 4.5;
    if (ratio < floor) add('contrast', el, `contrast ${ratio.toFixed(2)}:1, under ${floor}:1`);
  }
  return { findings, badSelectors };
"""

_LINT_JS = "opts => {\n  const effectiveRect = " + EFFECTIVE_RECT_JS + ";\n" + _LINT_BODY_JS + "}"


@dataclass(frozen=True)
class Finding:
    """One layout defect the lint measured on a rendered page."""

    kind: str
    element: str
    detail: str
    text: str = ""
    other: str | None = None
    allowed: str | None = None

    @property
    def severity(self) -> str:
        return "error" if self.kind in ERROR_KINDS else "warning"

    @property
    def fails(self) -> bool:
        """An error nobody allowed: this one fails the test."""
        return self.severity == "error" and self.allowed is None

    def line(self) -> str:
        """The public-safe one-line form: no element text, so a PR may quote it."""
        pair = f" x {self.other}" if self.other else ""
        tail = f" [allowed: {self.allowed}]" if self.allowed else ""
        return f"{self.kind} {self.element}{pair}: {self.detail}{tail}"


def lint_layout(
    page: Page,
    *,
    allow: Mapping[str, str] | None = None,
    phone: bool | None = None,
    min_target: float = MIN_TARGET_PX,
) -> list[Finding]:
    """Measure the page as it is rendered now; return every finding, allowed ones included.

    `phone` defaults to "the viewport is at most `PHONE_MAX_WIDTH` wide".
    """
    allow = dict(allow or {})
    for selector, reason in allow.items():
        if not reason.strip():
            raise ValueError(f"allow entry {selector!r} needs a reason")
    if phone is None:
        size = page.viewport_size
        phone = size is not None and size["width"] <= PHONE_MAX_WIDTH
    result: dict[str, Any] = page.evaluate(
        _LINT_JS, {"allow": list(allow.items()), "phone": phone, "minTarget": min_target}
    )
    if result["badSelectors"]:
        raise ValueError(f"allow selectors the page cannot parse: {result['badSelectors']}")
    return [Finding(**raw) for raw in result["findings"]]


def assert_layout_clean(page: Page, **kwargs: Any) -> list[Finding]:
    """`lint_layout`, then fail on any error nobody allowed; return all findings."""
    findings = lint_layout(page, **kwargs)
    failing = [f.line() for f in findings if f.fails]
    if failing:
        raise AssertionError("layout lint: " + "; ".join(failing))
    return findings


def proof_dir() -> Path:
    """The local folder screenshots and the manifest go to; never inside a checkout."""
    configured = os.environ.get(PROOF_DIR_ENV, "").strip()
    folder = Path(configured) if configured else Path(tempfile.gettempdir()) / "ui-proof"
    _refuse_checkout(folder)
    return folder


def _refuse_checkout(folder: Path) -> None:
    for parent in (folder.resolve(), *folder.resolve().parents):
        if (parent / ".git").exists():
            raise ValueError(
                f"proof folder {folder} is inside the git work tree at {parent}; screenshots "
                f"never go in a checkout. Point {PROOF_DIR_ENV} outside it."
            )


@dataclass(frozen=True)
class CaptureEntry:
    """One state at one viewport in one theme: its screenshot, findings and cost."""

    state: str
    viewport: str
    theme: str
    screenshot: str
    ms: int
    findings: tuple[Finding, ...]


@dataclass(frozen=True)
class Manifest:
    """Every captured leg of one `capture_states` call, and where it was written."""

    path: Path
    entries: tuple[CaptureEntry, ...]

    @property
    def failing(self) -> list[tuple[CaptureEntry, Finding]]:
        return [(e, f) for e in self.entries for f in e.findings if f.fails]

    def summary(self) -> str:
        """Public-safe text: counts per leg and each failing finding, no screenshots or page text."""
        lines = []
        for e in self.entries:
            errors = sum(f.fails for f in e.findings)
            warnings = sum(f.severity == "warning" for f in e.findings)
            allowed = sum(f.allowed is not None for f in e.findings)
            lines.append(
                f"{e.state} {e.viewport}-{e.theme}: {errors} error(s), {warnings} warning(s), "
                f"{allowed} allowed, {e.ms} ms"
            )
        lines += [f"  {e.state} {e.viewport}-{e.theme}: {f.line()}" for e, f in self.failing]
        return "\n".join(lines)


def _write_manifest(path: Path, entries: Sequence[CaptureEntry]) -> None:
    """Merge *entries* into the manifest at *path*, keyed by state x viewport x theme."""
    existing: dict[str, Any] = {}
    if path.exists():
        try:
            existing = {_key(e): e for e in json.loads(path.read_text(encoding="utf-8"))["entries"]}
        except (OSError, ValueError, KeyError, TypeError):
            existing = {}
    for entry in entries:
        raw = asdict(entry)
        for finding, source in zip(raw["findings"], entry.findings, strict=True):
            finding["severity"] = source.severity
        existing[_key(raw)] = raw
    path.write_text(json.dumps({"entries": list(existing.values())}, indent=2), encoding="utf-8")


def _key(raw: Mapping[str, Any]) -> str:
    return f"{raw['state']}|{raw['viewport']}|{raw['theme']}"


def capture_states(
    browser: Browser,
    states: Mapping[str, State],
    *,
    out_dir: Path | None = None,
    viewports: Sequence[Viewport] = VIEWPORTS,
    themes: Sequence[str] = THEMES,
    allow: Mapping[str, str] | None = None,
    theme_storage_key: str | None = None,
    check: bool = True,
) -> Manifest:
    """Render each state in every viewport x theme, screenshot it, lint it; fail on errors.

    Each state runs on a fresh pinned page (`_shot.variant_page`) and must leave
    it showing the state. `theme_storage_key` is for an app whose theme is an
    explicit toggle: the theme is written to that localStorage key and to
    `data-theme` before any page script runs. With `check=False` the manifest
    is returned without failing, for a test that asserts its own findings.
    """
    bad = [name for name in states if not _STATE_NAME.match(name)]
    if bad:
        raise ValueError(f"state names must be lowercase words joined by '.' or '-': {bad}")
    folder = out_dir if out_dir is not None else proof_dir()
    _refuse_checkout(folder)
    folder.mkdir(parents=True, exist_ok=True)
    entries = []
    for name, setup in states.items():
        for variant in variants(viewports, themes):
            started = time.perf_counter()
            with variant_page(browser, variant) as page:
                if theme_storage_key is not None:
                    page.context.add_init_script(_theme_init_script(theme_storage_key, variant.theme))
                setup(page)
                path = folder / f"{name}-{variant.slug}.png"
                shot(page, path)
                findings = lint_layout(page, allow=allow, phone=variant.viewport.width <= PHONE_MAX_WIDTH)
            entries.append(CaptureEntry(
                name, variant.viewport.name, variant.theme, str(path),
                round((time.perf_counter() - started) * 1000), tuple(findings),
            ))
    manifest = Manifest(folder / "manifest.json", tuple(entries))
    _write_manifest(manifest.path, entries)
    if check and manifest.failing:
        raise AssertionError(f"layout lint failed (manifest {manifest.path}):\n{manifest.summary()}")
    return manifest


def _theme_init_script(key: str, theme: str) -> str:
    return (
        "(([key, theme]) => { try { localStorage.setItem(key, theme); } catch (e) {} "
        "document.documentElement.dataset.theme = theme; })"
        f"({json.dumps([key, theme])});"
    )
