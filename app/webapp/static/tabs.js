/* Three-tab switcher: Coding | Apps | Jobs.
 *
 * The Coding tab's element ids keep the historical `claude` prefix
 * (tabClaude / paneClaude / state.tab='claude') — only the visible
 * label changed (issue #45). Jobs added by issue #47. */

import { els, state } from './state.js';

export function setTab(tab) {
  state.tab = tab;
  els.tabClaude.classList.toggle('active', tab === 'claude');
  els.tabApps.classList.toggle('active', tab === 'apps');
  if (els.tabJobs) els.tabJobs.classList.toggle('active', tab === 'jobs');
  els.paneClaude.hidden = tab !== 'claude';
  els.paneApps.hidden = tab !== 'apps';
  if (els.paneJobs) els.paneJobs.hidden = tab !== 'jobs';
}

export function wireTabs() {
  els.tabClaude.addEventListener('click', function () { setTab('claude'); });
  els.tabApps.addEventListener('click', function () { setTab('apps'); });
  if (els.tabJobs) {
    els.tabJobs.addEventListener('click', function () { setTab('jobs'); });
  }
}
