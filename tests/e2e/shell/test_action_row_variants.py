"""actionRow's optional variants for the Board (#1501), pinned.

Step 1 of #1500 adds what the Board's rows need to actionRow as optional
fields: a one-line density (title and meta on one line, rows.md), a small
density (the plan lane, rows.sm), a custom trailing element (a question
row's chevron, an Answer button) and an inert row (one that reports and does
nothing on tap). A row built without them stays byte-identical, which
``test_life_tab_glance.py``'s golden pins; this module pins the variants'
own markup and the two heights.
"""
from __future__ import annotations

import pytest
from playwright.sync_api import Page

pytestmark = [pytest.mark.smoke, pytest.mark.iphone]

_ROWS = """async () => {
  const { actionRow } = await import('/static/action-rows.js');
  const { chip } = await import('/static/glance.js');
  const chevron = document.createElement('span');
  chevron.className = 'demo-chevron';
  chevron.textContent = '>';
  const rows = [
    actionRow({
      id: 'q1', className: 'board-item', title: 'Pick a sample name',
      meta: 'demo-repo', label: 'Answer: Pick a sample name',
      onMain: function () {}, density: 'one-line', trailing: chevron,
    }),
    actionRow({
      id: 'p1', className: 'board-item', title: 'Sample plan step',
      meta: 'demo-repo', chips: [chip('queued', 'neutral')],
      density: 'small', inert: true,
    }),
  ];
  return rows.map(function (r) { return r.li.outerHTML; });
}"""

_GOLDEN = [
    '<li class="action-row board-item action-row--one-line" data-id="q1"><button type="button" class="action-row-main" aria-label="Answer: Pick a sample name"><span class="action-row-title" title="Pick a sample name">Pick a sample name</span><span class="action-row-meta">demo-repo</span></button><span class="demo-chevron action-row-trailing">&gt;</span></li>',
    '<li class="action-row board-item action-row--small" data-id="p1"><div class="action-row-main is-inert"><span class="action-row-title" title="Sample plan step">Sample plan step</span><span class="action-row-meta has-chips"><span class="action-row-meta-text">demo-repo</span><span class="chip" data-tone="neutral">queued</span></span></div></li>',
]


def test_the_board_variants_render_their_pinned_markup(
    authed_page: Page, base_url: str
) -> None:
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    got = authed_page.evaluate(_ROWS)
    assert got == _GOLDEN


_HEIGHTS = """async () => {
  const { actionRow } = await import('/static/action-rows.js');
  const ul = document.createElement('ul');
  ul.className = 'action-rows';
  ul.style.width = '358px';
  document.body.appendChild(ul);
  const make = function (density) {
    const r = actionRow({
      title: 'A sample title long enough to need an ellipsis on a phone row',
      meta: 'demo-repo', label: 'x', onMain: function () {}, density: density,
    });
    ul.appendChild(r.li);
    return r;
  };
  const one = make('one-line');
  const small = make('small');
  const plain = make(undefined);
  const out = {
    one: Math.round(one.li.getBoundingClientRect().height),
    small: Math.round(small.li.getBoundingClientRect().height),
    plain: Math.round(plain.li.getBoundingClientRect().height),
    sameLine: Math.abs(one.title.getBoundingClientRect().top -
      one.meta.getBoundingClientRect().top) < 8,
  };
  ul.remove();
  return out;
}"""


def test_one_line_and_small_rows_hold_their_heights(
    authed_page: Page, base_url: str
) -> None:
    authed_page.set_viewport_size({"width": 390, "height": 844})
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    m = authed_page.evaluate(_HEIGHTS)
    assert m["one"] == 52, m
    assert m["small"] == 44, m
    assert m["plain"] == 60, m
    assert m["sameLine"], f"one-line row put the meta on its own line: {m}"
