/* The History sheet's reading of GET /api/progress, with no DOM.
 *
 * Everything the sheet shows is worked out here from the response, so
 * tests/js/progress_model.test.mjs pins it under plain Node: the totals, the
 * series each chart draws, the per-repo and per-agent rows, and the words a
 * half or a metric carries when it was not measured.
 *
 * A figure that was not measured is `null`, never 0: a bucket the GitHub half
 * could not read is a gap in its chart, a total over no readable bucket reads
 * "unknown", and an agent whose logs carry no token counts reads "not
 * measured".
 */

import { agoFromSeconds, nameLabel } from '../shared/format.js';

export const BUCKETS = ['day', 'week', 'month'];

// The range choices: a number is the last N days (today included).
export const RANGES = [
  { key: '30', label: '30d', days: 30 },
  { key: '90', label: '90d', days: 90 },
  { key: '180', label: '6m', days: 180 },
  { key: 'custom', label: 'Custom', days: null },
];
export const DEFAULT_RANGE = '30';
// The route's own cap on a range.
export const MAX_RANGE_DAYS = 400;

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

// -------------------------------------------------------------- agents

// The hub names its agents by id; the order is the stack order, bottom up.
// `color` is the agent's brand token (styles/progress.css), shared by its
// chart series, its legend mark and its share bar.
const AGENTS = [
  { id: 'claude', name: 'Claude Code', brand: 'claude', color: '--brand-claude' },
  { id: 'codex', name: 'Codex', brand: 'codex', color: '--brand-codex' },
  { id: 'copilot', name: 'Copilot', brand: 'copilot', color: '--brand-copilot' },
  { id: 'antigravity', name: 'Antigravity', brand: 'antigravity', color: '--brand-antigravity', aliases: ['agy'] },
  { id: 'pi', name: 'Pi', brand: 'pi', color: '--brand-pi' },
  { id: 'grok', name: 'Grok', brand: 'grok', color: '--brand-grok' },
];

export function agentInfo(id) {
  const key = String(id || '').toLowerCase();
  const known = AGENTS.find(function (a) { return a.id === key || (a.aliases || []).indexOf(key) !== -1; });
  return known || { id: key, name: nameLabel(key), brand: null, color: '--muted' };
}

function agentOrder(id) {
  const info = agentInfo(id);
  const at = AGENTS.findIndex(function (a) { return a.id === info.id; });
  return at === -1 ? AGENTS.length : at;
}

// ------------------------------------------------------------- numbers

// 12 345 -> "12.3k". null is "–" so a cell never reads as a zero it is not.
export function fmtN(n) {
  if (n == null || typeof n !== 'number' || isNaN(n)) return '–';
  const a = Math.abs(n);
  if (a >= 1e9) return (n / 1e9).toFixed(a >= 1e10 ? 0 : 1) + 'B';
  if (a >= 1e6) return (n / 1e6).toFixed(a >= 1e7 ? 0 : 1) + 'M';
  if (a >= 1e4) return (n / 1e3).toFixed(a >= 1e5 ? 0 : 1) + 'k';
  return n.toLocaleString('en-US');
}

// ---------------------------------------------------------------- dates

function pad(n) { return String(n).padStart(2, '0'); }

// A local calendar day as YYYY-MM-DD.
export function isoDay(date) {
  return date.getFullYear() + '-' + pad(date.getMonth() + 1) + '-' + pad(date.getDate());
}

function parseDay(iso) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(iso || ''));
  return m ? new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3])) : null;
}

function shiftDays(date, n) {
  const d = new Date(date.getFullYear(), date.getMonth(), date.getDate());
  d.setDate(d.getDate() + n);
  return d;
}

// The first and last day a range choice asks for. A custom range that is not
// a usable pair of days reads as the default window, so the sheet never sends
// a request the route would refuse.
export function rangeDays(rangeKey, custom, today) {
  const end = today || new Date();
  const choice = RANGES.find(function (r) { return r.key === rangeKey; });
  if (choice && choice.days) {
    return { from: isoDay(shiftDays(end, 1 - choice.days)), to: isoDay(end), valid: true };
  }
  const from = parseDay(custom && custom.from);
  const to = parseDay(custom && custom.to);
  if (from && to && from <= to && Math.round((to - from) / 864e5) + 1 <= MAX_RANGE_DAYS) {
    return { from: isoDay(from), to: isoDay(to), valid: true };
  }
  return { from: isoDay(shiftDays(end, -29)), to: isoDay(end), valid: false };
}

export function progressUrl(bucket, from, to, refresh) {
  let url = '/api/progress?bucket=' + encodeURIComponent(bucket) +
    '&from=' + encodeURIComponent(from) + '&to=' + encodeURIComponent(to);
  if (refresh) url += '&refresh=true';
  return url;
}

// "4 Nov" for an axis; a month bucket is just "Nov".
export function bucketLabel(start, bucket) {
  const d = parseDay(start);
  if (!d) return '';
  return bucket === 'month' ? MONTHS[d.getMonth()] : d.getDate() + ' ' + MONTHS[d.getMonth()];
}

// The tooltip's title for a bucket.
export function bucketTitle(start, bucket) {
  const d = parseDay(start);
  if (!d) return '';
  if (bucket === 'month') return MONTHS[d.getMonth()] + ' ' + d.getFullYear();
  if (bucket === 'week') return 'Week of ' + d.getDate() + ' ' + MONTHS[d.getMonth()];
  return WEEKDAYS[d.getDay()] + ' ' + d.getDate() + ' ' + MONTHS[d.getMonth()];
}

// "4 Nov – 3 Dec 2026".
export function rangeText(from, to) {
  const a = parseDay(from);
  const b = parseDay(to);
  if (!a || !b) return '';
  return a.getDate() + ' ' + MONTHS[a.getMonth()] + ' – ' + b.getDate() + ' ' + MONTHS[b.getMonth()] + ' ' + b.getFullYear();
}

// ----------------------------------------------------- the two halves

const HALF_WORDS = {
  ok: '',
  stale: 'stale',
  unknown: 'unknown',
  unavailable: 'unavailable',
};

// What a half (the GitHub totals, the hub's tokens) tells the reader:
// `word` is the state word to show when it is not ok, `readable` whether any
// figure of it can be drawn, `age` when it was last read.
export function halfStatus(half) {
  const h = half || {};
  const state = HALF_WORDS[h.state] === undefined ? 'unknown' : h.state;
  return {
    state: state,
    word: HALF_WORDS[state],
    readable: state === 'ok' || state === 'stale',
    reason: h.reason || '',
    age: agoFromSeconds(h.age_seconds),
    refreshing: !!h.refreshing,
  };
}

// Whether the sheet's data is being refreshed, and whether anything on it is
// older than a fresh read.
export function summary(data) {
  const d = data || {};
  const github = halfStatus(d.github);
  const tokens = halfStatus(d.tokens);
  return {
    github: github,
    tokens: tokens,
    refreshing: !!d.refreshing,
    stale: github.state === 'stale' || tokens.state === 'stale',
  };
}

// ------------------------------------------------------------- totals

export const GITHUB_METRICS = ['lines_added', 'lines_removed', 'prs_merged', 'issues_opened', 'issues_closed'];

function bucketsOf(data) {
  return data && Array.isArray(data.buckets) ? data.buckets : [];
}

// One GitHub metric summed over the buckets that were read; null when none
// was, so an unread range is "unknown" and not a total of zero.
export function githubTotal(data, metric) {
  let sum = null;
  bucketsOf(data).forEach(function (b) {
    const v = b.github && typeof b.github[metric] === 'number' ? b.github[metric] : null;
    if (v != null) sum = (sum || 0) + v;
  });
  return sum;
}

// Tokens per agent over the range: `in` / `out` summed over the measured
// buckets, `state` measured / unknown / not_measured, in stack order.
export function tokenAgents(data) {
  const byAgent = new Map();
  bucketsOf(data).forEach(function (b) {
    const cells = b.tokens || {};
    Object.keys(cells).forEach(function (id) {
      const cell = cells[id] || {};
      const row = byAgent.get(id) || { id: id, state: 'not_measured', tokens_in: null, tokens_out: null, unknown_days: 0 };
      if (cell.state === 'measured') {
        row.state = 'measured';
        row.tokens_in = (row.tokens_in || 0) + (cell.tokens_in || 0);
        row.tokens_out = (row.tokens_out || 0) + (cell.tokens_out || 0);
      } else if (cell.state === 'unknown' && row.state !== 'measured') {
        row.state = 'unknown';
      }
      row.unknown_days += cell.unknown_days || 0;
      byAgent.set(id, row);
    });
  });
  return Array.from(byAgent.values()).sort(function (a, b) { return agentOrder(a.id) - agentOrder(b.id); });
}

// Tokens in and out over every measured agent; null when none was measured.
export function tokenTotals(data) {
  let tin = null;
  let tout = null;
  tokenAgents(data).forEach(function (a) {
    if (a.state !== 'measured') return;
    tin = (tin || 0) + a.tokens_in;
    tout = (tout || 0) + a.tokens_out;
  });
  return { tokens_in: tin, tokens_out: tout };
}

// The four headline tiles. A value is a number or null (unknown); the view
// puts the word beside a null.
export function tiles(data) {
  const t = tokenTotals(data);
  return [
    {
      key: 'lines', label: 'Lines of code', glyph: 'code-xml', source: 'github',
      pair: [githubTotal(data, 'lines_added'), githubTotal(data, 'lines_removed')],
    },
    { key: 'prs', label: 'PRs merged', glyph: 'git-pull-request', source: 'github', pair: [githubTotal(data, 'prs_merged')] },
    {
      key: 'issues', label: 'Issues', glyph: 'circle-check', source: 'github',
      pair: [githubTotal(data, 'issues_opened'), githubTotal(data, 'issues_closed')],
    },
    { key: 'tokens', label: 'Tokens', glyph: 'activity', source: 'tokens', pair: [t.tokens_in, t.tokens_out] },
  ];
}

// -------------------------------------------------------------- series

// One chart point per bucket: `k` the bucket's start, and each series' value
// or null for a bucket that was not read.
export function githubSeries(data, metrics) {
  return bucketsOf(data).map(function (b) {
    const point = { k: b.start };
    metrics.forEach(function (m) {
      point[m] = b.github && typeof b.github[m] === 'number' ? b.github[m] : null;
    });
    return point;
  });
}

// Tokens by agent for one direction ('in' | 'out'); an agent with nothing
// measured in a bucket is null there.
export function tokenSeries(data, direction) {
  const field = direction === 'out' ? 'tokens_out' : 'tokens_in';
  const agents = tokenAgents(data).filter(function (a) { return a.state === 'measured'; });
  const points = bucketsOf(data).map(function (b) {
    const point = { k: b.start };
    agents.forEach(function (a) {
      const cell = (b.tokens || {})[a.id];
      point[a.id] = cell && cell.state === 'measured' ? cell[field] : null;
    });
    return point;
  });
  return { agents: agents.map(function (a) { return agentInfo(a.id); }), points: points };
}

// ----------------------------------------------------------- tables

export const REPO_SORTS = ['lines_added', 'lines_removed', 'prs_merged', 'issues_opened', 'issues_closed'];

// The per-repo rows, the largest first by the chosen column.
export function repoRows(data, sortKey) {
  const key = REPO_SORTS.indexOf(sortKey) === -1 ? 'prs_merged' : sortKey;
  const rows = data && Array.isArray(data.repos) ? data.repos.slice() : [];
  return rows.sort(function (a, b) {
    return (b[key] || 0) - (a[key] || 0) || String(a.repo).localeCompare(String(b.repo));
  });
}

// Each measured agent's share of all measured tokens (in + out), 0..1.
export function agentShares(data) {
  const agents = tokenAgents(data);
  const total = agents.reduce(function (sum, a) {
    return a.state === 'measured' ? sum + a.tokens_in + a.tokens_out : sum;
  }, 0);
  return agents.map(function (a) {
    const used = a.state === 'measured' ? a.tokens_in + a.tokens_out : null;
    return Object.assign({}, a, { info: agentInfo(a.id), share: used != null && total ? used / total : null });
  });
}

// "3 repos" over the rows the range has.
export function reposCaption(data) {
  const n = data && Array.isArray(data.repos) ? data.repos.length : 0;
  const unread = data && data.github && Array.isArray(data.github.unknown_repos) ? data.github.unknown_repos.length : 0;
  return n + (n === 1 ? ' repo' : ' repos') + (unread ? ' · ' + unread + ' not read yet' : '');
}
