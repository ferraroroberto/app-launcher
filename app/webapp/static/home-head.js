/* Page-header context lines (one per tab): the one-line
 * status in each pane's vendored home-head. Pure render over shared state;
 * callers (sessions, apps, jobs and skills refreshes) invoke it whenever
 * their slice changes, and the .status slot ellipsizes on overflow by
 * contract.
 *
 * The header rule (every tab): the line
 * holds only the exceptions, each in its tone colour ("1 stalled · 1
 * needs you"), worst first, and falls back to the plain count when nothing is wrong. At
 * 390px that leaves about 22 characters, so at most two parts. Code, the
 * Board, Apps, Jobs, Life and Settings
 * are on it, all through the vendored renderHeadStatus
 * (_vendored/home-head/home-head.js), which sorts exceptions by
 * severity: "stalled" (danger) shows before "needs you" (attention).
 */

import { BOARD_POLL_MS, els, state } from './state.js';
import { renderHeadStatus } from './_vendored/home-head/home-head.js';
import { isHiddenChannel } from './channel-sessions.js';
import { openQuestionCount } from './board/chief-keys.js';
import { sessionAttention } from './glance.js';
import { setTabBadge } from './tabs.js';
import { profileNeedsSetup } from './settings/telegram-setup.js';

function plural(n, one, many) {
  return n + ' ' + (n === 1 ? one : many);
}

// Code: "N needs you" / "N stalled" from the Board's own column routing
// (glance.js sessionAttention), else "N sessions". The running-apps count
// is the Apps tab's, and git lives in the Projects card.
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

// The Board tab's badge:
// what waits on you, visible from every tab — the chief's unanswered
// questions, fleet-wide, plus the sessions in Your turn. While the Board is
// up its own payload is both halves, so the badge matches the lanes,
// external sessions included. Everywhere else the sessions poll, which runs
// on every tab, carries each session's Board column and the
// question count, from the server's same routing and the same key rule
// (chief-keys.js), so the badge stays live without polling /api/board off
// the Board. A question count that could not be read adds nothing: the
// badge then counts sessions only, never a made-up zero.
export function renderBoardBadge() {
  let sessions;
  let questions;
  if (state.board && Date.now() - state.boardFetchedAt < BOARD_BADGE_FRESH_MS) {
    sessions = ((state.board.columns || {}).your_turn || [])
      .filter(function (c) { return !isHiddenChannel(c); }).length;
    questions = openQuestionCount(state.chiefPlan);
  } else {
    sessions = state.sessions.filter(function (s) {
      return !isHiddenChannel(s) && s.board_column === 'your_turn';
    }).length;
    questions = state.chiefQuestions;
  }
  setTabBadge('board', sessions + (questions || 0), 'waiting');
}

// Apps: "N down" while a tunnel's probe says down, else "N apps ·
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

// Jobs: "N failing" (a failed last run, or stuck, as the Board
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

// Life: "recap overdue" / "recap due" while the weekly recap wants a
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

// Settings: only what the pane itself shows as
// a problem — a Telegram profile that needs setup, or a setup status that
// could not be read — else the build this page runs ("Build a1b2c3d"), so a
// stale PWA shows from the header. Blank until /api/version answers.
export function renderSettingsHead() {
  const ch = state.lifeOsChannels;
  const exceptions = [];
  const missing = (ch.profiles || []).filter(profileNeedsSetup).length;
  if (missing) exceptions.push({ text: 'Telegram: ' + missing + ' missing', tone: 'attention' });
  if (!ch.setup && state.lifeOsChannelsFailed) {
    exceptions.push({ text: 'Telegram unreadable', tone: 'neutral' });
  }
  renderHeadStatus(els.settingsHeadStatus, exceptions,
    state.buildSha ? 'Build ' + state.buildSha : '');
}

function renderOtherHeads() {
  renderAppsHead();
  renderJobsHead();
  renderLifeHead();
  // The Board's line is live: board.js writes it on every render.
  renderSettingsHead();
}
