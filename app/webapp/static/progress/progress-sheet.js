/* The History sheet: what the fleet shipped and the tokens it spent.
 *
 * Opened from the Usage card's ⋮ menu. It reads GET /api/progress, which
 * answers from its cache at once, so the sheet draws the cached figures the
 * moment it opens, however old, and says how old they are. Opening also asks
 * for a refresh (?refresh=true); while the server recalculates, a toast rides
 * the sheet's footer and the sheet polls the cache, then updates in place.
 * Changing the bucket or the range re-reads the cache only.
 *
 * A figure that was not measured shows its word (unknown, unavailable, stale,
 * not measured), never zero. The maths is progress-model.js; the drawing of a
 * chart is progress-charts.js.
 */

import { AuthRequiredError, jsonApi, logPollFailure, toast } from '../api.js';
import { staleMessage, setAsyncState } from '../async-state.js';
import { brandIconEl, el } from '../dom-utils.js';
import { icon } from '../_vendored/icons/icons.js';
import { chip } from '../glance.js';
import { drawChart } from './progress-charts.js';
import {
  DEFAULT_RANGE, agentShares, ageText, fmtN, githubSeries, githubTotal, halfStatus, progressUrl,
  rangeDays, rangeText, repoRows, reposCaption, summary, tiles, tokenSeries,
} from './progress-model.js';

// How often the cache is re-read while a refresh runs, and how many times
// before the sheet stops waiting (a first backfill can take minutes).
const POLL_MS = 4000;
const MAX_POLLS = 150;
// The refresh toast stays until the sheet hides it.
const TOAST_HOLD_MS = 10 * 60 * 1000;

const view = {
  bucket: 'day',
  range: DEFAULT_RANGE,
  custom: { from: '', to: '' },
  sort: 'prs_merged',
  direction: 'in',
  data: null,
  fetchedAt: null,
  seq: 0,
  failed: false,
  timer: null,
  refreshing: false,
};
// Redraw callbacks for the charts, run again on every resize of their host.
const charts = new Map();
let resizeObserver = null;

function dialog() { return document.getElementById('progressSheet'); }
function node(id) { return document.getElementById(id); }
function isOpen() { const d = dialog(); return !!(d && d.open); }

// ------------------------------------------------------------- reading

function currentRange() {
  return rangeDays(view.range, view.custom, new Date());
}

async function read(refresh) {
  const range = currentRange();
  return jsonApi(progressUrl(view.bucket, range.from, range.to, refresh));
}

function stopPolling() {
  clearTimeout(view.timer);
  view.timer = null;
}

function hideToast() {
  const host = node('progressToast');
  if (host) host.hidden = true;
}

function showToast(message, kind) {
  const host = node('progressToast');
  if (!host) return;
  toast(message, kind || '', { host: host, icon: kind === 'error' ? 'triangle-alert' : 'refresh-cw', duration: TOAST_HOLD_MS });
}

// One read of the cache, drawn. A failed read leaves the last good figures up
// under the stale line; with nothing yet it is the error block with a Retry.
async function load(refresh) {
  const seq = ++view.seq;
  const host = node('progressState');
  if (!view.data) setAsyncState(host, 'loading', { message: 'Reading the history…', glyph: 'hourglass' });
  try {
    const data = await read(refresh);
    if (seq !== view.seq || !isOpen()) return null;
    view.data = data;
    view.fetchedAt = new Date();
    view.failed = false;
    setAsyncState(host, 'ready');
    render();
    return data;
  } catch (exc) {
    if (seq !== view.seq) return null;
    if (exc instanceof AuthRequiredError) return null;
    logPollFailure('progress read failed', exc);
    view.failed = true;
    if (view.data) setAsyncState(host, 'stale', { message: staleMessage(view.fetchedAt) });
    else {
      setAsyncState(host, 'error', {
        message: 'Could not read the history.',
        retry: function () { load(false); },
      });
      node('progressContent').hidden = true;
    }
    return null;
  }
}

// Ask the server to recalculate, then follow the cache until it says it is
// done. The answer to the ask is still the cache, so nothing waits on it.
async function refresh() {
  if (view.refreshing) return;
  view.refreshing = true;
  syncRefreshButton();
  showToast('Refreshing GitHub and token totals…');
  stopPolling();
  await load(true);
  // A read the user's own change superseded still updates view.data, so the
  // loop follows the sheet's latest answer, not one call's return value.
  for (let n = 0; view.data && view.data.refreshing && !view.failed && n < MAX_POLLS && isOpen(); n += 1) {
    await new Promise(function (resolve) { view.timer = setTimeout(resolve, POLL_MS); });
    if (!isOpen()) break;
    await load(false);
  }
  view.refreshing = false;
  syncRefreshButton();
  if (!isOpen()) return;
  if (view.failed || !view.data) showToast('Could not refresh. Showing the last figures.', 'error');
  else if (view.data.refreshing) showToast('Still refreshing. The figures update when it finishes.');
  else hideToast();
}

function syncRefreshButton() {
  const button = node('progressRefresh');
  if (button) button.disabled = view.refreshing;
}

// -------------------------------------------------------------- render

function glyph(name) {
  const span = el('span', 'progress-glyph');
  span.innerHTML = icon(name);
  return span;
}

// The word a missing figure carries: the half's own state when it is not
// readable, else what the metric is.
function missingWord(source, status) {
  if (status.state === 'unavailable') return 'unavailable';
  if (status.state === 'unknown') return 'unknown';
  return source === 'tokens' ? 'not measured' : 'unknown';
}

function signed(prefix, n) {
  return prefix + fmtN(n);
}

function figure(value, className, prefix) {
  const span = el('span', className, value == null ? '' : (prefix || '') + fmtN(value));
  return span;
}

function tileValue(tile, status) {
  const wrap = el('span', 'progress-tile-value');
  const known = tile.pair.filter(function (v) { return v != null; });
  if (!known.length) {
    const word = el('span', 'progress-missing', missingWord(tile.source, status));
    wrap.appendChild(word);
    return wrap;
  }
  if (tile.key === 'lines') {
    wrap.append(
      tile.pair[0] == null ? el('span', 'progress-missing', 'unknown') : el('span', 'progress-pos', signed('+', tile.pair[0])),
      ' ',
      tile.pair[1] == null ? el('span', 'progress-missing', 'unknown') : el('span', 'progress-neg', signed('−', tile.pair[1])),
    );
    return wrap;
  }
  tile.pair.forEach(function (v, i) {
    if (i) wrap.appendChild(el('span', 'progress-sep', '/'));
    wrap.appendChild(v == null ? el('span', 'progress-missing', missingWord(tile.source, status)) : figure(v));
  });
  return wrap;
}

function tileCaption(tile, data) {
  const pair = tile.pair;
  if (tile.key === 'lines' && pair[0] != null && pair[1] != null) {
    const net = pair[0] - pair[1];
    return 'net ' + (net < 0 ? '−' : '') + fmtN(Math.abs(net));
  }
  if (tile.key === 'prs' && pair[0] != null) {
    const range = currentRange();
    const days = Math.max(1, Math.round((Date.parse(range.to) - Date.parse(range.from)) / 864e5) + 1);
    return (pair[0] / days).toFixed(1) + ' a day';
  }
  if (tile.key === 'issues') return 'opened / closed';
  if (tile.key === 'tokens') return 'in / out';
  return '';
}

function renderTiles(data) {
  const sum = summary(data);
  const host = node('progressTiles');
  host.replaceChildren();
  tiles(data).forEach(function (tile) {
    const status = tile.source === 'tokens' ? sum.tokens : sum.github;
    const card = el('div', 'progress-tile');
    card.dataset.tile = tile.key;
    const label = el('div', 'progress-tile-label');
    label.append(glyph(tile.glyph), tile.label);
    card.append(label, tileValue(tile, status));
    const sub = el('div', 'progress-tile-sub');
    const caption = tileCaption(tile, data);
    if (caption) sub.appendChild(el('span', null, caption));
    if (status.state === 'stale') sub.appendChild(chip('stale', 'neutral'));
    card.appendChild(sub);
    host.appendChild(card);
  });
}

function setNote(id, status) {
  const note = node(id);
  if (note) note.textContent = status.state === 'ok' ? '' : status.word;
}

function paintChart(id, status, draw, subject) {
  const host = node(id);
  if (!host) return;
  const paint = function () {
    if (!status.readable) {
      host.replaceChildren(el('p', 'progress-chart-empty', (subject || 'GitHub totals') + ' ' + status.word));
      return;
    }
    draw(host);
  };
  charts.set(host, paint);
  if (!resizeObserver) {
    resizeObserver = new ResizeObserver(function (entries) {
      entries.forEach(function (entry) {
        const redo = charts.get(entry.target);
        if (redo) redo();
      });
    });
  }
  resizeObserver.observe(host);
  paint();
}

function renderCharts(data, sum) {
  const bucket = data.bucket;
  setNote('progressLocNote', sum.github);
  setNote('progressPrsNote', sum.github);
  setNote('progressIssuesNote', sum.github);
  paintChart('progressChartLoc', sum.github, function (host) {
    drawChart(host, githubSeries(data, ['lines_added', 'lines_removed']), {
      label: 'Lines added and removed', bucket: bucket,
      series: [
        { name: 'Added', kind: 'bar', fill: 'var(--success)', get: function (p) { return p.lines_added; } },
        { name: 'Removed', kind: 'bar', fill: 'url(#progressHatch)', get: function (p) { return p.lines_removed == null ? null : -p.lines_removed; } },
      ],
    });
  });
  paintChart('progressChartPrs', sum.github, function (host) {
    drawChart(host, githubSeries(data, ['prs_merged']), {
      label: 'PRs merged', bucket: bucket,
      series: [{ name: 'Merged', kind: 'bar', fill: 'var(--accent)', get: function (p) { return p.prs_merged; } }],
    });
  });
  paintChart('progressChartIssues', sum.github, function (host) {
    drawChart(host, githubSeries(data, ['issues_opened', 'issues_closed']), {
      label: 'Issues opened and closed', bucket: bucket,
      series: [
        { name: 'Opened', kind: 'line', color: 'var(--accent)', point: 'circle', get: function (p) { return p.issues_opened; } },
        { name: 'Closed', kind: 'line', color: 'var(--success)', point: 'diamond', dash: '5 3', get: function (p) { return p.issues_closed; } },
      ],
    });
  });
  renderTokensChart(data, sum);
}

function renderTokensChart(data, sum) {
  const series = tokenSeries(data, view.direction);
  const legend = node('progressTokensLegend');
  legend.replaceChildren();
  series.agents.forEach(function (info) {
    const item = el('span');
    item.appendChild(agentMark(info, 'progress-legend-mark', true));
    item.append(info.name.replace(' Code', ''));
    legend.appendChild(item);
  });
  const status = series.agents.length
    ? { readable: true, word: '' }
    : { readable: false, word: missingWord('tokens', sum.tokens) };
  paintChart('progressChartTokens', status, function (host) {
    drawChart(host, series.points, {
      label: 'Tokens ' + view.direction + ' by agent', bucket: data.bucket, stacked: true,
      series: series.agents.map(function (info) {
        return { name: info.name, kind: 'bar', fill: 'var(' + info.color + ')', get: function (p) { return p[info.id]; } };
      }),
    });
  }, 'Tokens');
}

// `colourOnly` is the legend's swatch: the brand colour alone, no icon or letter.
// A row's mark is the icon on a neutral tile, because several brand icons are
// drawn in their own colour and would vanish on that colour.
function agentMark(info, extraClass, colourOnly) {
  const mark = el('span', 'progress-agent-mark ' + (extraClass || ''));
  if (colourOnly) {
    mark.style.background = 'var(' + info.color + ')';
    return mark;
  }
  if (info.brand) mark.appendChild(brandIconEl(info.brand));
  else mark.textContent = (info.name[0] || '?').toUpperCase();
  return mark;
}

function renderRepos(data) {
  const rows = repoRows(data, view.sort);
  const body = node('progressRepos').querySelector('tbody');
  body.replaceChildren();
  if (!rows.length) {
    const row = el('tr');
    const cell = el('td', 'progress-table-empty', 'No repos read yet');
    cell.colSpan = 6;
    row.appendChild(cell);
    body.appendChild(row);
  }
  rows.forEach(function (r) {
    const row = el('tr');
    row.dataset.repo = r.repo;
    const name = el('td', 'progress-repo', r.repo);
    if (r.state === 'stale') name.appendChild(chip('stale', 'neutral'));
    row.appendChild(name);
    row.appendChild(el('td', 'num progress-pos', '+' + fmtN(r.lines_added)));
    row.appendChild(el('td', 'num progress-neg', '−' + fmtN(r.lines_removed)));
    [r.prs_merged, r.issues_opened, r.issues_closed].forEach(function (v) {
      row.appendChild(el('td', 'num', fmtN(v)));
    });
    body.appendChild(row);
  });
  node('progressRepos').querySelectorAll('th button').forEach(function (button) {
    if (button.dataset.sort === view.sort) button.setAttribute('aria-sort', 'descending');
    else button.removeAttribute('aria-sort');
  });
  node('progressReposMeta').textContent = reposCaption(data);
}

function agentMeta(row) {
  if (row.state === 'not_measured') return 'Not measured: its local logs carry no token counts';
  if (row.state === 'unknown') return 'Unknown: no day of the range was measured';
  const gaps = row.unknown_days ? ' · ' + row.unknown_days + (row.unknown_days === 1 ? ' day' : ' days') + ' not measured' : '';
  return fmtN(row.tokens_in) + ' in · ' + fmtN(row.tokens_out) + ' out' + gaps;
}

function renderAgents(data, sum) {
  const rows = agentShares(data);
  const split = node('progressSplit');
  const list = node('progressAgents');
  split.replaceChildren();
  list.replaceChildren();
  rows.forEach(function (row) {
    if (row.share) {
      const seg = el('i');
      seg.style.width = (row.share * 100).toFixed(2) + '%';
      seg.style.background = 'var(' + row.info.color + ')';
      seg.title = row.info.name;
      split.appendChild(seg);
    }
    const item = el('div', 'progress-agent-row');
    item.dataset.agent = row.id;
    item.appendChild(agentMark(row.info));
    item.appendChild(el('span', 'progress-agent-name', row.info.name));
    const share = row.share == null ? '–' : (row.share * 100).toFixed(row.share < 0.01 && row.share > 0 ? 1 : 0) + '%';
    item.appendChild(el('span', 'progress-agent-share', share));
    item.appendChild(el('span', 'progress-agent-meta', agentMeta(row)));
    list.appendChild(item);
  });
  if (!rows.length) {
    list.appendChild(el('p', 'progress-note', 'No agent reported tokens: ' + missingWord('tokens', sum.tokens)));
  }
  split.hidden = !split.childElementCount;
}

const SOURCE_COPY = {
  github: {
    what: 'Lines, PRs, issues',
    from: 'Default-branch commits and merged PRs, read once and kept as daily rollups. Refresh reads only what is new.',
  },
  tokens: {
    what: 'Tokens',
    from: 'The hub’s per-day usage history for the agents that log token counts.',
  },
};

const STATE_SENTENCE = {
  stale: 'The last read failed; showing what was kept.',
  unknown: 'Not read yet.',
  unavailable: 'Could not be read right now.',
};

function sourceRow(key, status) {
  const copy = SOURCE_COPY[key];
  const row = el('li');
  row.dataset.source = key;
  row.appendChild(el('span', 'progress-source-what', copy.what));
  const label = key === 'github' ? 'GitHub' : 'LLM hub';
  const tail = status.state === 'ok' || status.state === 'stale'
    ? (status.age || status.word) + (status.state === 'stale' ? ' · stale' : '')
    : status.word;
  row.appendChild(chip(label + ' · ' + tail, status.state === 'ok' ? 'neutral' : 'attention'));
  row.appendChild(el('span', 'progress-source-from', STATE_SENTENCE[status.state] || copy.from));
  return row;
}

function renderSources(sum) {
  const host = node('progressSources');
  host.replaceChildren(sourceRow('github', sum.github), sourceRow('tokens', sum.tokens));
}

function render() {
  const data = view.data;
  if (!data) return;
  const sum = summary(data);
  node('progressContent').hidden = false;
  const range = currentRange();
  node('progressSheetSub').textContent = rangeText(data.from || range.from, data.to || range.to) +
    (sum.stale ? ' · some figures are stale' : '');
  renderTiles(data);
  renderCharts(data, sum);
  renderRepos(data);
  renderAgents(data, sum);
  renderSources(sum);
}

// ------------------------------------------------------------ controls

function press(group, attr, value) {
  node('progressSheet').querySelectorAll('[' + attr + ']').forEach(function (button) {
    if (group.contains(button)) button.setAttribute('aria-pressed', String(button.getAttribute(attr) === value));
  });
}

function syncCustomInputs() {
  const custom = view.range === 'custom';
  node('progressCustom').hidden = !custom;
  if (custom && !view.custom.from) {
    const range = rangeDays('30', null, new Date());
    view.custom = { from: range.from, to: range.to };
  }
  node('progressFrom').value = view.custom.from;
  node('progressTo').value = view.custom.to;
}

function onControl(ev) {
  const button = ev.target.closest('button');
  if (!button) return;
  if (button.dataset.bucket) {
    view.bucket = button.dataset.bucket;
    press(button.parentElement, 'data-bucket', view.bucket);
    load(false);
  } else if (button.dataset.range) {
    view.range = button.dataset.range;
    press(button.parentElement, 'data-range', view.range);
    syncCustomInputs();
    load(false);
  } else if (button.dataset.direction) {
    view.direction = button.dataset.direction;
    press(button.parentElement, 'data-direction', view.direction);
    if (view.data) renderTokensChart(view.data, summary(view.data));
  } else if (button.dataset.sort) {
    view.sort = button.dataset.sort;
    if (view.data) renderRepos(view.data);
  }
}

function onCustomChange() {
  view.custom = { from: node('progressFrom').value, to: node('progressTo').value };
  if (rangeDays('custom', view.custom, new Date()).valid) load(false);
}

function close() {
  const d = dialog();
  if (d && d.open) d.close();
}

export function openProgressSheet() {
  const d = dialog();
  if (!d) return;
  if (!d.open) d.showModal();
  syncCustomInputs();
  load(false).then(function () { if (isOpen()) refresh(); });
}

export function wireProgressSheet() {
  const d = dialog();
  if (!d) return;
  d.addEventListener('click', onControl);
  node('progressFrom').addEventListener('change', onCustomChange);
  node('progressTo').addEventListener('change', onCustomChange);
  node('progressRefresh').addEventListener('click', refresh);
  node('progressSheetClose').addEventListener('click', close);
  node('progressSheetDone').addEventListener('click', close);
  d.addEventListener('close', function () {
    stopPolling();
    view.seq += 1;
    view.refreshing = false;
    syncRefreshButton();
    hideToast();
    charts.clear();
    if (resizeObserver) { resizeObserver.disconnect(); resizeObserver = null; }
  });
}
