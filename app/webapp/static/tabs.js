/* Two-tab switcher: Coding | Apps.
 *
 * The Coding tab's element ids keep the historical `claude` prefix
 * (tabClaude / paneClaude / state.tab='claude') — only the visible
 * label changed (issue #45). */

import { els, state } from './state.js';

export function setTab(tab) {
  state.tab = tab;
  els.tabClaude.classList.toggle('active', tab === 'claude');
  els.tabApps.classList.toggle('active', tab === 'apps');
  els.paneClaude.hidden = tab !== 'claude';
  els.paneApps.hidden = tab !== 'apps';
}

export function wireTabs() {
  els.tabClaude.addEventListener('click', function () { setTab('claude'); });
  els.tabApps.addEventListener('click', function () { setTab('apps'); });
}
