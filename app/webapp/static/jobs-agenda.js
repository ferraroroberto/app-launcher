/* Jobs tab: the Next up card and the 7-day agenda sheet (#230, #1438).
 *
 * One GET /api/jobs/agenda payload feeds both: the card shows the next
 * three fires without a tap, and its header opens the whole week in a
 * sheet, day-grouped (the deliberate alternative to a 2D calendar grid),
 * with the frequent jobs that would flood it in the footer. A fire opens
 * its job's sheet (jobs.js openJobSheet).
 *
 * Fetched once per visit to the tab and again when the app comes back to
 * the foreground on it, never on the 4s jobs poll. A refresh that fails
 * after a good read keeps the fires on screen.
 */

import { els, state } from './state.js';
import { AuthRequiredError, jsonApi, toast } from './api.js';
import { actionRow } from './action-rows.js';
import { openJobSheet } from './jobs.js';
import { cadenceText } from './jobs-row.js';
import { openJobDialog } from './jobs-dialog.js';
import { icon } from './_vendored/icons/icons.js';
import { emptyStateEl } from './_vendored/empty-state/empty-state.js';

const NEXT_UP_COUNT = 3;
const AGENDA_DAYS = 7;

const _DOW = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const _MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
              'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

let agenda = null;          // the last good payload
let agendaFailed = false;   // the last fetch failed
let visitFetched = false;   // this visit to the tab has fetched already

function _dayKey(epoch) {
  const d = new Date(epoch * 1000);
  return d.getFullYear() + '-' + d.getMonth() + '-' + d.getDate();
}

function _dayOffset(epoch) {
  const d = new Date(epoch * 1000);
  const now = new Date();
  const a = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const b = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  return Math.round((b - a) / 86400000);
}

function _dayHeader(epoch) {
  const diff = _dayOffset(epoch);
  if (diff === 0) return 'Today';
  if (diff === 1) return 'Tomorrow';
  const d = new Date(epoch * 1000);
  return _DOW[d.getDay()] + ' ' + d.getDate() + ' ' + _MON[d.getMonth()];
}

// The same day, as the start of a meta line: "today", "tomorrow", "Mon 12 Oct".
function _dayWord(epoch) {
  const diff = _dayOffset(epoch);
  return diff === 0 ? 'today' : diff === 1 ? 'tomorrow' : _dayHeader(epoch);
}

function _clock(epoch) {
  const d = new Date(epoch * 1000);
  return String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
}

function openFire(o) {
  if (els.jobsAgendaSheet && els.jobsAgendaSheet.open) els.jobsAgendaSheet.close();
  if (!state.jobs.some(function (j) { return j.id === o.job_id; })) {
    toast('That job is no longer registered.', 'error');
    return;
  }
  openJobSheet(o.job_id);
}

// One fire on the action-row: the time as the leading value, the job's
// name, and a meta line. Next up's carries the day ("tomorrow · Daily
// 03:00"); the sheet's rows sit under a day header and carry the cadence.
function fireRow(o, withDay) {
  const time = document.createElement('span');
  time.className = 'jobs-fire-time';
  time.textContent = _clock(o.fire_epoch);
  const meta = [withDay ? _dayWord(o.fire_epoch) : '', cadenceText(o.cadence)]
    .filter(Boolean).join(' · ');
  const row = actionRow({
    className: 'jobs-fire-row',
    title: o.name,
    meta: meta,
    avatar: time,
    label: 'Open ' + o.name + ', next at ' + time.textContent,
    onMain: function () { openFire(o); },
  });
  row.li.dataset.jobId = o.job_id;
  return row.li;
}

// The card's lifecycle state (design.md "Async data & feedback"): the list
// when there are fires, else the empty-state block saying why.
function setNextUpState(name, glyph, message) {
  const card = els.jobsNextUpList && els.jobsNextUpList.closest('.jobs-next-card');
  if (card) card.dataset.state = name;
  const box = els.jobsNextUpState;
  if (!box) return;
  box.hidden = name === 'ready';
  if (name === 'ready') return;
  box.innerHTML = icon(glyph, 'empty-state-icon');
  const p = document.createElement('p');
  p.className = 'empty-state-message';
  p.textContent = message;
  box.appendChild(p);
}

function renderNextUp() {
  const host = els.jobsNextUpList;
  if (!host) return;
  host.replaceChildren();
  if (!agenda) {
    if (agendaFailed) setNextUpState('error', 'triangle-alert', 'Could not load the schedule.');
    else setNextUpState('loading', 'hourglass', 'Reading the schedule…');
    return;
  }
  const occ = (agenda.occurrences || []).slice(0, NEXT_UP_COUNT);
  if (!occ.length) {
    setNextUpState('empty', 'calendar-days',
      'Nothing scheduled in the next ' + (agenda.days || AGENDA_DAYS) + ' days.');
    return;
  }
  occ.forEach(function (o) { host.appendChild(fireRow(o, true)); });
  setNextUpState('ready');
}

function renderAgendaSheet() {
  const host = els.jobsAgendaBody;
  if (!host) return;
  host.replaceChildren();
  if (!agenda) {
    host.appendChild(agendaFailed
      ? emptyStateEl('triangle-alert', 'Could not load the schedule.')
      : emptyStateEl('hourglass', 'Reading the schedule…'));
    return;
  }
  const occ = agenda.occurrences || [];
  const frequent = agenda.frequent || [];
  if (!occ.length && !frequent.length) {
    // Its own next step (#1201): the same Add job dialog the Jobs card's +
    // opens.
    host.appendChild(emptyStateEl(
      'calendar-days',
      'No scheduled runs in the next ' + (agenda.days || AGENDA_DAYS) + ' days.',
      { actionLabel: 'Add job', onAction: function () {
        els.jobsAgendaSheet.close();
        openJobDialog(null);
      } }
    ));
    return;
  }

  let list = null;
  let currentKey = null;
  occ.forEach(function (o) {
    const key = _dayKey(o.fire_epoch);
    if (key !== currentKey) {
      currentKey = key;
      const h = document.createElement('h3');
      h.className = 'jobs-agenda-day';
      h.textContent = _dayHeader(o.fire_epoch);
      host.appendChild(h);
      list = document.createElement('ul');
      list.className = 'card-list action-rows';
      host.appendChild(list);
    }
    list.appendChild(fireRow(o, false));
  });

  if (frequent.length) {
    const foot = document.createElement('p');
    foot.className = 'group-footer jobs-agenda-frequent';
    foot.textContent = 'Also frequent: ' + frequent.map(function (f) {
      return f.name + ' (' + f.cadence + ')';
    }).join(' · ');
    host.appendChild(foot);
  }
}

export async function fetchAgenda() {
  try {
    agenda = await jsonApi('/api/jobs/agenda?days=' + AGENDA_DAYS);
    agendaFailed = false;
  } catch (exc) {
    if (exc instanceof AuthRequiredError) return;
    agendaFailed = true;
  }
  renderNextUp();
  if (els.jobsAgendaSheet && els.jobsAgendaSheet.open) renderAgendaSheet();
}

function openAgendaSheet() {
  renderAgendaSheet();
  els.jobsAgendaSheet.showModal();
  // Nothing to show yet (the visit's fetch failed): try again on open.
  if (!agenda) fetchAgenda().catch(function () {});
}

function onJobsVisit() {
  if (visitFetched) return;
  visitFetched = true;
  fetchAgenda().catch(function () {});
}

export function wireJobsAgenda() {
  if (!els.jobsNextUpList) return;
  document.addEventListener('launcher:tab', function (ev) {
    if (ev.detail && ev.detail.tab === 'jobs') onJobsVisit();
    else visitFetched = false;
  });
  // Back to the foreground on the Jobs tab: the fires may have moved on.
  document.addEventListener('visibilitychange', function () {
    if (!document.hidden && state.tab === 'jobs') fetchAgenda().catch(function () {});
  });
  // The tab the app reopened on is a visit too.
  if (state.tab === 'jobs') onJobsVisit();

  els.jobsAgendaOpen.addEventListener('click', openAgendaSheet);
  els.jobsAgendaSheetClose.addEventListener('click', function () { els.jobsAgendaSheet.close(); });
  els.jobsAgendaSheetDone.addEventListener('click', function () { els.jobsAgendaSheet.close(); });
}
