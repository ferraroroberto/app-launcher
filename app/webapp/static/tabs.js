/* Five-tab switcher: Code | Apps | Jobs | Life | Board.
 *
 * Wraps the vendored _vendored/nav/nav-tabs.js (issue #355) — that file owns
 * tab/pane discovery, ARIA + roving tabindex, the standalone-PWA fixed-inset
 * .app scroller reset, and the visualViewport pin (browser-tab toolbar only;
 * never a measured translate in standalone). This module only keeps
 * state.tab in sync (apps.js/board.js/jobs.js self-gate their polling on it)
 * and re-exports setTab for board.js's chained-job / ?board= deep-link calls.
 *
 * The Coding tab's element ids keep the historical `claude` prefix
 * (tabClaude / paneClaude / state.tab='claude') — only the visible
 * label changed (issue #45). Jobs added by issue #47; Board by #300. */

import { state } from './state.js';
import { initNavTabs } from './_vendored/nav/nav-tabs.js';

let nav = null;

export function setTab(tab) {
  if (nav) nav.setTab(tab);
}

export function wireTabs() {
  nav = initNavTabs({
    onChange: function (tab) { state.tab = tab; },
  });
}
