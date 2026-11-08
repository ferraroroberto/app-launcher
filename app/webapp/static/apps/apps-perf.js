/* Apps tab: the Performance card, read from fleet-config's perf ledger
 * through GET /api/apps/perf (src/perf_ledger).
 *
 * One fact, one way: an app with a record whose own verdict is `pass` reads
 * as within budget, and nothing else does. An app with no record, a run that
 * measured nothing, or a verdict this page does not know is listed under
 * "Not measured" with its reason; a ledger that cannot be read (or a failed
 * fetch with nothing good to show) is "Unknown", never an empty or green card.
 */

import { jsonApi, logPollFailure } from '../api.js';
import { chip } from '../glance.js';
import { setAsyncState } from '../async-state.js';
import { els } from '../state.js';
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

// Why an app is not in the measured list, in the order the card shows them.
const REASON_ORDER = ['never', 'blank-run', 'unrecognised', 'tunnel'];
const REASON_TEXT = {
  never: 'Never measured',
  'blank-run': 'Last run measured nothing',
  unrecognised: 'Result not recognised',
  tunnel: 'A tunnel, not a web app of its own',
};

// null until the first answer; `failed` once a read failed with nothing good.
const perf = { ledger: null, apps: {}, rows: {}, readAt: 0, failed: false, inflight: null };

export function fmtMs(ms) {
  return ms >= 1000 ? (Math.round(ms / 100) / 10) + ' s' : Math.round(ms) + ' ms';
}

export function fmtKb(kb) {
  return kb >= 1024 ? (Math.round(kb / 102.4) / 10) + ' MB' : Math.round(kb) + ' KB';
}

function node(tag, cls, text) {
  const el = document.createElement(tag);
  if (cls) el.className = cls;
  if (text != null) el.textContent = text;
  return el;
}

// What the card can say, split from the registry rows and the last read:
// `status` is 'loading' | 'unknown' | 'ok'; `measured` is a list of
// { name, entry } (one per ledger app, named by its first registry row);
// `unmeasured` maps a reason to the row names it covers.
export function perfSplit(apps) {
  if (perf.failed || perf.ledger === 'unreadable') return { status: 'unknown' };
  if (perf.ledger === null) return { status: 'loading' };
  const measured = [];
  const seen = {};
  const unmeasured = {};
  function skip(reason, name) {
    (unmeasured[reason] = unmeasured[reason] || []).push(name);
  }
  apps.forEach(function (a) {
    if (a.kind === 'claude-code') return;
    if (a.kind === 'tunnel') return skip('tunnel', a.name);
    const ledgerApp = perf.rows[a.id];
    const entry = ledgerApp && perf.apps[ledgerApp];
    if (!entry) return skip('never', a.name);
    if (entry.state === 'pass' || entry.state === 'over-budget') {
      if (!seen[ledgerApp]) {
        seen[ledgerApp] = true;
        measured.push({ id: a.id, name: a.name, entry: entry });
      }
      return;
    }
    skip(entry.state === 'unmeasured' ? 'blank-run' : 'unrecognised', a.name);
  });
  return { status: 'ok', measured: measured, unmeasured: unmeasured };
}

// "cold 3.1 s / 2.5 s": measured against budget; the failing ones flagged.
function numberPart(label, check, fmt, failed) {
  if (!check) return null;
  const text = label + ' ' + fmt(check.measured) + (check.budget != null ? ' / ' + fmt(check.budget) : '');
  return node('span', 'perf-num' + (failed ? ' is-over' : ''), text);
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
  svg.setAttribute('class', 'perf-spark');
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

// The reference-pill link to the repo's open perf-review issues; only a
// breach has one. The anchor is the 44px touch target, the pill is what is drawn.
function issuesLink(name, url) {
  const a = node('a', 'perf-issues');
  a.href = url;
  a.target = '_blank';
  a.rel = 'noopener noreferrer';
  a.dataset.track = 'apps.perf-issues';
  a.appendChild(node('span', 'perf-issues-pill', 'Issues'));
  a.setAttribute('aria-label', 'Open the perf-review issues for ' + name);
  return a;
}

// One measured app: name, verdict and age, then the figures against budget
// and the cold-ready sparkline. The chip is for the exception only; within
// budget is plain success text.
function measuredRow(item) {
  const entry = item.entry;
  const over = entry.state === 'over-budget';
  const failedIds = entry.fails || [];
  const li = node('li', 'perf-row');
  li.dataset.id = item.id;
  li.dataset.state = over ? 'over' : 'pass';
  const text = node('div', 'perf-row-text');
  text.appendChild(node('span', 'perf-name', item.name));

  const status = node('div', 'perf-status');
  if (over) status.appendChild(chip('Over budget', 'danger', 'perf-chip'));
  else status.appendChild(node('span', 'perf-verdict', entry.seeded ? 'Passed, no timings yet' : 'Within budget'));
  if (over && failedIds.length) {
    status.appendChild(node('span', 'perf-ago', failedIds.map(function (id) {
      return CHECK_LABEL[id] || id;
    }).join(', ')));
  } else {
    const secs = entry.ts ? Date.parse(entry.ts) / 1000 : NaN;
    if (!isNaN(secs)) status.appendChild(node('span', 'perf-ago', fmtAgo(secs) + ' ago'));
  }
  text.appendChild(status);

  const parts = [
    numberPart('cold', entry.cold_ready_ms, fmtMs, failedIds.indexOf('cold.ready_ms') >= 0),
    numberPart('warm', entry.warm_ready_ms, fmtMs, failedIds.indexOf('warm.ready_ms') >= 0),
    numberPart('transfer', entry.warm_bytes_kb, fmtKb, failedIds.indexOf('warm.bytes_kb') >= 0),
  ].filter(Boolean);
  const spark = sparkline(entry.trend || []);
  if (parts.length || spark) {
    const nums = node('div', 'perf-nums');
    parts.forEach(function (p) { nums.appendChild(p); });
    if (spark) nums.appendChild(spark);
    text.appendChild(nums);
  }
  li.appendChild(text);
  if (over && entry.issues_url) li.appendChild(issuesLink(item.name, entry.issues_url));
  return li;
}

function renderUnmeasured(host, unmeasured) {
  host.replaceChildren();
  REASON_ORDER.forEach(function (reason) {
    const names = unmeasured[reason];
    if (!names || !names.length) return;
    const group = node('div', 'perf-reason');
    group.dataset.reason = reason;
    group.appendChild(node('dt', 'perf-reason-label', REASON_TEXT[reason]));
    group.appendChild(node('dd', 'perf-reason-apps', names.join(', ')));
    host.appendChild(group);
  });
  host.hidden = host.childElementCount === 0;
}

function summaryText(split) {
  if (split.status === 'unknown') return { text: 'Unknown', over: false };
  if (split.status === 'loading') return null;
  const over = split.measured.filter(function (m) { return m.entry.state === 'over-budget'; }).length;
  if (!split.measured.length) return { text: 'None measured', over: false };
  return {
    text: split.measured.length + ' measured' + (over ? ' · ' + over + ' over budget' : ''),
    over: over > 0,
  };
}

// Draws the card from the registry rows (`apps`) and the last ledger read.
// `retry` re-reads after a read that failed with nothing good to show.
export function renderPerfCard(apps, retry) {
  const split = perfSplit(apps);
  // The list redraws on every registry poll; skip a draw that would be
  // identical so a tap on Issues is never lost to a rebuild. The minute keeps
  // the "ago" text moving.
  const sig = JSON.stringify([split, perf.failed, Math.floor(Date.now() / 60000)]);
  if (els.perfMeasured.dataset.sig === sig) return;
  els.perfMeasured.dataset.sig = sig;
  const meta = summaryText(split);
  els.perfSummaryMeta.hidden = !meta;
  els.perfSummaryMeta.textContent = meta ? meta.text : '';
  els.perfSummaryMeta.classList.toggle('is-over', !!(meta && meta.over));
  els.perfMeasured.replaceChildren();
  if (split.status !== 'ok') {
    els.perfNone.hidden = true;
    els.perfUnmeasured.replaceChildren();
    els.perfUnmeasured.hidden = true;
    if (split.status === 'loading') {
      setAsyncState(els.perfState, 'loading', { message: 'Reading performance results…' });
    } else {
      setAsyncState(els.perfState, 'error', {
        message: perf.failed ? 'Could not read the performance results.' : 'The performance ledger cannot be read.',
        retry: retry,
      });
    }
    return;
  }
  setAsyncState(els.perfState, 'ready');
  split.measured.forEach(function (item) { els.perfMeasured.appendChild(measuredRow(item)); });
  els.perfNone.hidden = split.measured.length !== 0;
  renderUnmeasured(els.perfUnmeasured, split.unmeasured);
}

// Reads the ledger view; `onChange` redraws the card when it differs from
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
    // Keep the last good read; with none, the card says Unknown.
    if (perf.ledger === null) perf.failed = true;
    logPollFailure('apps perf', exc);
  }).finally(function () {
    perf.readAt = Date.now();
    perf.inflight = null;
    if (before !== JSON.stringify([perf.ledger, perf.apps, perf.rows, perf.failed])) onChange();
  });
  return perf.inflight;
}
