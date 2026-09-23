/* Page-header context lines (#496, one per tab since #1131): the one-line
 * status in each pane's vendored home-head. The Code tab's carries sessions,
 * running apps and the git dirty/off-main aggregate; the others a count of
 * what the tab lists. Pure render over shared state; callers (sessions,
 * apps, git, jobs and skills refreshes) invoke it whenever their slice
 * changes, and the .status slot ellipsizes on overflow by contract.
 */

function plural(n, one, many) {
  return n + ' ' + (n === 1 ? one : many);
}

function setStatus(el, text) {
  if (el) el.textContent = text;
}

import { els, state } from './state.js';

export function renderHomeHead() {
  const el = els.homeHeadStatus;
  if (!el) return;
  const parts = [];
  const sessions = state.sessions.length;
  parts.push(sessions + (sessions === 1 ? ' session' : ' sessions'));
  // Running apps only when known non-zero — the running-apps poll gates on
  // the Apps tab being visible, so away from that tab the count is merely
  // last-known; a positive number is still useful, a stale 0 is noise.
  const apps = state.runningApps.length;
  if (apps) parts.push(apps + (apps === 1 ? ' app' : ' apps') + ' running');
  if (state.gitStatus) {
    let dirty = 0;
    let offMain = 0;
    Object.keys(state.gitStatus).forEach(function (id) {
      const gs = state.gitStatus[id];
      if (!gs || !gs.is_git) return;
      // Same precedence as the tile colours: red (dirty) wins, so a repo
      // that is both counts once, as dirty.
      if (gs.dirty) dirty += 1;
      else if (gs.branch && !gs.on_default_branch) offMain += 1;
    });
    if (dirty) parts.push(dirty + ' dirty');
    if (offMain) parts.push(offMain + ' off-main');
  }
  el.textContent = parts.join(' · ');
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
