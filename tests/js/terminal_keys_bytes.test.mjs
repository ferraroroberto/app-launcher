// Unit pin for the keys-popover byte table (issue #986). Runs under plain
// Node — no browser — against the pure data module the popover imports, so
// the WS-`input` byte mapping is verified without booting the webapp or
// Playwright. Invoked by tests/test_terminal_keys_bytes.py.

import assert from 'node:assert/strict';
import {
  KEY_BYTES,
  SHIFT_KEY_BYTES,
  CTRL_KEY_BYTES,
} from '../../app/webapp/static/terminal-keys-bytes.js';

assert.deepEqual(KEY_BYTES, {
  up: '\x1b[A', down: '\x1b[B', right: '\x1b[C', left: '\x1b[D',
  enter: '\r', esc: '\x1b', tab: '\t',
});

assert.deepEqual(SHIFT_KEY_BYTES, {
  tab: '\x1b[Z',
  up: '\x1b[1;2A', down: '\x1b[1;2B', right: '\x1b[1;2C', left: '\x1b[1;2D',
});

// Issue #986: Ctrl+C (interrupt), Ctrl+U (clear line), Ctrl+X.
assert.deepEqual(CTRL_KEY_BYTES, { c: '\x03', u: '\x15', x: '\x18' });

console.log('terminal-keys-bytes: OK');
