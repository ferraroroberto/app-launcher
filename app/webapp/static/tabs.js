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
  wireBottomTabsPin();
}

// Keep the floating bottom tab bar glued to the *visual* viewport bottom
// (issue #267). iOS Safari positions `position: fixed` against the layout
// viewport and only re-snaps fixed elements once a scroll / address-bar
// transition settles, so the bar visibly drifts a few px before locking.
// Translating it by the gap between the visual and layout viewport bottoms
// tracks the address bar in real time. No-op when the two viewports
// coincide (delta 0) — i.e. desktop, the e2e projections, and a settled
// phone — so it only ever moves during the transient that causes drift.
function pinBottomTabs() {
  const nav = els.tabClaude && els.tabClaude.closest('.tabs');
  if (!nav) return;
  const vp = window.visualViewport;
  if (!vp) return;
  const layoutH = document.documentElement.clientHeight;
  // Distance the visual viewport bottom sits above the layout viewport
  // bottom that the bar is anchored to. > 0 while the address bar / a
  // bottom toolbar is encroaching; the bar is pulled up to match.
  const delta = Math.round(layoutH - (vp.offsetTop + vp.height));
  nav.style.transform = delta > 0 ? 'translateY(' + -delta + 'px)' : '';
}

function wireBottomTabsPin() {
  if (!window.visualViewport) return;
  pinBottomTabs();
  window.visualViewport.addEventListener('resize', pinBottomTabs);
  window.visualViewport.addEventListener('scroll', pinBottomTabs);
  window.addEventListener('scroll', pinBottomTabs, { passive: true });
}
