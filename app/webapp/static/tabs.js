/* Five-tab switcher: Board | Code | Life | Apps | Jobs (#1131: six
 * exceeded the five a bottom bar holds, forcing 11px labels). Settings is
 * no tab: every page header's gear opens its pane, and choosing any tab
 * leaves it.
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
 * label changed (issue #45). Jobs added by issue #47; Board by #300;
 * Settings by #383. */

import { state } from './state.js';
import { initNavTabs } from './_vendored/nav/nav-tabs.js';

let nav = null;

export function setTab(tab) {
  if (nav) nav.setTab(tab);
}

// Show the Settings pane over the current tab (#1131). The vendored nav
// only manages its own tabs' panes, so this hides them here and leaves no
// tab selected; the nav's next setTab (any tab tap) shows that tab's pane
// again, and onChange below hides this one.
export function openSettings() {
  const settings = document.getElementById('paneSettings');
  const tabs = document.querySelector('nav.tabs');
  if (!settings || !tabs) return;
  document.querySelectorAll('main.app > section.pane').forEach(function (pane) {
    pane.hidden = pane !== settings;
  });
  tabs.querySelectorAll('.tab[data-tab]').forEach(function (btn) {
    btn.classList.remove('active');
    btn.setAttribute('aria-selected', 'false');
  });
  tabs.dataset.activeTab = 'settings';
  state.tab = 'settings';
  const scroller = document.querySelector('.app');
  if (scroller) scroller.scrollTop = 0;
  window.scrollTo(0, 0);
}

export function wireTabs() {
  nav = initNavTabs({
    // Board leads the visual workflow, but Coding remains the first-launch
    // default. Without this explicit default the vendored controller selects
    // the first DOM tab, coupling presentation order to startup behavior.
    defaultTab: 'claude',
    onChange: function (tab) {
      state.tab = tab;
      const settings = document.getElementById('paneSettings');
      if (settings) settings.hidden = true;
    },
  });
  document.querySelectorAll('.settings-open-btn').forEach(function (btn) {
    btn.addEventListener('click', openSettings);
  });
}
