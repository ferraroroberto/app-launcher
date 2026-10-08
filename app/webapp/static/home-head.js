/* Page-header context lines (#496, one per tab since #1131): the one-line
 * status in each pane's vendored home-head. Pure render over shared state;
 * callers (sessions, apps, jobs and skills refreshes) invoke it whenever
 * their slice changes, and the .status slot ellipsizes on overflow by
 * contract.
 *
 * The header rule (#1434, for every tab as #1432 moves it over): the line
 * holds only the exceptions, each in its tone colour ("1 needs you · 1
 * stalled"), and falls back to the plain count when nothing is wrong. At
 * 390px that leaves about 22 characters, so at most two parts. Code is the
 * first tab on it; renderHeadStatus is the one writer the others will use.
 */

import { els, state } from './state.js';
import { isHiddenChannel } from './channel-sessions.js';
import { sessionAttention } from './glance.js';

// The most exceptions one header line shows (about 22 characters at 390px).
export const HEAD_MAX_PARTS = 2;

function plural(n, one, many) {
  return n + ' ' + (n === 1 ? one : many);
}

function setStatus(el, text) {
  if (el) el.textContent = text;
}

// The header rule: `exceptions` is [{ text, tone }] in priority order
// (tone: attention | danger), `fallback` the plain count. Exceptions win,
// two at most, each its own span in its tone.
export function renderHeadStatus(el, exceptions, fallback) {
  if (!el) return;
  const shown = (exceptions || []).filter(function (x) { return x && x.text; })
    .slice(0, HEAD_MAX_PARTS);
  if (!shown.length) {
    el.textContent = fallback;
    delete el.dataset.tone;
    return;
  }
  el.replaceChildren();
  shown.forEach(function (x, i) {
    if (i) el.appendChild(document.createTextNode(' · '));
    const part = document.createElement('span');
    part.className = 'head-exception';
    part.dataset.tone = x.tone;
    part.textContent = x.text;
    el.appendChild(part);
  });
  el.dataset.tone = shown[0].tone;
}

// Code: "N needs you" / "N stalled" from the Board's own column routing
// (glance.js sessionAttention), else "N sessions". The running-apps count
// is the Apps tab's, and git lives in the Projects card (#1434).
export function renderHomeHead() {
  const shown = state.sessions.filter(function (s) { return !isHiddenChannel(s); });
  let needsYou = 0;
  let stalled = 0;
  shown.forEach(function (s) {
    const att = sessionAttention(s);
    if (att === 'stalled') stalled += 1;
    else if (att === 'needs-you') needsYou += 1;
  });
  const exceptions = [];
  if (needsYou) exceptions.push({ text: needsYou + ' needs you', tone: 'attention' });
  if (stalled) exceptions.push({ text: stalled + ' stalled', tone: 'danger' });
  renderHeadStatus(els.homeHeadStatus, exceptions, plural(shown.length, 'session', 'sessions'));
  renderOtherHeads();
}

function renderOtherHeads() {
  const registered = state.apps.filter(function (a) { return a.kind !== 'claude-code'; });
  const running = state.runningApps.length;
  setStatus(els.appsHeadStatus, plural(registered.length, 'app', 'apps') +
    (running ? ' · ' + running + ' running' : ''));
  setStatus(els.jobsHeadStatus, plural(state.jobs.length, 'job', 'jobs'));
  setStatus(els.lifeHeadStatus, plural(state.lifeOsSkills.length, 'skill', 'skills'));
  setStatus(els.boardHeadStatus, 'Issues, PRs and runs across the fleet');
  setStatus(els.settingsHeadStatus, 'This launcher, on this PC');
}
