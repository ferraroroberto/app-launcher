/* Four-tab switcher: Code | Apps | Jobs | Life.
 *
 * The Coding tab's element ids keep the historical `claude` prefix
 * (tabClaude / paneClaude / state.tab='claude') — only the visible
 * label changed (issue #45). Jobs added by issue #47. */

import { els, state } from './state.js';

const TAB_CONFIG = [
  { name: 'claude', tab: 'tabClaude', pane: 'paneClaude' },
  { name: 'apps', tab: 'tabApps', pane: 'paneApps' },
  { name: 'jobs', tab: 'tabJobs', pane: 'paneJobs' },
  { name: 'lifeos', tab: 'tabLifeOS', pane: 'paneLifeOS' },
];

export function setTab(tab) {
  state.tab = tab;
  TAB_CONFIG.forEach(function (cfg) {
    const tabEl = els[cfg.tab];
    const paneEl = els[cfg.pane];
    const active = tab === cfg.name;
    if (tabEl) {
      tabEl.classList.toggle('active', active);
      tabEl.setAttribute('aria-selected', active ? 'true' : 'false');
      tabEl.tabIndex = active ? 0 : -1;
    }
    if (paneEl) paneEl.hidden = !active;
  });
  const nav = els.tabClaude && els.tabClaude.closest('.tabs');
  if (nav) nav.dataset.activeTab = tab;
}

export function wireTabs() {
  TAB_CONFIG.forEach(function (cfg) {
    const tabEl = els[cfg.tab];
    if (tabEl) tabEl.addEventListener('click', function () { setTab(cfg.name); });
  });
}
