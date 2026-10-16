/* The page header's CPU reading (#1522): "is the box loaded?" at a glance
 * on every tab, and "by what?" one tap away.
 *
 * Every pane's home-head carries a .cpu-reading button left of the theme
 * toggle. It is a quiet muted number at rest; colour only rings an
 * exception — attention once CPU stays at or above 70% for ~30 s, danger at
 * or above 90% — and a reading the server could not take shows "–", never
 * "0%". The level is the server's (src/system_load.py), so the rule lives in
 * one place.
 *
 * One small poll for the whole page, never one per tab: the server answers
 * from its shared sampler's cached sample, and the poll skips while the
 * page is hidden. Coming back to the foreground reads once at once. While
 * the sampler is still taking its first sample, one short retry follows.
 */

import { SYSTEM_LOAD_WARMING_RETRY_MS } from './state.js';
import { jsonApi, logPollFailure } from './api.js';
import { el, pctText, sheetRow } from './dom-utils.js';

// The last payload; null when the fetch itself failed. Before the first
// answer it reads as the server's own "still taking the first sample".
let reading = { level: 'unknown', reason: 'warming' };
let retryTimer = null;

function readingKnown(r) {
  return !!(r && r.level !== 'unknown' && typeof r.cpu_percent === 'number');
}

// Why a reading is unknown, in words: each condition its own message.
function unknownText(r) {
  const reason = r ? r.reason : 'fetch';
  if (reason === 'warming') return 'Reading the CPU…';
  if (reason === 'stale') return 'Last reading is too old';
  if (reason === 'error') return "Couldn't read the CPU";
  return "Couldn't reach the launcher";
}

function gb(bytes) {
  return (bytes / 1024 ** 3).toFixed(1).replace(/\.0$/, '');
}

function coresText(n) {
  const v = Math.round(n * 10) / 10;
  return v + (v === 1 ? ' core' : ' cores');
}

function renderReadings() {
  const known = readingKnown(reading);
  const level = known ? reading.level : 'unknown';
  const text = known ? pctText(reading.cpu_percent) : '–';
  const label = known
    ? 'CPU ' + text + (level === 'attention' ? ', high for 30 seconds' : '')
      + (level === 'danger' ? ', very high' : '') + '. Show the busiest processes'
    : 'CPU load unknown. Show details';
  document.querySelectorAll('.cpu-reading').forEach(function (btn) {
    btn.textContent = text;
    btn.dataset.level = level;
    btn.setAttribute('aria-label', label);
  });
}

function renderSheet() {
  const body = document.getElementById('cpuSheetBody');
  if (!body) return;
  body.replaceChildren();
  if (!readingKnown(reading)) {
    body.appendChild(sheetRow('CPU', unknownText(reading), 'cpu'));
    return;
  }
  const r = reading;
  const cpu = r.cores ? pctText(r.cpu_percent) + ' of ' + r.cores + ' cores' : pctText(r.cpu_percent);
  const cpuRow = sheetRow('CPU', cpu, 'cpu');
  if (r.level !== 'normal') cpuRow.lastChild.dataset.tone = r.level;
  body.appendChild(cpuRow);
  const mem = r.memory;
  body.appendChild(sheetRow(
    'Memory',
    mem && mem.total_bytes
      ? gb(mem.used_bytes) + ' of ' + gb(mem.total_bytes) + ' GB (' + pctText(mem.percent) + ')'
      : 'Unknown',
    'memory'
  ));
  body.appendChild(el('p', 'cpu-sheet-caption', 'Busiest processes, last few seconds'));
  if (!r.top || !r.top.length) {
    body.appendChild(sheetRow('Processes', 'None busy', 'top-none'));
    return;
  }
  r.top.forEach(function (p, i) {
    body.appendChild(sheetRow(groupLabel(p), coresText(p.cores), 'top-' + i));
  });
  // What the rows above do not explain, so they add up to the CPU line.
  // Hidden when it rounds to nothing.
  if (typeof r.other_cores === 'number' && Math.round(r.other_cores * 10) > 0) {
    body.appendChild(sheetRow('Everything else', coresText(r.other_cores), 'other'));
  }
}

// "python ×16 · fleet-config-wt-1440": a count only for a real group (never
// "×1"), and the repo or worktree folder when the server resolved one.
function groupLabel(p) {
  return p.name + (p.count > 1 ? ' ×' + p.count : '') + (p.owner ? ' · ' + p.owner : '');
}

function sheet() {
  return document.getElementById('cpuSheet');
}

function render() {
  renderReadings();
  const dialog = sheet();
  if (dialog && dialog.open) renderSheet();
}

export async function fetchSystemLoad() {
  if (document.hidden) return;
  try {
    reading = await jsonApi('/api/system/load');
  } catch (exc) {
    reading = null;
    render();
    logPollFailure('system load poll failed', exc);
    return;
  }
  render();
  // The sampler's first sample lands ~2 s after the first read wakes it:
  // one short retry instead of a "–" for a whole poll.
  if (reading && reading.reason === 'warming' && !retryTimer) {
    retryTimer = setTimeout(function () {
      retryTimer = null;
      fetchSystemLoad().catch(function () {});
    }, SYSTEM_LOAD_WARMING_RETRY_MS);
  }
}

function openSheet() {
  const dialog = sheet();
  if (!dialog) return;
  renderSheet();
  if (!dialog.open) dialog.showModal();
}

function closeSheet() {
  const dialog = sheet();
  if (dialog && dialog.open) dialog.close();
}

// The poll itself is armed by main.js after the boot fetch, like every
// other poll; this wires the taps and the foreground read.
export function wireSystemLoad() {
  renderReadings();
  document.querySelectorAll('.cpu-reading').forEach(function (btn) {
    btn.addEventListener('click', openSheet);
  });
  const close = document.getElementById('cpuSheetClose');
  const done = document.getElementById('cpuSheetDone');
  if (close) close.addEventListener('click', closeSheet);
  if (done) done.addEventListener('click', closeSheet);
  document.addEventListener('visibilitychange', function () {
    if (!document.hidden) fetchSystemLoad().catch(function () {});
  });
}
