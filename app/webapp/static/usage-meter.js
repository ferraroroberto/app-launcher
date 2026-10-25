/* Usage meter: one component and one sheet. It
 * replaced the coloured quota sentences.
 *
 *   The Code tab's Usage card: a "Claude Code" row, then 5h and Week rows
 *   (label, 6px bar, %, reset time) with a 2px pace tick on the week bar at
 *   the week-elapsed %, then one-line Codex and Grok rows, each only while
 *   Settings shows that provider. (Usage stays on Code.)
 *
 * Tapping it opens the Usage sheet: exact resets, week elapsed, Codex state
 * and the context filter's savings.
 *
 * It is fed from GET /api/rate-limits (sessions.js polls it on every tab).
 *
 * Colour follows pace, not raw percent: a bar is accent
 * while its window is on or under pace, attention when the week is ahead of
 * the week elapsed, and danger at 90% or more in either window. The 5h
 * window has no pace of its own, so it is accent or danger. A stale reading
 * dims and carries a "stale" chip; with nothing measured, the state word
 * ("unknown", "unsupported", "unavailable") is kept as text.
 *
 * The reading logic (meterReading / windowTone) is pure and touches no DOM,
 * so tests/js/usage_meter.test.mjs pins it under plain Node.
 */

import { icon } from './_vendored/icons/icons.js';
import {
  brandIconEl, el, fmtResetFull, nameLabel, pctText, quotaPace, sheetRow,
} from './dom-utils.js';
import { chip } from './glance.js';

export const DANGER_PCT = 90;

// The words a row with nothing measured keeps (and the chip a fallback
// reading carries). An unlisted state reads as "unknown".
const STATE_COPY = {
  stale: 'stale',
  unknown: 'unknown',
  unsupported: 'unsupported',
  error: 'unavailable',
};

// The "Usage shows" setting: which providers the meter draws, each on or
// off by itself. An empty list draws no meter at all (the Code tab's Usage
// card hides); anything but a list reads as the default.
export const USAGE_PROVIDERS = ['claude', 'codex', 'grok'];
export const USAGE_PROVIDERS_DEFAULT = ['claude', 'codex'];
const USAGE_SHOWS_KEY = 'app-launcher.usageshows';

// The list in card order, unknown names dropped.
export function normalizeProviders(list) {
  const names = Array.isArray(list) ? list : USAGE_PROVIDERS_DEFAULT;
  return USAGE_PROVIDERS.filter(function (name) { return names.indexOf(name) !== -1; });
}

export function usageProviders(list) {
  const names = normalizeProviders(list);
  return {
    claude: names.indexOf('claude') !== -1,
    codex: names.indexOf('codex') !== -1,
    grok: names.indexOf('grok') !== -1,
  };
}

// Grok exposes no session or weekly limit to scripts, so its row never draws
// a bar: it shows today's spend from Grok's own ledger when the server read
// it, and otherwise says why there is nothing to show.
function grokCost(reading) {
  const cost = reading && reading.cost;
  if (cost && cost.state === 'available' && typeof cost.usd === 'number') {
    return '$' + cost.usd.toFixed(2);
  }
  return '';
}

export function grokText(reading) {
  const cost = grokCost(reading);
  return cost ? cost + ' today' : 'limits not exposed by Grok';
}

const TONE_RANK = { none: 0, accent: 1, attention: 2, danger: 3 };

function usedPct(windowData) {
  const v = windowData && windowData.used_percentage;
  return typeof v === 'number' && !isNaN(v) ? v : null;
}

// One window's tone. `pace` is the share of the window elapsed (the week
// only); null means "no pace to compare against".
export function windowTone(pct, pace) {
  if (pct == null) return 'none';
  if (pct >= DANGER_PCT) return 'danger';
  if (pace != null && pct > pace) return 'attention';
  return 'accent';
}

function worseTone(a, b) {
  return TONE_RANK[a] >= TONE_RANK[b] ? a : b;
}

// What one quota line shows, from the backend row (src/usage/quota.py
// `_quota_line`) and, when this poll measured nothing, the last pair this
// page did measure.
//
// Claude's shard is only rewritten while a session paints its statusline and
// expires after ten minutes, so an idle stretch routinely reads `unknown`
// with a good reading behind it. That reading is kept, dimmed and chipped
// with the state word, never shown as current (a check that failed to
// establish a fact reports that as its own state). Its resets and pace are
// dropped: they may already be in the past.
export function meterReading(line, nowMs, fallback) {
  const row = line || {};
  const state = row.state || '';
  let five = row.five_hour || null;
  let week = row.weekly || null;
  const measured = usedPct(five) != null || usedPct(week) != null;
  let stale = false;
  let chipText = '';
  let withResets = true;

  if (measured) {
    stale = state === 'stale' || row.stale === true;
    if (stale) chipText = 'stale';
  } else if (fallback && (usedPct(fallback[0]) != null || usedPct(fallback[1]) != null)) {
    five = fallback[0];
    week = fallback[1];
    stale = true;
    withResets = false;
    chipText = STATE_COPY[state] || 'unknown';
  } else {
    five = null;
    week = null;
  }

  const fivePct = usedPct(five);
  const weekPct = usedPct(week);
  const pace = withResets && weekPct != null ? quotaPace(week, nowMs) : null;
  const fiveTone = windowTone(fivePct, null);
  const weekTone = windowTone(weekPct, pace);
  const hasNumbers = fivePct != null || weekPct != null;

  return {
    harness: row.harness || '',
    label: row.label || nameLabel(row.harness),
    state: state,
    measured: measured,
    five: fivePct == null ? null : {
      pct: fivePct, resetsAt: withResets ? five.resets_at : null,
    },
    week: weekPct == null ? null : {
      pct: weekPct, resetsAt: withResets ? week.resets_at : null, pace: pace,
    },
    fiveTone: fiveTone,
    weekTone: weekTone,
    tone: worseTone(fiveTone, weekTone),
    stale: stale,
    chip: chipText,
    // Kept as text when there is nothing to draw.
    note: hasNumbers ? '' : (STATE_COPY[state] || 'unknown'),
    // Grok's spend today (its row has no windows); null for the others.
    cost: row.cost_today || null,
  };
}

// --------------------------------------------------------------- rendering

// Last measured pair per harness, for the life of the page.
const lastMeasured = new Map();
let lastLines = [];
let filterStats = null;
let usageShows = USAGE_PROVIDERS_DEFAULT;
// Whether a poll has landed: the setting alone never paints an empty meter.
let polled = false;

function readings(lines) {
  const now = Date.now();
  return (Array.isArray(lines) ? lines : []).map(function (line) {
    const harness = (line && line.harness) || '';
    const reading = meterReading(line, now, lastMeasured.get(harness));
    if (reading.measured) lastMeasured.set(harness, [line.five_hour, line.weekly]);
    return reading;
  });
}

function byHarness(list, harness) {
  return list.find(function (r) { return r.harness === harness; }) || null;
}

const PROVIDER_NAMES = { claude: 'Claude Code', codex: 'Codex', grok: 'Grok' };

function mark(harness) {
  return brandIconEl(PROVIDER_NAMES[harness] ? harness : 'claude', 'um-mark');
}

// A bar: the track, its fill at the used share, and (the week only) the
// pace tick at the share elapsed.
function bar(win, tone) {
  const track = el('span', 'um-bar');
  const fill = el('span', 'um-fill');
  fill.dataset.tone = tone;
  fill.style.width = Math.max(0, Math.min(100, win.pct)) + '%';
  track.appendChild(fill);
  if (win.pace != null) {
    const tick = el('span', 'um-tick');
    tick.style.left = win.pace + '%';
    track.appendChild(tick);
  }
  return track;
}

// Compact reset stamps for these rows: the 5-hour window always resets
// today or tomorrow, so the clock alone is enough; the weekly one needs
// the day, not the time.
const ROW_RESET_CLOCK = { hour: 'numeric', minute: '2-digit' };
const ROW_RESET_DAY = { month: 'short', day: 'numeric' };

function windowRow(win, tone, label, resetFormat) {
  const row = el('span', 'um-row');
  row.dataset.tone = tone;
  row.appendChild(el('span', 'um-label', label));
  row.appendChild(bar(win, tone));
  row.appendChild(el('span', 'um-pct', pctText(win.pct)));
  const reset = fmtResetFull(win.resetsAt, resetFormat);
  const resetEl = el('span', 'um-reset');
  if (reset) {
    resetEl.innerHTML = icon('refresh-cw');
    resetEl.appendChild(document.createTextNode(reset));
  }
  row.appendChild(resetEl);
  return row;
}

// The plain reading of a line, for the meter's accessible name and title.
function readingText(r) {
  if (!r) return '';
  if (r.harness === 'grok') return r.label + ' ' + grokText(r);
  if (r.note) return r.label + ' quota ' + r.note;
  const parts = [];
  if (r.five) parts.push('5h ' + pctText(r.five.pct));
  if (r.week) {
    parts.push('week ' + pctText(r.week.pct) +
      (r.week.pace != null ? ' with ' + r.week.pace + '% of the week elapsed' : ''));
  }
  return r.label + ' ' + parts.join(', ') + (r.chip ? ' (' + r.chip + ')' : '');
}

// Codex's one-line value: both windows when measured, else its state word.
function codexValue(r) {
  const value = el('span', 'um-codex-value');
  if (!r || r.note) {
    value.textContent = r ? r.note : 'unknown';
    value.dataset.tone = 'none';
    return value;
  }
  value.dataset.tone = r.tone;
  const parts = [];
  if (r.five) parts.push('5h ' + pctText(r.five.pct));
  if (r.week) parts.push('wk ' + pctText(r.week.pct));
  value.textContent = parts.join(' · ');
  if (r.stale) value.dataset.stale = 'true';
  return value;
}

// The provider that leads a meter: Claude Code when shown (the reading that
// gates a launch), else Codex, else Grok.
function leading(show, claude, codex, grok) {
  if (show.claude) return { harness: 'claude', reading: claude };
  if (show.codex) return { harness: 'codex', reading: codex };
  return { harness: 'grok', reading: grok };
}

// A one-line row under the lead: Codex's windows, or Grok's spend.
function sideRow(harness, r) {
  const row = el('span', harness === 'grok' ? 'um-grok' : 'um-codex');
  row.appendChild(mark(harness));
  row.appendChild(el('span', 'um-name', r ? r.label : PROVIDER_NAMES[harness]));
  if (harness === 'grok') {
    const value = el('span', 'um-codex-value', grokText(r));
    value.dataset.tone = 'none';
    row.appendChild(value);
    return row;
  }
  row.appendChild(codexValue(r));
  if (r && r.chip) row.appendChild(chip(r.chip, 'neutral', 'um-chip'));
  return row;
}

function meterButton(size, lead, shown) {
  const btn = el('button', 'usage-meter usage-meter-' + size);
  btn.type = 'button';
  btn.dataset.tone = lead ? lead.tone : 'none';
  if (lead && lead.stale) btn.dataset.stale = 'true';
  const summary = shown.map(readingText).filter(Boolean).join('; ');
  btn.title = summary;
  btn.setAttribute('aria-label', 'Usage: ' + summary + '. Open usage details');
  return btn;
}

function renderFull(claude, codex, grok, show) {
  const lead = leading(show, claude, codex, grok);
  const r = lead.reading;
  const shown = [show.claude && claude, show.codex && codex, show.grok && grok].filter(Boolean);
  const btn = meterButton('full', r, shown);

  // The harness heading names a line only when there is more than one to tell
  // apart; alone, the card title and the button's name already say
  // whose it is. A chip (stale, ...) has no other home, so it keeps a row of
  // its own, ahead of the windows, with no heading beside it.
  const many = [show.claude, show.codex, show.grok].filter(Boolean).length > 1;
  if (many || (r && r.chip)) {
    const head = el('span', 'um-head');
    if (many) {
      head.appendChild(mark(lead.harness));
      head.appendChild(el('span', 'um-name', r ? r.label : PROVIDER_NAMES[lead.harness]));
    }
    if (r && r.chip) head.appendChild(chip(r.chip, 'neutral', 'um-chip'));
    btn.appendChild(head);
  }

  if (lead.harness === 'grok') {
    btn.appendChild(el('span', 'um-note', grokText(r)));
  } else if (!r || r.note) {
    btn.appendChild(el('span', 'um-note', 'Quota ' + (r ? r.note : 'unknown')));
  } else {
    const windows = el('span', 'um-windows');
    if (r.five) windows.appendChild(windowRow(r.five, r.fiveTone, '5h', ROW_RESET_CLOCK));
    if (r.week) windows.appendChild(windowRow(r.week, r.weekTone, 'Week', ROW_RESET_DAY));
    btn.appendChild(windows);
  }

  // Codex and Grok ride under the lead as one-line rows; alone, one leads.
  if (show.codex && lead.harness !== 'codex') btn.appendChild(sideRow('codex', codex));
  if (show.grok && lead.harness !== 'grok') btn.appendChild(sideRow('grok', grok));
  return btn;
}

// Swap in the fresh meter only when it differs, so the 5 s poll never
// replaces a focused button or restarts a press for nothing.
function place(container, node) {
  if (!container) return;
  if (!node) {
    if (container.firstElementChild) container.replaceChildren();
    return;
  }
  const current = container.firstElementChild;
  if (current && current.outerHTML === node.outerHTML) return;
  container.replaceChildren(node);
}

// The Usage card's summary: the pace in its tone, from Claude Code's
// reading (the one that gates a launch). Nothing measured says nothing: the
// meter below already carries the state word.
const PACE_WORDS = {
  accent: 'under pace',
  attention: 'ahead of pace',
  danger: 'nearly used',
};

export function paceSummary(reading) {
  if (!reading || reading.note) return { text: '', tone: 'none' };
  const text = PACE_WORDS[reading.tone] || '';
  return { text: text, tone: text ? reading.tone : 'none' };
}

function renderCardMeta(reading) {
  const meta = document.getElementById('codingUsageMeta');
  if (!meta) return;
  const summary = paceSummary(reading);
  meta.textContent = summary.text;
  meta.dataset.tone = summary.tone;
}

// The meter from the poll of /api/rate-limits (sessions.js), drawn for the
// providers "Usage shows" names. With none, the Code tab's Usage card
// is hidden.
export function renderUsage(lines) {
  lastLines = Array.isArray(lines) ? lines : [];
  polled = true;
  const list = readings(lastLines);
  const claude = byHarness(list, 'claude');
  const codex = byHarness(list, 'codex');
  const grok = byHarness(list, 'grok');
  const show = usageProviders(usageShows);
  const any = show.claude || show.codex || show.grok;
  const card = document.getElementById('codingUsageCard');
  if (card) card.hidden = !any;
  place(document.getElementById('codingUsage'), any ? renderFull(claude, codex, grok, show) : null);
  renderCardMeta(any ? leading(show, claude, codex, grok).reading : null);
  if (sheetOpen()) renderSheet(list);
}

// The setting, pushed in by the Settings loader so this module keeps no
// import of the page state. Repaints at once from the last poll, so a change
// on this device or a pick-up from another shows without waiting for the next.
export function setUsageShows(list) {
  const next = normalizeProviders(list);
  if (next.join(' ') === usageShows.join(' ')) return;
  usageShows = next;
  // The pre-paint choice (index.html): the next load draws this row
  // set from its first paint, and the placeholder follows it until a poll lands.
  document.documentElement.dataset.usageShows = next.join(' ');
  try { localStorage.setItem(USAGE_SHOWS_KEY, next.join(' ')); } catch (_) { /* storage blocked */ }
  if (polled) renderUsage(lastLines);
}

// The context filter's stats (context-filter.js, GET /api/context-filter),
// for the sheet's savings rows.
export function setUsageFilterStats(stats) {
  filterStats = stats || null;
  if (sheetOpen()) renderSheet(readings(lastLines));
}

// ------------------------------------------------------------- Usage sheet

function sheet() {
  return document.getElementById('usageSheet');
}

function sheetOpen() {
  const dialog = sheet();
  return !!(dialog && dialog.open);
}

// The sheet's exact resets: the 5h window resets today or tomorrow, so its
// weekday is enough; the week's needs the date.
const SHEET_RESET_5H = { weekday: 'short', hour: 'numeric', minute: '2-digit' };
const SHEET_RESET_WEEK = { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' };

// Compact k/M abbreviation for a token count — 12300 -> "12.3k".
function fmtTokens(n) {
  const v = Number(n) || 0;
  if (v >= 1e6) return (v / 1e6).toFixed(1).replace(/\.0$/, '') + 'M';
  if (v >= 1e3) return (v / 1e3).toFixed(1).replace(/\.0$/, '') + 'k';
  return String(Math.round(v));
}

function windowValue(win, format) {
  const reset = fmtResetFull(win.resetsAt, format);
  return pctText(win.pct) + (reset ? ' · resets ' + reset : '');
}

// Short labels ("Claude 5h"), so a phone-width row keeps label and value
// side by side.
function harnessRows(body, r, harness, name) {
  if (!r || r.note) {
    body.appendChild(sheetRow(name, 'Quota ' + (r ? r.note : 'unknown'), harness + '-state'));
    return;
  }
  if (r.five) body.appendChild(sheetRow(name + ' 5h', windowValue(r.five, SHEET_RESET_5H), harness + '-5h'));
  if (r.week) body.appendChild(sheetRow(name + ' week', windowValue(r.week, SHEET_RESET_WEEK), harness + '-week'));
  if (r.chip) body.appendChild(sheetRow(name + ' reading', r.chip, harness + '-state'));
}

// The context filter's savings ("Filter" is its Settings card's name).
function savingsValue(bucket) {
  const b = bucket || {};
  return fmtTokens(b.tokens_saved) + ' saved · ' + (b.rows || 0) + ' calls';
}

function renderSheet(list) {
  const body = document.getElementById('usageSheetBody');
  if (!body) return;
  const claude = byHarness(list, 'claude');
  const codex = byHarness(list, 'codex');
  const grok = byHarness(list, 'grok');
  const show = usageProviders(usageShows);
  body.replaceChildren();
  if (show.claude) harnessRows(body, claude, 'claude', 'Claude');
  if (show.claude || show.codex) {
    const lead = leading(show, claude, codex, grok).reading;
    const pace = lead && lead.week ? lead.week.pace : null;
    body.appendChild(sheetRow('Week elapsed', pace == null ? 'unknown' : pace + '%', 'elapsed'));
  }
  if (show.codex) harnessRows(body, codex, 'codex', 'Codex');
  if (show.grok) {
    body.appendChild(sheetRow('Grok today', grokCost(grok) || 'unknown', 'grok-cost'));
    body.appendChild(sheetRow('Grok limits', 'Not exposed by Grok', 'grok-limits'));
  }
  if (filterStats && filterStats.available) {
    body.appendChild(sheetRow('Filter today', savingsValue(filterStats.today), 'filter-today'));
    body.appendChild(sheetRow('Filter 7 days', savingsValue(filterStats.last_7_days), 'filter-week'));
  } else {
    body.appendChild(sheetRow('Context filter', 'No savings recorded', 'filter-none'));
  }
}

export function openUsageSheet() {
  const dialog = sheet();
  if (!dialog) return;
  renderSheet(readings(lastLines));
  if (!dialog.open) dialog.showModal();
}

function closeUsageSheet() {
  const dialog = sheet();
  if (dialog && dialog.open) dialog.close();
}

// The meter opens the sheet, and so does the rest of the Usage card (its header and footer are
// part of the one tap). The listener sits on the
// stable card: the button inside is rebuilt whenever its reading changes.
export function wireUsageMeter() {
  const card = document.getElementById('codingUsageCard');
  if (card) card.addEventListener('click', openUsageSheet);
  const close = document.getElementById('usageSheetClose');
  const done = document.getElementById('usageSheetDone');
  if (close) close.addEventListener('click', closeUsageSheet);
  if (done) done.addEventListener('click', closeUsageSheet);
}
