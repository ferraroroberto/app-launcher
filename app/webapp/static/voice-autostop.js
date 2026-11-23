/* Dictation's silence auto-stop: end a take once the speaker has paused.
 *
 * Settings > Voice sets `dictation_auto_stop_seconds` (0 = off). While a take
 * records, the level of the stream the recorder already holds is read ten
 * times a second; once speech has been heard and then the input stays quiet
 * for that many seconds, the take stops as if the mic had been tapped. The
 * transcript still lands in the textarea for review, so the one tap left is
 * Send.
 *
 * Quiet is judged against the room, not a fixed level: a phone's input sits
 * well above zero in a noisy room, and the rec-wave silence floor only tells a
 * dead route from a live one. The floor follows the quietest recent level,
 * dropping at once and rising slowly, and speech is anything a few times above
 * it. Before any speech nothing stops: a take that never hears a voice is the
 * mic-notice's no-sound warning, not a pause.
 *
 * `createPauseDetector` is pure (no DOM, no timers) so plain Node can pin it;
 * `watchForPause` wires it to a level source and an interval.
 */

import { createLevelSource } from './_vendored/rec-wave/rec-wave.js';

export const PAUSE_TICK_MS = 100;
// Per-tick share of the gap the floor climbs while the level sits above it:
// two minutes of unbroken sound lift it under a third of the way, short of the
// SPEECH_OVER_FLOOR line, so a long sentence is never taken for the room. The
// gaps between words pull it straight back down.
const FLOOR_RISE = 0.0003;
// Speech is this many times the room's floor ...
const SPEECH_OVER_FLOOR = 3;
// ... and never quieter than this, so a near-silent room's hiss is not speech.
const SPEECH_MIN_RMS = 0.01;
// Speech heard for at least this long arms the stop; a cough or a tap on the
// phone does not.
const ARM_SPEECH_MS = 300;

/**
 * A pause detector for one take. `feed(rms, now)` takes each level reading
 * with its time in ms and returns true exactly once: on the reading that
 * completes `pauseMs` of quiet after armed speech.
 *
 * @param {number} pauseMs
 * @returns {{feed(rms: number, now: number): boolean, readonly armed: boolean}}
 */
export function createPauseDetector(pauseMs) {
  let floor = Infinity;
  let speechMs = 0;
  let lastTick = null;
  let lastSpeech = null;
  let fired = false;
  return {
    get armed() { return speechMs >= ARM_SPEECH_MS; },
    feed(rms, now) {
      if (fired) return false;
      const step = lastTick === null ? 0 : now - lastTick;
      lastTick = now;
      // Each reading is already a 100 ms window's RMS, so it is used as is: a
      // smoothed level would lag the end of speech by most of a second.
      floor = rms < floor ? rms : floor + (rms - floor) * FLOOR_RISE;
      const speaking = rms > Math.max(SPEECH_MIN_RMS, floor * SPEECH_OVER_FLOOR);
      if (speaking) {
        speechMs += step;
        lastSpeech = now;
        return false;
      }
      if (!this.armed || now - lastSpeech < pauseMs) return false;
      fired = true;
      return true;
    },
  };
}

/**
 * Watch `stream` for a pause and call `onPause` once when it comes. Returns
 * null when the level cannot be measured (no Web Audio): the take then runs
 * until it is tapped, as with the setting off. `source` is the reader the
 * caller may share with the mic notice, so a take opens one AudioContext.
 *
 * @param {MediaStream} stream
 * @param {number} pauseMs
 * @param {() => void} onPause
 * @returns {{source: object, stop(): void}|null}
 */
export function watchForPause(stream, pauseMs, onPause) {
  const source = createLevelSource(stream);
  if (!source) return null;
  const detector = createPauseDetector(pauseMs);
  // request-budget-exempt: samples the microphone level in the page; it never calls the server.
  const timer = setInterval(function () {
    const reading = source.read(1);
    // A context not running yet measures nothing; never count it as quiet.
    if (reading && detector.feed(reading.rms, Date.now())) {
      clearInterval(timer);
      onPause();
    }
  }, PAUSE_TICK_MS);
  return {
    source,
    stop() {
      clearInterval(timer);
      source.close();
    },
  };
}
