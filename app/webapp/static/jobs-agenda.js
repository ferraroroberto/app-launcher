/* Jobs tab: the foldable Schedule agenda panel (issue #230) — a mobile-
 * native, day-grouped list of upcoming fires (the deliberate alternative
 * to a 2D calendar grid). Backed by GET /api/jobs/agenda, fetched lazily
 * when the panel opens (see wireJobsAgenda in jobs.js).
 *
 * Split out of jobs.js (audit #315) — the agenda is its own lens over the
 * job list with its own render + fetch cycle; tapping a row hands off to
 * jobs.js's revealJob() to jump the Registered-jobs list to that job.
 */

import { els } from './state.js';
import { AuthRequiredError, jsonApi } from './api.js';
import { revealJob } from './jobs.js';

const _DOW = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const _MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
              'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

function _dayKey(epoch) {
  const d = new Date(epoch * 1000);
  return d.getFullYear() + '-' + d.getMonth() + '-' + d.getDate();
}

function _dayHeader(epoch) {
  const d = new Date(epoch * 1000);
  const now = new Date();
  const a = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const b = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  const diff = Math.round((b - a) / 86400000);
  if (diff === 0) return 'Today';
  if (diff === 1) return 'Tomorrow';
  return _DOW[d.getDay()] + ' ' + d.getDate() + ' ' + _MON[d.getMonth()];
}

function _clock(epoch) {
  const d = new Date(epoch * 1000);
  const hh = String(d.getHours()).padStart(2, '0');
  const mm = String(d.getMinutes()).padStart(2, '0');
  return hh + ':' + mm;
}

function renderAgenda(data) {
  const host = els.jobsAgendaBody;
  if (!host) return;
  host.innerHTML = '';
  const occ = (data && data.occurrences) || [];
  const frequent = (data && data.frequent) || [];
  if (!occ.length && !frequent.length) {
    const p = document.createElement('p');
    p.className = 'muted small';
    p.textContent = 'No scheduled runs in the next ' +
      ((data && data.days) || 7) + ' days.';
    host.appendChild(p);
    return;
  }

  let currentKey = null;
  occ.forEach(function (o) {
    const key = _dayKey(o.fire_epoch);
    if (key !== currentKey) {
      currentKey = key;
      const h = document.createElement('div');
      h.className = 'jobs-agenda-day';
      h.textContent = _dayHeader(o.fire_epoch);
      host.appendChild(h);
    }
    const row = document.createElement('button');
    row.type = 'button';
    row.className = 'jobs-agenda-row';
    row.dataset.jobId = o.job_id;

    const time = document.createElement('span');
    time.className = 'jobs-agenda-time';
    time.textContent = _clock(o.fire_epoch);
    row.appendChild(time);

    const name = document.createElement('span');
    name.className = 'jobs-agenda-name';
    name.textContent = o.name;
    row.appendChild(name);

    if (o.cadence) {
      const chip = document.createElement('span');
      chip.className = 'kind-pill';
      chip.textContent = o.cadence;
      row.appendChild(chip);
    }
    row.addEventListener('click', function () { revealJob(o.job_id); });
    host.appendChild(row);
  });

  if (frequent.length) {
    const foot = document.createElement('div');
    foot.className = 'jobs-agenda-frequent muted small';
    foot.textContent = 'Also frequent: ' + frequent.map(function (f) {
      return f.name + ' (' + f.cadence + ')';
    }).join(' · ');
    host.appendChild(foot);
  }
}

export async function fetchAgenda() {
  const host = els.jobsAgendaBody;
  try {
    const data = await jsonApi('/api/jobs/agenda?days=7');
    renderAgenda(data);
  } catch (exc) {
    if (exc instanceof AuthRequiredError) return;
    if (host) {
      host.innerHTML = '';
      const p = document.createElement('p');
      p.className = 'muted small';
      p.textContent = 'Could not load schedule.';
      host.appendChild(p);
    }
  }
}

// Lazy + fresh: the agenda is collapsed by default and re-fetched on each
// open (mirrors the system-map panel). Nothing polls it.
export function wireJobsAgenda() {
  if (!els.jobsAgendaCard) return;
  els.jobsAgendaCard.addEventListener('toggle', function () {
    if (els.jobsAgendaCard.open) fetchAgenda().catch(function () {});
  });
}
