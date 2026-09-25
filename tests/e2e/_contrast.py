"""Rendered text contrast, measured in the page (WCAG 2.2 SC 1.4.3).

The ratio of an element's text colour against the background it actually
sits on: its own background composited over every translucent ancestor's
(the app's tints are `color-mix(... transparent)`), down to the first opaque
one. Used by the design review's COLOR-02 pins (#1238).
"""

from playwright.sync_api import Locator

_CONTRAST_JS = r"""
el => {
  const parse = (s) => {
    const n = (s.match(/-?[\d.]+/g) || []).map(Number);
    const scale = s.startsWith('color(') ? 255 : 1;
    return { r: n[0] * scale, g: n[1] * scale, b: n[2] * scale, a: n.length > 3 ? n[3] : 1 };
  };
  const layers = [];
  for (let node = el; node; node = node.parentElement) {
    const c = parse(getComputedStyle(node).backgroundColor);
    if (c.a > 0) { layers.push(c); if (c.a >= 1) break; }
  }
  let bg = { r: 255, g: 255, b: 255 };
  for (const c of layers.reverse()) {
    bg = { r: c.r * c.a + bg.r * (1 - c.a), g: c.g * c.a + bg.g * (1 - c.a),
           b: c.b * c.a + bg.b * (1 - c.a) };
  }
  const lum = ({ r, g, b }) => {
    const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
  };
  const a = lum(parse(getComputedStyle(el).color)), b = lum(bg);
  return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
}
"""


def contrast_ratio(locator: Locator) -> float:
    """The first match's text-to-background contrast ratio, as rendered."""
    return float(locator.first.evaluate(_CONTRAST_JS))
