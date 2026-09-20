"""Regression pin for issue #1099: both mounts of the shared composer take the
same bottom-padding recipe.

The shared composer (``composer.js``, #980) is mounted twice inside the session
overlay — once under the terminal host and once under the chat pane — and
``mountComposer`` renders byte-identical markup for both. The *only* thing that
differed between them was one CSS override: the chat mount carried
``padding-bottom: calc(env(safe-area-inset-bottom, 0px) + 8px)`` while the
terminal mount kept the base ``.compose-bar`` recipe's flat ``8px``. On a device
with a non-zero bottom inset that is a gap of exactly one
``env(safe-area-inset-bottom)`` — ~34 CSS px on a 1290x2796 iPhone — of extra
empty space below the chat composer's button grid that the terminal composer
does not have. It was reported as "Chat leaves more dead space than Terminal",
and Terminal is the mode the owner calls correct.

The override was not a double application of the inset: the inset entered
exactly once, in exactly this rule. It was drift, not duplication — it arrived
with the detached-session composer of #975 (which at the time was the only
composer in the transcript view and genuinely did have to clear the home
indicator by itself), was carried over verbatim when #983 replaced that composer
with a second mount of the shared one, and was never reconciled against the
terminal mount that had always run 8px from the overlay's edge.

**What this pin can and cannot prove.** It reads the parsed stylesheets, not a
rendered box, and that is not a shortcut — it is the only read that
discriminates. ``env(safe-area-inset-bottom)`` resolves to ``0px`` in headless
Chromium and in the WebKit/iPhone projection alike, so *before* the fix both
mounts already computed to the same ``8px`` bottom padding on this engine and no
geometry assertion anywhere could tell the two states apart. The magnitude of
the gap, and that it closes, is a device reading and is verified on the phone,
not here. Treat a green run as "the declaration did not come back", never as
"the band is gone" — that conflation is what let the previous attempt at this
issue (e8c07f0, reverted) pass a full green gate while being materially broken
on the actual phone.

The assertion is written as "no rule scoped to one composer mount declares a
bottom padding the other does not get" rather than as a literal-text match on
the old declaration, so it also catches the same drift reintroduced on the
*terminal* mount, or under a different spelling of the inset.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page

pytestmark = pytest.mark.smoke

# Walk every parsed rule, at any nesting depth, whose selector is scoped to a
# single composer mount (an id under the session overlay) AND which declares a
# bottom padding. Nesting is walked because a future rule could legitimately sit
# inside a media block; a one-level scan would miss it.
#
# Style rules are both tested and descended into: with CSS nesting every
# CSSStyleRule carries an (often empty) cssRules list, so testing `rule.cssRules`
# first would skip every selector on both engines.
_MOUNT_SCOPED_BOTTOM_PADDING_JS = r"""
() => {
  const found = [];
  const MOUNT = /#(chatComposeBar|terminalComposeBar)\b/;
  const walk = (rules, conditions) => {
    for (const rule of rules || []) {
      const media = rule.media ? (rule.conditionText || '') : '';
      const next = media ? conditions.concat(media) : conditions;
      if (rule.selectorText && MOUNT.test(rule.selectorText)) {
        const pb = rule.style.getPropertyValue('padding-bottom')
                || rule.style.getPropertyValue('padding');
        if (pb && pb.trim()) {
          found.push({
            selector: rule.selectorText.trim(),
            value: pb.trim(),
            conditions: next,
          });
        }
      }
      if (rule.cssRules && rule.cssRules.length) walk(rule.cssRules, next);
    }
  };
  for (const sheet of document.styleSheets) {
    try { walk(sheet.cssRules, []); } catch (_) { /* cross-origin sheet */ }
  }
  return found;
}
"""


def test_composer_mounts_share_one_bottom_padding_recipe(
    authed_page: Page, base_url: str
) -> None:
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    authed_page.wait_for_selector("#terminalOverlay", state="attached", timeout=5_000)

    offenders = authed_page.evaluate(_MOUNT_SCOPED_BOTTOM_PADDING_JS)

    assert offenders == [], (
        "A bottom padding is declared for one composer mount only, which is the "
        "Terminal/Chat asymmetry issue #1099 is about: on a device with a "
        "non-zero safe-area inset the two modes then leave different amounts of "
        "empty space below the same button grid. Both mounts must take the base "
        "`.compose-bar` recipe. If bottom UI genuinely needs to clear the home "
        "indicator, that belongs on `.terminal-overlay`'s own `padding-bottom` "
        "(see the comment in its rule), so every mode and all three overlays "
        f"sharing the class move together. Found: {offenders}"
    )
