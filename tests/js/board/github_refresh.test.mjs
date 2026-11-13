// Pin for the Board's automatic GitHub refresh gate: due only for a visible,
// foreground Board tab whose cache is older than the age (or never filled),
// with no refresh running and no attempt inside the age. Synthetic data only.
// Invoked by tests/unit/board/test_github_auto_refresh.py.

import assert from 'node:assert/strict';
import { GH_AUTO_REFRESH_AGE_MS, shouldAutoRefreshGithub } from '../../../app/webapp/static/board/github-refresh.js';

const NOW = Date.parse('2026-11-13T12:00:00Z');
const ago = (ms) => new Date(NOW - ms).toISOString();
const base = () => ({
  github: { fetched_at: ago(GH_AUTO_REFRESH_AGE_MS + 1000), error: null },
  now: NOW, hidden: false, tab: 'board', inFlight: false, lastAttemptAt: 0,
});
const due = (over) => shouldAutoRefreshGithub({ ...base(), ...over });

// Age gate.
assert.equal(due({}), true, 'older than the age is due');
assert.equal(due({ github: { fetched_at: ago(GH_AUTO_REFRESH_AGE_MS - 1000) } }), false, 'younger than the age is not');
assert.equal(due({ github: { fetched_at: null } }), true, 'never fetched is due');
assert.equal(due({ github: { fetched_at: 'garbage' } }), true, 'an unparseable stamp is treated as never fetched');
assert.equal(due({ github: null }), false, 'no github section in the payload: nothing to judge');

// Hidden-page and tab gates.
assert.equal(due({ hidden: true }), false, 'a hidden page never refreshes');
assert.equal(due({ tab: 'apps' }), false, 'another tab never refreshes');

// A refresh already running, and a recent attempt (a failing gh must not loop).
assert.equal(due({ inFlight: true }), false);
assert.equal(due({ lastAttemptAt: NOW - 1000 }), false, 'a recent attempt holds the next one off');
assert.equal(due({ lastAttemptAt: NOW - GH_AUTO_REFRESH_AGE_MS - 1 }), true);

// An errored cache keeps its old stamp, so it retries on the same schedule.
assert.equal(due({ github: { fetched_at: ago(GH_AUTO_REFRESH_AGE_MS + 1000), error: 'gh failed' } }), true);

console.log('ok');
