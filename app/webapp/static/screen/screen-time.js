/* Screen time: how long the launcher was on screen, like
 * the phone's Screen Time, in Settings › Activity.
 *
 * The minutes are nothing new to record. The vendored usage log already stores
 * each visit's start and end, and its GET /api/usage/time turns them into
 * foreground minutes (overlaps merged, a desktop's idle stretches cut at
 * 15 min; the rules live in src/usage_log.py). This module only draws it:
 *
 * - the Settings row's value, today's total, read whenever Settings opens;
 * - the sheet: a Today / 7 days / 30 days period, the total, daily bars
 *   split by device class (several days only), the device totals, which
 *   double as the chart's legend, the time per section (tab) and the uses
 *   per input feature, split by device (GET /api/usage/features; the page
 *   records them in settings/feature-track.js).
 *
 * The sheet asks three times per period, by=device, by=view and the feature
 * counts: the time route keeps one breakdown per answer. A failed feature
 * read hides only that block. No poll: it reads on open and on a period change.
 *
 * The sheet's primary, Send to task-os, sends the last 7 completed
 * days now (POST /api/screen-time/export, the same code as the nightly
 * usage-export Job), and the line above it says when that last worked, or
 * why the newest try failed (GET /api/screen-time/export, read on open).
 */

import { apiFailToast, jsonApi, logPollFailure, toast } from '../api.js';
import { state } from '../state.js';
import { setAsyncState } from '../async-state.js';
import { renderSettingsValues } from '../settings/settings-sheets.js';
import { calendarDaysFromToday } from '../dom-utils.js';

// Device classes in drawing order. Each fill differs by pattern as well as
// tone (design.md Charts: a non-colour second channel); styles/settings.css draws
// them from data-device.
const DEVICES = [
  { id: 'desktop', label: 'PC' },
  { id: 'phone', label: 'Phone' },
  { id: 'tablet', label: 'Tablet' },
];

// The input features in the order they tie-break, with their labels. The ids
// are src/usage/feature_counts.py's FEATURE_IDS.
const FEATURES = [
  { id: 'feat.send.typed', label: 'Sent typed' },
  { id: 'feat.send.dictated', label: 'Sent dictated' },
  { id: 'feat.dictation', label: 'Dictations' },
  { id: 'feat.attach.image', label: 'Images' },
  { id: 'feat.attach.file', label: 'Files' },
  { id: 'feat.attach.large', label: 'Large files' },
  { id: 'feat.ocr', label: 'Text from images' },
  { id: 'feat.paste-pc', label: 'Paste to PC' },
];

// The tabs' view ids (tabs.js) and the names they show. Time before a
// visit's first view, or under an id this list doesn't know, is Other.
const SECTIONS = [
  { id: 'claude', label: 'Coding' },
  { id: 'board', label: 'Board' },
  { id: 'lifeos', label: 'Life OS' },
  { id: 'jobs', label: 'Jobs' },
  { id: 'apps', label: 'Apps' },
  { id: 'settings', label: 'Settings' },
];

let days = 1;
let request = 0;

// "0 m", "<1 m", "45 m", "2 h", "2 h 5 m".
export function formatMinutes(minutes) {
  const m = Number(minutes) || 0;
  if (m <= 0) return '0 m';
  if (m < 1) return '<1 m';
  const whole = Math.round(m);
  const h = Math.floor(whole / 60);
  const rest = whole % 60;
  if (!h) return rest + ' m';
  return rest ? h + ' h ' + rest + ' m' : h + ' h';
}

function timeReport(n, by) {
  return jsonApi('/api/usage/time?days=' + n + '&by=' + by);
}

// The feature counts, or null when they could not be read: that hides the
// By feature block and leaves the minutes standing.
async function featureReport(n) {
  try {
    return await jsonApi('/api/usage/features?days=' + n);
  } catch (exc) {
    logPollFailure('screen time features', exc);
    return null;
  }
}

// The Settings row (settings-sheets.js renders it from state): today's
// total, or blank when it could not be read.
export async function fetchScreenTimeToday() {
  try {
    const report = await timeReport(1, 'day');
    state.screenTimeToday = report.total_minutes;
  } catch (exc) {
    logPollFailure('screen time today', exc);
    state.screenTimeToday = null;
  }
  renderSettingsValues();
}

// "2026-10-18" is a local calendar day: parse it as local noon so no zone
// moves it to the day before.
function localDay(iso) {
  return new Date(iso + 'T12:00:00');
}

function dayName(iso) {
  return localDay(iso).toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric', month: 'short' });
}

// The x labels: every day for a week, about four for a month (design.md
// Charts' tick budget), always the last day; never rotated. A month's label
// is centred under a narrow bar, so none sits in the first three, where it
// would hang past the chart's left edge.
function tickLabel(iso, index, count) {
  const d = localDay(iso);
  if (count <= 7) return d.toLocaleDateString('en-GB', { weekday: 'short' });
  if (index < 3 || (count - 1 - index) % 7 !== 0) return '';
  return d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short' });
}

function deviceParts(byDevice) {
  return DEVICES
    .filter(function (d) { return (byDevice[d.id] || 0) > 0; })
    .map(function (d) { return d.label + ' ' + formatMinutes(byDevice[d.id]); })
    .join(', ');
}

function renderChart(series) {
  const chart = document.getElementById('screenTimeChart');
  chart.hidden = series.length < 2;
  chart.replaceChildren();
  if (chart.hidden) return;
  const sums = series.map(function (row) {
    return DEVICES.reduce(function (s, d) { return s + (row.by_device[d.id] || 0); }, 0);
  });
  const top = Math.max.apply(null, sums) || 1;
  const bars = document.createElement('div');
  bars.className = 'screen-time-bars';
  const ticks = document.createElement('div');
  ticks.className = 'screen-time-ticks';
  bars.style.setProperty('--days', series.length);
  ticks.style.setProperty('--days', series.length);
  series.forEach(function (row, i) {
    const bar = document.createElement('div');
    bar.className = 'screen-time-bar';
    const parts = deviceParts(row.by_device);
    const text = dayName(row.day) + ': ' + formatMinutes(row.minutes) + (parts ? ' (' + parts + ')' : '');
    bar.title = text;
    bar.setAttribute('role', 'img');
    bar.setAttribute('aria-label', text);
    DEVICES.forEach(function (d) {
      const m = row.by_device[d.id] || 0;
      if (m <= 0) return;
      const seg = document.createElement('span');
      seg.className = 'screen-time-seg';
      seg.dataset.device = d.id;
      seg.style.height = (m / top * 100) + '%';
      bar.appendChild(seg);
    });
    bars.appendChild(bar);
    const label = tickLabel(row.day, i, series.length);
    if (!label) return;
    const tick = document.createElement('span');
    tick.className = 'screen-time-tick';
    tick.style.gridColumn = String(i + 1);
    if (i === 0) tick.dataset.edge = 'start';
    if (i === series.length - 1) tick.dataset.edge = 'end';
    tick.textContent = label;
    ticks.appendChild(tick);
  });
  chart.append(bars, ticks);
}

// One row: a label, a share bar and the minutes. `device` puts the chart's
// fill on the bar, so the device list is the chart's legend too.
function shareRow(label, minutes, total, device) {
  const li = document.createElement('li');
  li.className = 'screen-time-row';
  const name = document.createElement('span');
  name.className = 'screen-time-row-label';
  name.textContent = label;
  const track = document.createElement('span');
  track.className = 'screen-time-share';
  track.setAttribute('aria-hidden', 'true');
  const fill = document.createElement('span');
  fill.className = 'screen-time-share-fill';
  if (device) fill.dataset.device = device;
  fill.style.width = (total > 0 ? Math.min(100, minutes / total * 100) : 0) + '%';
  track.appendChild(fill);
  const value = document.createElement('span');
  value.className = 'screen-time-row-value';
  value.textContent = formatMinutes(minutes);
  li.append(name, track, value);
  return li;
}

function renderDevices(byDevice, total) {
  const list = document.getElementById('screenTimeDevices');
  list.replaceChildren(...DEVICES
    .filter(function (d) { return (byDevice[d.id] || 0) > 0; })
    .map(function (d) { return shareRow(d.label, byDevice[d.id], total, d.id); }));
}

// Every section, the busiest first; Other only when it holds time.
function renderSections(byView, total) {
  const known = new Set(SECTIONS.map(function (s) { return s.id; }));
  const other = Object.keys(byView).reduce(function (sum, id) {
    return known.has(id) ? sum : sum + byView[id];
  }, 0);
  const rows = SECTIONS.map(function (s) { return { label: s.label, minutes: byView[s.id] || 0 }; });
  rows.sort(function (a, b) { return b.minutes - a.minutes; });
  if (other > 0) rows.push({ label: 'Other', minutes: other });
  const list = document.getElementById('screenTimeSections');
  list.replaceChildren(...rows.map(function (r) { return shareRow(r.label, r.minutes, total); }));
}

// One row per feature, the busiest first: the bar is split by device (the
// chart's fills), its length the share of the busiest feature, the value the
// uses. A device's own count rides the row's title.
function featureRow(label, byDevice, total, top) {
  const li = shareRow(label, total, top, null);
  const value = li.querySelector('.screen-time-row-value');
  value.textContent = String(total);
  const fill = li.querySelector('.screen-time-share-fill');
  const track = fill.parentNode;
  track.replaceChildren(...DEVICES
    .filter(function (d) { return (byDevice[d.id] || 0) > 0; })
    .map(function (d) {
      const seg = document.createElement('span');
      seg.className = 'screen-time-share-fill';
      seg.dataset.device = d.id;
      seg.style.width = (byDevice[d.id] / top * 100) + '%';
      return seg;
    }));
  li.title = DEVICES
    .filter(function (d) { return (byDevice[d.id] || 0) > 0; })
    .map(function (d) { return d.label + ' ' + byDevice[d.id]; }).join(' · ');
  return li;
}

function renderFeatures(report) {
  const block = document.getElementById('screenTimeFeaturesBlock');
  block.hidden = !report;
  if (!report) return;
  const rows = FEATURES.map(function (f) {
    const hit = report.features.find(function (r) { return r.id === f.id; });
    return { label: f.label, total: hit ? hit.total : 0, by_device: hit ? hit.by_device : {} };
  });
  rows.sort(function (a, b) { return b.total - a.total; });
  const top = Math.max(1, ...rows.map(function (r) { return r.total; }));
  document.getElementById('screenTimeFeatures').replaceChildren(...rows.map(function (r) {
    return featureRow(r.label, r.by_device, r.total, top);
  }));
}

function caption(n, total) {
  if (n === 1) return 'Today';
  return 'Last ' + n + ' days · ' + formatMinutes(total / n) + ' a day on average';
}

function emptyMessage(n) {
  return n === 1 ? 'No screen time today yet.' : 'No screen time in the last ' + n + ' days.';
}

function renderReport(n, byDevice, byView, features) {
  const host = document.getElementById('screenTimeState');
  const report = document.getElementById('screenTimeReport');
  const total = byDevice.total_minutes;
  if (n === 1) {
    state.screenTimeToday = total;
    renderSettingsValues();
  }
  if (!byDevice.log_exists || total <= 0) {
    report.hidden = true;
    setAsyncState(host, 'empty', { message: emptyMessage(n) });
    return;
  }
  document.getElementById('screenTimeTotal').textContent = formatMinutes(total);
  document.getElementById('screenTimeCaption').textContent = caption(n, total);
  renderChart(byDevice.days_series);
  renderDevices(byDevice.by_device, total);
  renderSections(byView.by_view, total);
  renderFeatures(features);
  setAsyncState(host, 'ready');
  report.hidden = false;
}

async function loadSheet() {
  const n = days;
  const ticket = ++request;
  const host = document.getElementById('screenTimeState');
  document.getElementById('screenTimeReport').hidden = true;
  setAsyncState(host, 'loading', { message: 'Reading screen time…' });
  let answers;
  try {
    answers = await Promise.all([timeReport(n, 'device'), timeReport(n, 'view'), featureReport(n)]);
  } catch (exc) {
    logPollFailure('screen time', exc);
    if (ticket !== request) return;
    setAsyncState(host, 'error', { message: 'Could not load screen time.', retry: loadSheet });
    return;
  }
  // A later period tap or reopen has asked again: its answer wins.
  if (ticket !== request) return;
  renderReport(n, answers[0], answers[1], answers[2]);
}

// "today 03:10", "yesterday 03:10", "Mon 12 Oct 03:10".
function whenText(iso) {
  const at = new Date(iso);
  const clock = at.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' });
  const ago = calendarDaysFromToday(at);
  if (ago === 0) return 'today ' + clock;
  if (ago === 1) return 'yesterday ' + clock;
  return at.toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric', month: 'short' }) + ' ' + clock;
}

// The line above Send: not set up, the newest failure (only when it is newer
// than the last success), the last success, or never sent.
function sentText(status) {
  if (!status.configured) return 'Not set up: fill the task-os keys in webapp_config.json.';
  const ok = status.last_ok;
  const err = status.last_error;
  if (err && (!ok || err.at > ok.at)) return 'Last send failed ' + whenText(err.at) + ': ' + err.message;
  if (ok) return 'Last sent to task-os ' + whenText(ok.at) + '.';
  return 'Not sent to task-os yet.';
}

// Send stays enabled when the export is not set up: a tap toasts the
// server's reason, and this line already says what is missing.
function renderSent(status) {
  const line = document.getElementById('screenTimeSent');
  line.textContent = sentText(status);
  line.hidden = false;
}

async function loadSent() {
  try {
    renderSent(await jsonApi('/api/screen-time/export'));
  } catch (exc) {
    logPollFailure('screen time export status', exc);
    document.getElementById('screenTimeSent').hidden = true;
  }
}

async function sendNow() {
  const button = document.getElementById('screenTimeSend');
  button.setAttribute('aria-busy', 'true');
  button.disabled = true;
  button.textContent = 'Sending…';
  try {
    const answer = await jsonApi('/api/screen-time/export', { method: 'POST' });
    const r = answer.result;
    toast('Sent ' + r.days + ' days to task-os', 'ok');
    renderSent(answer);
  } catch (exc) {
    apiFailToast('Send failed', exc);
    await loadSent();
  } finally {
    button.removeAttribute('aria-busy');
    button.disabled = false;
    button.textContent = 'Send to task-os';
  }
}

function selectDays(n) {
  days = n;
  document.querySelectorAll('#screenTimeRange [data-days]').forEach(function (btn) {
    btn.setAttribute('aria-pressed', String(Number(btn.dataset.days) === n));
  });
  loadSheet();
}

export function wireScreenTime() {
  const row = document.querySelector('#paneSettings [data-sheet="screenTimeSheet"]');
  if (row) {
    row.addEventListener('click', loadSheet);
    row.addEventListener('click', loadSent);
  }
  document.getElementById('screenTimeSend').addEventListener('click', sendNow);
  document.querySelectorAll('#screenTimeRange [data-days]').forEach(function (btn) {
    btn.addEventListener('click', function () { selectDays(Number(btn.dataset.days)); });
  });
  document.addEventListener('launcher:tab', function (e) {
    if (e.detail && e.detail.tab === 'settings') fetchScreenTimeToday();
  });
}
