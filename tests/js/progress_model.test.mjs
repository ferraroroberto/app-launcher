// Unit pin for the History sheet's reading of /api/progress: the totals, the
// series, the words a figure carries when it was not measured, and the range
// maths. Runs under plain Node against the real ES module. Invoked by
// tests/unit/progress/test_progress_model_js.py.

import assert from 'node:assert/strict';
import {
  agentInfo, agentShares, bucketLabel, fmtN, githubSeries, githubTotal, halfStatus,
  progressUrl, rangeDays, repoRows, summary, tiles, tokenAgents, tokenSeries, tokenTotals,
} from '../../app/webapp/static/progress/progress-model.js';

const cell = (tin, tout, unknown = 0) => ({ state: 'measured', tokens_in: tin, tokens_out: tout, unknown_days: unknown });
const NOT_MEASURED = { state: 'not_measured', tokens_in: null, tokens_out: null, unknown_days: 0 };
const UNKNOWN = { state: 'unknown', tokens_in: null, tokens_out: null, unknown_days: 1 };

const data = {
  bucket: 'day', from: '2026-11-01', to: '2026-11-03',
  github: { state: 'ok', age_seconds: 240, refreshing: false, unknown_repos: [] },
  tokens: { state: 'ok', age_seconds: 60, refreshing: false },
  buckets: [
    { start: '2026-11-01', end: '2026-11-01',
      github: { lines_added: 100, lines_removed: 40, prs_merged: 2, issues_opened: 3, issues_closed: 1 },
      tokens: { claude: cell(1000, 200), codex: cell(500, 50), grok: NOT_MEASURED } },
    // A bucket the GitHub half could not read: a gap, not a zero.
    { start: '2026-11-02', end: '2026-11-02', github: null,
      tokens: { claude: cell(2000, 300), codex: UNKNOWN, grok: NOT_MEASURED } },
    { start: '2026-11-03', end: '2026-11-03',
      github: { lines_added: 50, lines_removed: 0, prs_merged: 1, issues_opened: 0, issues_closed: 2 },
      tokens: { claude: cell(100, 10, 1), codex: cell(5, 5), grok: NOT_MEASURED } },
  ],
  repos: [
    { repo: 'alpha', state: 'ok', lines_added: 10, lines_removed: 5, prs_merged: 1, issues_opened: 0, issues_closed: 0 },
    { repo: 'beta', state: 'stale', lines_added: 90, lines_removed: 35, prs_merged: 2, issues_opened: 3, issues_closed: 3 },
  ],
};

// GitHub totals sum only the buckets that were read.
assert.equal(githubTotal(data, 'lines_added'), 150);
assert.equal(githubTotal(data, 'issues_closed'), 3);
// ...and are null (unknown), never 0, when none was.
assert.equal(githubTotal({ buckets: [{ github: null }, { github: null }] }, 'prs_merged'), null);
assert.equal(githubTotal({ buckets: [] }, 'prs_merged'), null);
// A real zero stays a zero.
assert.equal(githubTotal({ buckets: [{ github: { prs_merged: 0 } }] }, 'prs_merged'), 0);

// The GitHub chart series keeps the unread bucket as null.
const series = githubSeries(data, ['lines_added', 'prs_merged']);
assert.equal(series.length, 3);
assert.deepEqual(series[1], { k: '2026-11-02', lines_added: null, prs_merged: null });
assert.equal(series[2].lines_added, 50);

// Tokens per agent: measured days summed, an all-unknown agent stays unknown,
// an agent without counts is not measured; stack order is claude, codex, grok.
const agents = tokenAgents(data);
assert.deepEqual(agents.map((a) => a.id), ['claude', 'codex', 'grok']);
assert.equal(agents[0].tokens_in, 3100);
assert.equal(agents[0].tokens_out, 510);
assert.equal(agents[0].unknown_days, 1);
assert.equal(agents[1].tokens_in, 505);
assert.equal(agents[1].unknown_days, 1);
assert.equal(agents[2].state, 'not_measured');
assert.equal(agents[2].tokens_in, null);
assert.deepEqual(tokenTotals(data), { tokens_in: 3605, tokens_out: 565 });
// No agent measured -> null totals, not zero.
assert.deepEqual(tokenTotals({ buckets: [{ tokens: { grok: NOT_MEASURED } }] }), { tokens_in: null, tokens_out: null });
assert.deepEqual(tokenTotals({ buckets: [{ tokens: { claude: UNKNOWN } }] }), { tokens_in: null, tokens_out: null });

// The token chart draws measured agents only; an unmeasured bucket is null.
const tokens = tokenSeries(data, 'in');
assert.deepEqual(tokens.agents.map((a) => a.id), ['claude', 'codex']);
assert.equal(tokens.points[1].claude, 2000);
assert.equal(tokens.points[1].codex, null);
assert.equal(tokenSeries(data, 'out').points[0].claude, 200);

// Shares: of measured tokens only; an unmeasured agent has no share.
const shares = agentShares(data);
assert.ok(Math.abs(shares[0].share + shares[1].share - 1) < 1e-9);
assert.equal(shares[2].share, null);

// The four tiles.
const t = tiles(data);
assert.deepEqual(t.map((x) => x.key), ['lines', 'prs', 'issues', 'tokens']);
assert.deepEqual(t[0].pair, [150, 40]);
assert.deepEqual(t[1].pair, [3]);
assert.deepEqual(t[2].pair, [3, 3]);
assert.deepEqual(t[3].pair, [3605, 565]);

// Repo rows: largest first by the chosen column, a bad key falls back to PRs.
assert.deepEqual(repoRows(data, 'lines_added').map((r) => r.repo), ['beta', 'alpha']);
assert.deepEqual(repoRows(data, 'nonsense').map((r) => r.repo), ['beta', 'alpha']);
assert.deepEqual(repoRows({}, 'prs_merged'), []);

// A half's words.
assert.equal(halfStatus({ state: 'ok' }).word, '');
assert.equal(halfStatus({ state: 'stale' }).readable, true);
assert.equal(halfStatus({ state: 'unavailable' }).readable, false);
assert.equal(halfStatus({ state: 'unavailable' }).word, 'unavailable');
assert.equal(halfStatus({ state: 'unknown' }).word, 'unknown');
assert.equal(halfStatus(null).state, 'unknown');
assert.equal(halfStatus({ state: 'surprise' }).state, 'unknown');
assert.equal(halfStatus({ state: 'ok', age_seconds: 240 }).age, '4 min ago');
assert.equal(summary({ github: { state: 'stale' }, tokens: { state: 'ok' } }).stale, true);
assert.equal(summary({ github: { state: 'ok' }, tokens: { state: 'ok' }, refreshing: true }).refreshing, true);

// Numbers and ages.
assert.equal(fmtN(null), '–');
assert.equal(fmtN(0), '0');
assert.equal(fmtN(999), '999');
assert.equal(fmtN(12345), '12.3k');
assert.equal(fmtN(2_500_000), '2.5M');

// Ranges: the last N days including today; a custom pair must be usable.
const today = new Date(2026, 10, 4);
assert.deepEqual(rangeDays('30', null, today), { from: '2026-10-06', to: '2026-11-04', valid: true });
assert.deepEqual(rangeDays('custom', { from: '2026-10-01', to: '2026-10-31' }, today), { from: '2026-10-01', to: '2026-10-31', valid: true });
assert.equal(rangeDays('custom', { from: '2026-11-02', to: '2026-11-01' }, today).valid, false);
assert.equal(rangeDays('custom', { from: '', to: '' }, today).valid, false);
assert.equal(rangeDays('custom', { from: '2025-01-01', to: '2026-11-01' }, today).valid, false);
assert.equal(rangeDays('custom', { from: '2025-01-01', to: '2026-11-01' }, today).to, '2026-11-04');

assert.equal(progressUrl('week', '2026-10-01', '2026-10-31', false), '/api/progress?bucket=week&from=2026-10-01&to=2026-10-31');
assert.ok(progressUrl('day', '2026-10-01', '2026-10-31', true).endsWith('&refresh=true'));

// Labels.
assert.equal(bucketLabel('2026-11-04', 'day'), '4 Nov');
assert.equal(bucketLabel('2026-11-01', 'month'), 'Nov');

// Agent names: the hub's id, the CLI's alias, and an unknown id.
assert.equal(agentInfo('agy').id, 'antigravity');
assert.equal(agentInfo('claude').name, 'Claude Code');
assert.equal(agentInfo('mystery').name, 'Mystery');

// Each known agent paints its own brand token; the six are all different.
const brandColours = ['claude', 'codex', 'copilot', 'antigravity', 'pi', 'grok'].map((id) => agentInfo(id).color);
brandColours.forEach((c) => assert.match(c, /^--brand-/));
assert.equal(new Set(brandColours).size, 6);

console.log('OK');
