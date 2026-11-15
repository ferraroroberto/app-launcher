// Unit pin for dictation's silence auto-stop detector (voice-autostop.js):
// a pause stops a take only after speech, only once, and judged against the
// room's own level. Plain Node against the real ES module. Invoked by
// tests/unit/static_ui/test_voice_autostop.py.

import assert from 'node:assert/strict';
import './_browser_stubs.mjs';

const { createPauseDetector, PAUSE_TICK_MS } = await import('../../app/webapp/static/voice-autostop.js');

// Feed `ms` of a steady level at the 100 ms tick; returns whether it fired.
function run(detector, clock, rms, ms) {
  let fired = false;
  for (let t = 0; t < ms; t += PAUSE_TICK_MS) {
    clock.now += PAUSE_TICK_MS;
    if (detector.feed(rms, clock.now)) fired = true;
  }
  return fired;
}

// Quiet before any speech never stops the take.
{
  const d = createPauseDetector(2000);
  const clock = { now: 0 };
  assert.equal(run(d, clock, 0.001, 10000), false);
  assert.equal(d.armed, false);
}

// Speech, then quiet: fires once the pause reaches 2 s, not before, and once.
{
  const d = createPauseDetector(2000);
  const clock = { now: 0 };
  run(d, clock, 0.001, 500);
  assert.equal(run(d, clock, 0.2, 1500), false);
  assert.equal(d.armed, true);
  assert.equal(run(d, clock, 0.001, 1500), false, 'a 1.5 s breath keeps recording');
  assert.equal(run(d, clock, 0.2, 500), false);
  assert.equal(run(d, clock, 0.001, 2500), true, 'a 2 s pause stops');
  assert.equal(run(d, clock, 0.2, 500) || run(d, clock, 0.001, 3000), false, 'it fires once');
}

// A cough shorter than the arming window does not arm the stop.
{
  const d = createPauseDetector(2000);
  const clock = { now: 0 };
  run(d, clock, 0.001, 500);
  run(d, clock, 0.3, 200);
  assert.equal(run(d, clock, 0.001, 5000), false);
}

// In a noisy room the floor is the room: its hum is quiet, a voice over it
// is speech, and the pause after the voice stops the take.
{
  const d = createPauseDetector(3000);
  const clock = { now: 0 };
  assert.equal(run(d, clock, 0.03, 2000), false);
  assert.equal(d.armed, false, 'room noise alone is not speech');
  run(d, clock, 0.25, 1000);
  assert.equal(d.armed, true);
  assert.equal(run(d, clock, 0.03, 2500), false);
  assert.equal(run(d, clock, 0.03, 1000), true);
}

// Two minutes of unbroken speech is never taken for the room.
{
  const d = createPauseDetector(2000);
  const clock = { now: 0 };
  run(d, clock, 0.001, 500);
  assert.equal(run(d, clock, 0.1, 120000), false);
  assert.equal(run(d, clock, 0.001, 2500), true);
}

console.log('OK');
