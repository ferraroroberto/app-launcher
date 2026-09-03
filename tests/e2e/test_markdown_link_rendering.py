"""Regression pin: the shared text escaper must cover the attribute position.

``life-os.js``'s ``renderMarkdown`` is escape-first-then-format: it runs the
whole document through ``api.js``'s ``escapeHtml`` once, then ``inlineMd``
applies the small markdown subset — and one of those rules, the link rule,
interpolates a captured value into an ``href="…"`` **attribute** rather than
into element content. Every other ``escapeHtml`` call site in the SPA is a
text position, so the attribute case is the one that is easy to regress: a
quote character that survives the escape pass terminates the ``href`` value
early, and an HTML parser reads whatever follows as further attributes on the
same element instead of as part of the link target.

Pinned as a pure-function probe via dynamic ``import()`` in the page (the same
pattern as ``test_session_title_naming.py``), so it costs no session and no
network beyond the page load.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page

pytestmark = pytest.mark.smoke

# The SPA loads its modules cache-busted (`life-os.js?v=<asset_hash>`). Resolve
# the page's real module URL from the resource timeline so the import shares
# the live instance instead of evaluating a parallel one.
_LIVE_MODULE = """
(name) => {
  const hit = performance.getEntriesByType('resource')
    .map((r) => r.name)
    .find((n) => n.includes('/static/' + name + '?v='));
  return hit || ('/static/' + name);
}
"""

# A link target carrying a double quote. Deliberately whitespace-free: the
# link rule's target group is `[^\\s)]+`, so this is the shape that actually
# reaches the `href="…"` interpolation.
_LINK_TARGET = 'https://example.com/a"data-extra="1'

_PROBE = (
    r"""
async () => {
  const live = (""" + _LIVE_MODULE + r""");
  const { renderMarkdown } = await import(live('life-os.js'));
  const { escapeHtml } = await import(live('api.js'));

  const host = document.createElement('div');
  host.innerHTML = renderMarkdown('[label](""" + _LINK_TARGET + r""")');
  const anchor = host.querySelector('a');

  return {
    rendered: !!anchor,
    attributes: anchor ? Array.from(anchor.attributes).map((a) => a.name).sort() : [],
    href: anchor ? anchor.getAttribute('href') : null,
    text: anchor ? anchor.textContent : null,
    escapedDoubleQuote: escapeHtml('say "hi"'),
    escapedSingleQuote: escapeHtml("it's"),
    escapedAngle: escapeHtml('<b>&</b>'),
  };
}
"""
)


def test_link_target_stays_inside_its_own_attribute(
    authed_page: Page, base_url: str
) -> None:
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    r = authed_page.evaluate(_PROBE)

    assert r["rendered"], "the markdown link rule did not produce an <a> at all"

    # The whole point: a quote inside the target must not open a new attribute.
    assert r["attributes"] == ["href", "rel", "target"], (
        f"the rendered link carries {r['attributes']} — a quote in the link "
        "target ended the href value early and the remainder was parsed as "
        "additional attributes on the same element"
    )
    assert "data-extra" not in r["attributes"]

    # ...and the target is not silently truncated at the quote either.
    assert r["href"] is not None and r["href"].endswith('1'), (
        f"href is {r['href']!r} — the target was cut short at the quote "
        "instead of being carried through as escaped text"
    )
    assert r["text"] == "label"

    # The escaper's own contract, both positions.
    assert r["escapedDoubleQuote"] == "say &quot;hi&quot;"
    assert r["escapedSingleQuote"] == "it&#39;s"
    # Content-position escaping is unchanged, and entities are not double-escaped.
    assert r["escapedAngle"] == "&lt;b&gt;&amp;&lt;/b&gt;"
