/* Page-header context lines (#496, one per tab since #1131): the one-line
 * status in each pane's vendored home-head. Pure render over shared state;
 * callers (sessions, apps, jobs and skills refreshes) invoke it whenever
 * their slice changes, and the .status slot ellipsizes on overflow by
 * contract.
 *
 * The header rule (#1434, for every tab as #1432 moves it over): the line
 * holds only the exceptions, each in its tone colour ("1 needs you · 1
 * stalled"), and falls back to the plain count when nothing is wrong. At
 * 390px that leaves about 22 characters, so at most two parts. Code, the
 * Board (#1436), Apps (#1437), Jobs (#1438) and Life (#1439) are on it,
 * all through renderHeadStatus.
 */

import { BOARD_POLL_MS, els, state } from './state.js';
import { isHiddenChannel } from './channel-sessions.js';
import { sessionAttention } from './glance.js';
import { setTabBadge } from './tabs.js';

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
  renderBoardBadge();
}

// How long a Board payload stays the badge's source: two polls, so a late
// response never hands the count back and forth between the two sources.
const BOARD_BADGE_FRESH_MS = 2 * BOARD_POLL_MS;

// The Board tab's badge (#1436, decision 2 of #1432): how many sessions are
// in Your turn, visible from every tab. While the Board is up its own payload
// is the count, so the badge matches the lane, external sessions included.
// Everywhere else the sessions poll, which runs on every tab, carries each
// session's Board column (#1434) from the server's same routing, so the
// count stays live without polling /api/board off the Board.
export function renderBoardBadge() {
  let n;
  if (state.board && Date.now() - state.boardFetchedAt < BOARD_BADGE_FRESH_MS) {
    n = ((state.board.columns || {}).your_turn || [])
      .filter(function (c) { return !isHiddenChannel(c); }).length;
  } else {
    n = state.sessions.filter(function (s) {
      return !isHiddenChannel(s) && s.board_column === 'your_turn';
    }).length;
  }
  setTabBadge('board', n, 'waiting');
}

// Apps (#1437): "N down" while a tunnel's probe says down, else "N apps ·
// M running", where N is the Apps card's rows (trays and Code's projects
// are not apps here).
function renderAppsHead() {
  const apps = state.apps.filter(function (a) {
    return a.kind !== 'claude-code' && a.kind !== 'tray';
  });
  const down = apps.filter(function (a) {
    return a.kind === 'tunnel' && a.health === 'down';
  }).length;
  const running = state.runningApps.length;
  renderHeadStatus(
    els.appsHeadStatus,
    down ? [{ text: down + ' down', tone: 'danger' }] : [],
    plural(apps.length, 'app', 'apps') + (running ? ' · ' + running + ' running' : '')
  );
}

// Jobs (#1438): "N failing" (a failed last run, or stuck, as the Board
// counts them) and "N not firing" (a schedule with no runs), else "N jobs".
function renderJobsHead() {
  let failing = 0;
  let notFiring = 0;
  state.jobs.forEach(function (j) {
    const last = j.last_run || {};
    const outcome = last.outcome || last.status;
    if (j.stuck || (!j.running && outcome === 'failed')) failing += 1;
    if (j.coverage && j.coverage.state === 'problem') notFiring += 1;
  });
  const exceptions = [];
  if (failing) exceptions.push({ text: failing + ' failing', tone: 'danger' });
  if (notFiring) exceptions.push({ text: notFiring + ' not firing', tone: 'attention' });
  renderHeadStatus(els.jobsHeadStatus, exceptions, plural(state.jobs.length, 'job', 'jobs'));
}

// Life (#1439): "recap overdue" / "recap due" while the weekly recap wants a
// review, else "N skills". Only a recap Life OS can actually read counts.
function renderLifeHead() {
  const r = state.lifeOsRecap;
  const status = r && r.available ? r.staleness : '';
  renderHeadStatus(
    els.lifeHeadStatus,
    status === 'due' || status === 'overdue'
      ? [{ text: 'recap ' + status, tone: 'attention' }] : [],
    plural(state.lifeOsSkills.length, 'skill', 'skills')
  );
}

function renderOtherHeads() {
  renderAppsHead();
  renderJobsHead();
  renderLifeHead();
  // The Board's line is live (#1436): board.js writes it on every render.
  setStatus(els.settingsHeadStatus, 'This launcher, on this PC');
}
