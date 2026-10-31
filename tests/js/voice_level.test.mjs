// Unit pin for the dictation input name. Runs under plain Node; invoked by
// tests/unit/static_ui/test_voice_level.py. The level strip and the silent-input
// verdict are the vendored rec-wave component's, pinned in a real browser by
// tests/e2e/compose/test_voice_silent_input.py.

import assert from 'node:assert/strict';

const { inputLabel } = await import('../../app/webapp/static/voice-level.js');

// The input name: the first audio track's label, '' when withheld.
assert.equal(inputLabel({ getAudioTracks: () => [{ label: 'AirPods' }] }), 'AirPods');
assert.equal(inputLabel({ getAudioTracks: () => [{ label: '' }] }), '');
assert.equal(inputLabel({ getAudioTracks: () => [] }), '');
assert.equal(inputLabel({}), '');
assert.equal(inputLabel(null), '');

console.log('OK');
