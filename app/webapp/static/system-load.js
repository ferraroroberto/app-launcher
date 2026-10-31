/* The page header's CPU reading: "is the box loaded?" at a glance
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
 *
 * The Code header's reading also carries a dot (data-pc-health) for the
 * local-llm-hub's verdict on the machine, drawn only for "watch" or "act"
 * and read on the same poll. A verdict that could not be read is "Not
 * confirmed" in the sheet, never "OK"; the hub does the alerting.
 */

import { SYSTEM_LOAD_WARMING_RETRY_MS } from './state.js';
import { jsonApi, logPollFailure, toast, apiFailToast } from './api.js';
import { confirmDialog } from './confirm-dialog.js';
import { el, sheetRow } from './dom-utils.js';
import { pctText } from './shared/format.js';

// The last payload; null when the fetch itself failed. Before the first
// answer it reads as the server's own "still taking the first sample".
let reading = { level: 'unknown', reason: 'warming' };
// The hub's verdict: undefined before the first answer, null when the launcher
// could not be asked.
let health;
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

// What the hub's health verdict reads as here. Anything the launcher could not
// read (no answer yet is `pending`) is its own state and never reads "ok".
const HEALTH_WORDS = { ok: 'OK', watch: 'Watch', act: 'Needs action', unknown: 'Not confirmed' };
const HEALTH_TONES = { watch: 'attention', act: 'danger' };

function healthView() {
  if (health === undefined) return { verdict: 'pending' };
  if (!health || !HEALTH_WORDS[health.verdict]) return { verdict: 'unknown', why: 'launcher' };
  return health;
}

function unknownHealthText(h) {
  if (h.why === 'disabled') return 'The hub is not set up in Settings';
  if (h.why === 'unreachable') return "Couldn't reach the hub";
  if (h.why === 'unreadable') return "The hub's answer wasn't a verdict";
  if (h.why === 'hub') return h.reason || "The hub couldn't confirm the machine's health";
  return "Couldn't reach the launcher";
}

// The verdict as a dot on the readings that opt in with data-pc-health (the
// Code header's), drawn only for watch or act; the label says it in words.
function renderHealthMark() {
  const h = healthView();
  const tone = HEALTH_TONES[h.verdict] || '';
  document.querySelectorAll('.cpu-reading[data-pc-health]').forEach(function (btn) {
    btn.dataset.pcHealth = tone;
    btn.title = tone ? 'CPU load. PC health: ' + HEALTH_WORDS[h.verdict].toLowerCase() : 'CPU load';
    if (!tone) return;
    btn.setAttribute('aria-label', btn.getAttribute('aria-label').replace(/\. Show .*$/, '')
      + '. PC health: ' + HEALTH_WORDS[h.verdict].toLowerCase()
      + (h.reason ? ', ' + h.reason : '') + '. Show details');
  });
}

// The hub's verdict first: the dot on the Code header's reading points here.
function appendHealthRows(body) {
  const h = healthView();
  const row = sheetRow('Health', h.verdict === 'pending' ? 'Reading…' : HEALTH_WORDS[h.verdict], 'health');
  if (HEALTH_TONES[h.verdict]) row.lastChild.dataset.tone = HEALTH_TONES[h.verdict];
  body.appendChild(row);
  if (h.owner && h.verdict !== 'unknown') body.appendChild(sheetRow('Owner', h.owner, 'owner'));
  const notes = h.verdict === 'unknown' ? [unknownHealthText(h)]
    : [h.reason, h.fix ? 'Suggested: ' + h.fix : null];
  notes.filter(Boolean).forEach(function (text) {
    body.appendChild(el('p', 'cpu-sheet-note', text));
  });
  // Only an allowlisted process gets the button; the server decides which.
  if (h.restart_id && h.verdict !== 'unknown') {
    const btn = el('button', 'button-tint cpu-sheet-restart', 'Restart');
    btn.type = 'button';
    btn.addEventListener('click', function () { restartOwner(h); });
    body.appendChild(btn);
  }
}

async function restartOwner(h) {
  const ok = await confirmDialog({
    title: 'Restart ' + h.owner + '?',
    message: 'This stops it on the PC; its host relaunches it. The hub re-checks within ten minutes.',
    action: 'Restart',
  });
  if (!ok) return;
  const host = document.getElementById('cpuSheetToast');
  try {
    const r = await jsonApi('/api/system/restart/' + encodeURIComponent(h.restart_id), { method: 'POST' });
    toast('Stopped ' + r.stopped.join(', ') + '.', 'good', { host });
  } catch (exc) {
    apiFailToast('Restart failed', exc, { host });
  }
  await fetchHealth();
}

function renderSheet() {
  const body = document.getElementById('cpuSheetBody');
  if (!body) return;
  body.replaceChildren();
  appendHealthRows(body);
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
  renderHealthMark();
  const dialog = sheet();
  if (dialog && dialog.open) renderSheet();
}

async function fetchHealth() {
  try {
    health = await jsonApi('/api/system/health');
  } catch (exc) {
    health = null;
    logPollFailure('pc health poll failed', exc);
  }
  render();
}

export async function fetchSystemLoad() {
  if (document.hidden) return;
  // Its own request: the verdict changes every few minutes, and a slow hub
  // must never hold up the CPU reading.
  fetchHealth().catch(function () {});
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
  renderHealthMark();
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
