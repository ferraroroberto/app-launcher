/* Apps tab: each app's phone-speed result and cold-ready trend, read from
 * fleet-config's perf ledger through GET /api/apps/perf (src/perf_ledger).
 *
 * One fact, one way: an app with no ledger record is "Not measured", a ledger
 * that cannot be read (or a failed fetch with nothing good to show) is
 * "Unknown", and neither is ever drawn as a pass. Only a record whose own
 * verdict is `pass` reads as within budget.
 */

import { jsonApi, logPollFailure } from '../api.js';
import { chip } from '../glance.js';
import { fmtAgo } from '../sessions.js';

// A ledger changes once per /perf-review run, so a slow cadence is plenty; the
// Apps poll that triggers this runs every few seconds.
const REFRESH_MS = 60000;
const SVG_NS = 'http://www.w3.org/2000/svg';

const CHECK_LABEL = {
  'cold.ready_ms': 'cold ready',
  'cold.bytes_kb': 'cold transfer',
  'warm.ready_ms': 'warm ready',
  'warm.bytes_kb': 'warm transfer',
};

// null until the first answer; `failed` once a read failed with nothing good.
const perf = { ledger: null, apps: {}, rows: {}, readAt: 0, failed: false, inflight: null };

export function fmtMs(ms) {
  return ms >= 1000 ? (Math.round(ms / 100) / 10) + ' s' : Math.round(ms) + ' ms';
}

export function fmtKb(kb) {
  return kb >= 1024 ? (Math.round(kb / 102.4) / 10) + ' MB' : Math.round(kb) + ' KB';
}

// Which words and tone an app row shows. null before the first read lands
// (the row draws nothing rather than a guess).
export function perfView(appId) {
  if (perf.failed) return { kind: 'unknown', text: 'Unknown', tone: 'attention' };
  if (perf.ledger === null) return null;
  if (perf.ledger === 'unreadable') return { kind: 'unknown', text: 'Unknown', tone: 'attention' };
  const app = perf.rows[appId];
  const entry = app && perf.apps[app];
  if (perf.ledger === 'missing' || !entry) {
    return { kind: 'none', text: 'Not measured', tone: 'neutral' };
  }
  if (entry.state === 'pass') {
    const text = entry.seeded ? 'Passed, no timings yet' : 'Within budget';
    return { kind: 'pass', text: text, entry: entry };
  }
  if (entry.state === 'over-budget') {
    return { kind: 'over', text: 'Over budget', tone: 'danger', entry: entry };
  }
  if (entry.state === 'unmeasured') {
    return { kind: 'none', text: 'Not measured', tone: 'neutral', entry: entry };
  }
  return { kind: 'unknown', text: 'Unknown', tone: 'attention', entry: entry };
}

function span(cls, text) {
  const el = document.createElement('span');
  el.className = cls;
  el.textContent = text;
  return el;
}

// "cold 3.1 s / 2.5 s": measured against budget; the failing ones flagged.
function numberPart(label, check, fmt, failed) {
  if (!check) return null;
  const text = label + ' ' + fmt(check.measured) + (check.budget != null ? ' / ' + fmt(check.budget) : '');
  const el = span('app-perf-num' + (failed ? ' is-over' : ''), text);
  return el;
}

function sparkline(values) {
  if (values.length < 2) return null;
  const w = 60;
  const h = 16;
  const lo = Math.min.apply(null, values);
  const hi = Math.max.apply(null, values);
  const range = hi - lo || 1;
  const pts = values.map(function (v, i) {
    const x = (i / (values.length - 1)) * (w - 2) + 1;
    const y = h - 2 - ((v - lo) / range) * (h - 4);
    return x.toFixed(1) + ',' + y.toFixed(1);
  });
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('class', 'app-perf-spark');
  svg.setAttribute('viewBox', '0 0 ' + w + ' ' + h);
  svg.setAttribute('role', 'img');
  svg.setAttribute('aria-label', 'Cold ready, last ' + values.length + ' runs: '
    + fmtMs(values[0]) + ' to ' + fmtMs(values[values.length - 1]));
  const line = document.createElementNS(SVG_NS, 'polyline');
  line.setAttribute('points', pts.join(' '));
  line.setAttribute('fill', 'none');
  line.setAttribute('stroke', 'currentColor');
  line.setAttribute('stroke-width', '1.5');
  line.setAttribute('stroke-linejoin', 'round');
  line.setAttribute('stroke-linecap', 'round');
  svg.appendChild(line);
  return svg;
}

// The block an app row carries under its title, or null before the first
// read. A chip only for the exceptions: normal ("Within budget") is text.
export function perfBlock(appId) {
  const view = perfView(appId);
  if (!view) return null;
  const block = document.createElement('div');
  block.className = 'app-perf';
  block.dataset.state = view.kind;
  const head = document.createElement('span');
  head.className = 'app-perf-head';
  if (view.kind === 'pass') head.appendChild(span('app-perf-state is-pass', view.text));
  else head.appendChild(chip(view.text, view.tone, 'app-perf-chip'));
  const entry = view.entry;
  const failedIds = entry ? entry.fails || [] : [];
  if (view.kind === 'over' && failedIds.length) {
    head.appendChild(span('app-perf-ago', failedIds.map(function (id) {
      return CHECK_LABEL[id] || id;
    }).join(', ')));
  } else if (entry && entry.ts && view.kind !== 'unknown') {
    const secs = Date.parse(entry.ts) / 1000;
    if (!isNaN(secs)) head.appendChild(span('app-perf-ago', fmtAgo(secs) + ' ago'));
  }
  block.appendChild(head);
  if (entry && view.kind !== 'unknown') {
    const parts = [
      numberPart('cold', entry.cold_ready_ms, fmtMs, failedIds.indexOf('cold.ready_ms') >= 0),
      numberPart('warm', entry.warm_ready_ms, fmtMs, failedIds.indexOf('warm.ready_ms') >= 0),
      numberPart('transfer', entry.warm_bytes_kb, fmtKb, failedIds.indexOf('warm.bytes_kb') >= 0),
    ].filter(Boolean);
    if (parts.length) {
      const nums = document.createElement('span');
      nums.className = 'app-perf-nums';
      parts.forEach(function (p) { nums.appendChild(p); });
      block.appendChild(nums);
    }
    const spark = sparkline(entry.trend || []);
    if (spark) head.appendChild(spark);
  }
  return block;
}

// The reference-pill link to the repo's open perf-review issues; only a
// breach has one. Not inside the row's main button: a link cannot nest in it.
export function perfIssuesLink(appId, appName) {
  const view = perfView(appId);
  if (!view || view.kind !== 'over' || !view.entry.issues_url) return null;
  const a = document.createElement('a');
  a.className = 'app-perf-issues';
  a.href = view.entry.issues_url;
  a.target = '_blank';
  a.rel = 'noopener noreferrer';
  a.dataset.track = 'apps.perf-issues';
  // The anchor is the 44px touch target; the pill inside is what is drawn.
  a.appendChild(span('app-perf-issues-pill', 'Issues'));
  a.setAttribute('aria-label', 'Open the perf-review issues for ' + appName);
  return a;
}

// Reads the ledger view; `onChange` redraws the rows when it differs from
// what was drawn. Self-throttled: a poll calls it freely, `force` skips the wait.
export function fetchAppsPerf(onChange, force) {
  if (perf.inflight) return perf.inflight;
  if (!force && perf.readAt && Date.now() - perf.readAt < REFRESH_MS) return Promise.resolve();
  const before = JSON.stringify([perf.ledger, perf.apps, perf.rows, perf.failed]);
  perf.inflight = jsonApi('/api/apps/perf').then(function (body) {
    perf.ledger = body.ledger || 'unreadable';
    perf.apps = body.apps || {};
    perf.rows = body.rows || {};
    perf.failed = false;
  }).catch(function (exc) {
    // Keep the last good read; with none, every row says Unknown.
    if (perf.ledger === null) perf.failed = true;
    logPollFailure('apps perf', exc);
  }).finally(function () {
    perf.readAt = Date.now();
    perf.inflight = null;
    if (before !== JSON.stringify([perf.ledger, perf.apps, perf.rows, perf.failed])) onChange();
  });
  return perf.inflight;
}
