/* Two-tab switcher: Claude Code | Apps. */

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
