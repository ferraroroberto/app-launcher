/* Home-head summary line (#496): the one-line stats in the vendored
 * home-head card at the top of the Coding tab — sessions, running apps,
 * and the git dirty/off-main aggregate. Pure render over shared state;
 * callers (sessions/apps/git refreshes) invoke it whenever their slice
 * changes, and the .status slot ellipsizes on overflow by contract.
 */

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
}
