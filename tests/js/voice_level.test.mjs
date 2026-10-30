// Unit pin for the dictation input-level watch. Runs under plain Node with a
// fake Web Audio: a silent stream must raise onSilent within the window and a
// tone must never raise it. Invoked by tests/unit/static_ui/test_voice_level.py;
// the same behaviour in a real browser is tests/e2e/compose/test_voice_silent_input.py.

import assert from 'node:assert/strict';

let ctxState = 'running';
let level = 0;
let closed = 0;

class FakeAnalyser {
  constructor() { this.fftSize = 0; }
  getFloatTimeDomainData(buf) { buf.fill(level); }
}
class FakeContext {
  constructor() { this.state = ctxState; }
  resume() {}
  close() { closed += 1; }
  createAnalyser() { return new FakeAnalyser(); }
  createMediaStreamSource() { return { connect() {} }; }
}
globalThis.AudioContext = FakeContext;

const { inputLabel, watchInputLevel } = await import(
  '../../app/webapp/static/voice-level.js'
);

const FAST = { windowMs: 80, pollMs: 10 };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function watch(opts) {
  const seen = { silent: 0, signal: 0 };
  const stop = watchInputLevel({}, {
    onSilent: () => { seen.silent += 1; },
    onSignal: () => { seen.signal += 1; },
  }, opts || FAST);
  return { seen, stop };
}

// A silent input is reported once, only after the window has passed.
level = 0;
let w = watch();
await sleep(40);
assert.equal(w.seen.silent, 0, 'no verdict before the window ends');
await sleep(120);
assert.equal(w.seen.silent, 1, 'silent input reported within the window');
assert.equal(w.seen.signal, 0);
// Sound arriving after the verdict clears it, and ends the watch.
level = 0.2;
await sleep(40);
assert.equal(w.seen.signal, 1, 'sound after a silent verdict clears the banner');
assert.equal(closed, 1, 'the audio context is released once sound is heard');
w.stop();
assert.equal(closed, 1, 'stop after the watch ended closes nothing twice');

// A tone is never reported silent.
level = 0.2;
w = watch();
await sleep(160);
assert.equal(w.seen.silent, 0, 'a tone never raises the banner');
assert.equal(w.seen.signal, 1);
w.stop();

// A level just under the floor still counts as silent; just over does not.
level = 0.0015;
w = watch();
await sleep(160);
assert.equal(w.seen.silent, 1, 'a level under the floor is silent');
w.stop();
level = 0.0025;
w = watch();
await sleep(60);
assert.equal(w.seen.silent, 0, 'a level over the floor is not silent');
w.stop();

// stop() ends the watch: no verdict after it.
level = 0;
w = watch();
w.stop();
await sleep(160);
assert.equal(w.seen.silent, 0, 'a stopped watch reports nothing');

// A context that never runs is unmeasured, not silent.
ctxState = 'suspended';
level = 0;
w = watch();
await sleep(160);
assert.equal(w.seen.silent, 0, 'a suspended context is not reported as silent');
w.stop();
ctxState = 'running';

// No Web Audio at all: unmeasured, and stop() is still callable.
delete globalThis.AudioContext;
w = watch();
await sleep(40);
assert.equal(w.seen.silent, 0);
w.stop();
globalThis.AudioContext = FakeContext;

// The input name: the first audio track's label, '' when withheld.
assert.equal(inputLabel({ getAudioTracks: () => [{ label: 'AirPods' }] }), 'AirPods');
assert.equal(inputLabel({ getAudioTracks: () => [{ label: '' }] }), '');
assert.equal(inputLabel({ getAudioTracks: () => [] }), '');
assert.equal(inputLabel({}), '');
assert.equal(inputLabel(null), '');

console.log('OK');
