/* Fleet scope: Settings > Fleet scope. One switch per fleet repo; on = active
 * (fleet-wide runs include it), off = dormant (they skip it).
 *
 * GET /api/fleet/scope -> {available, reason, repos: [{name, status}]};
 * PUT /api/fleet/scope/{repo} flips one and returns the same listing. The
 * edit itself is fleet-config's writer, so this module only relays: a switch
 * flips at once, then the server's listing replaces what is drawn. A failed
 * save puts the switch back and shows the writer's own message.
 */

import { els } from './state.js';
import { apiFailToast, jsonApi, toast } from './api.js';
import { actionRow } from './action-rows.js';
import { listFilter } from './list-filter.js';
import { setSwitch, switchEl } from './_vendored/switch/switch.js';

const FILTER_ABOVE_ROWS = 12;   // design.md action-row: filterAboveRows

let filter = null;

function counts(repos) {
  const dormant = repos.filter(function (r) { return r.status === 'dormant'; }).length;
  return { active: repos.length - dormant, dormant: dormant };
}

function summary(repos) {
  const c = counts(repos);
  return c.active + ' active, ' + c.dormant + ' dormant';
}

function setRowValue(text) {
  document.querySelectorAll('[data-settings-value="fleet-scope"]').forEach(function (el) {
    el.textContent = text;
  });
}

async function save(repo, next, sw) {
  sw.disabled = true;
  setSwitch(sw, next);
  try {
    const body = await jsonApi('/api/fleet/scope/' + encodeURIComponent(repo), {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status: next ? 'active' : 'dormant' }),
    });
    render(body);
    toast(repo + (next ? ' is active.' : ' is dormant.'), 'good');
  } catch (exc) {
    setSwitch(sw, !next);
    sw.disabled = false;
    apiFailToast('Fleet scope change failed', exc);
  }
}

function repoRow(repo) {
  const sw = switchEl(repo.status === 'active', {
    label: repo.name + ' in fleet-wide runs',
    onToggle: function (next, btn) { save(repo.name, next, btn); },
  });
  const parts = actionRow({
    title: repo.name,
    // Only the exception names itself: a dormant repo is the one to notice.
    meta: repo.status === 'active' ? '' : 'Skipped by fleet-wide runs',
    inert: true,
    trailing: sw,
  });
  return parts.li;
}

function render(body) {
  const list = els.fleetScopeList;
  if (!list) return;
  const repos = body && body.available ? body.repos : [];
  const unavailable = !body || !body.available;
  list.innerHTML = '';
  repos.forEach(function (r) { list.appendChild(repoRow(r)); });
  els.fleetScopeFilterWrap.hidden = repos.length <= FILTER_ABOVE_ROWS;
  els.fleetScopeFilterHelp.hidden = els.fleetScopeFilterWrap.hidden;
  els.fleetScopeCount.hidden = unavailable;
  els.fleetScopeCount.textContent = unavailable ? '' : summary(repos);
  els.fleetScopeUnavailable.hidden = !unavailable;
  els.fleetScopeReason.textContent = unavailable ? ((body && body.reason) || 'Fleet scope could not be read.') : '';
  list.dataset.state = unavailable ? 'error' : (repos.length ? 'ready' : 'empty');
  setRowValue(unavailable ? 'Unavailable' : summary(repos));
  if (filter) filter.apply();
}

async function fetchFleetScope(quiet) {
  try {
    render(await jsonApi('/api/fleet/scope'));
  } catch (exc) {
    render({ available: false, reason: 'Fleet scope could not be read.' });
    if (!quiet) apiFailToast('Fleet scope', exc);
  }
}

export function wireFleetScope() {
  if (!els.fleetScopeList) return;
  filter = listFilter({
    input: els.fleetScopeFilterInput,
    list: els.fleetScopeList,
    empty: els.fleetScopeFilterEmpty,
    storageKey: 'app-launcher.filter.fleet-scope',
  });
  // Read on each visit to Settings and on opening the sheet, so the row's
  // value and the switches are never older than the file.
  document.querySelectorAll('[data-sheet="fleetScopeSheet"]').forEach(function (btn) {
    btn.addEventListener('click', function () { fetchFleetScope(false); });
  });
  document.addEventListener('launcher:tab', function (e) {
    if (e.detail && e.detail.tab === 'settings') fetchFleetScope(true);
  });
}
