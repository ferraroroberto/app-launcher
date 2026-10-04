// Unit pin for the Telegram sessions' context alert threshold (#1402). Runs
// under plain Node against the real ES module channel-sessions.js's summary
// line and popup read it from. Invoked by tests/test_context_alert.py.

import assert from 'node:assert/strict';
import { CONTEXT_ALERT_PCT, contextAlert } from '../../app/webapp/static/dom-utils.js';

assert.equal(CONTEXT_ALERT_PCT, 50);

// At the threshold counts (>=), one point under does not.
assert.equal(contextAlert(50), true);
assert.equal(contextAlert(49), false);
assert.equal(contextAlert(49.9), false);
assert.equal(contextAlert(100), true);
assert.equal(contextAlert(0), false);

// "Not known" is never an alert: a missing figure is not a full context.
assert.equal(contextAlert(null), false);
assert.equal(contextAlert(undefined), false);
assert.equal(contextAlert(NaN), false);
assert.equal(contextAlert('80'), false);

console.log('OK');
