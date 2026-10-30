/* Live input-level check for a dictation take.
 *
 * iOS routes the mic to a connected Bluetooth headset that may carry no
 * sound, and the recorder then streams silence for minutes while nothing
 * looks wrong. This watches the same MediaStream the recorder uses: an
 * AnalyserNode reads the signal level every _POLL_MS, and `onSilent` fires
 * once the peak level stays under the floor for _WINDOW_MS. `onSignal` fires
 * when sound is heard, including after a silent verdict, so a banner can
 * clear itself.
 *
 * "Could not measure" is its own outcome: with no Web Audio, or a context
 * that will not run, neither callback fires, so an unmeasured take is never
 * reported as silent. */

// A dead route reads exact zeros and a quiet room's noise floor sits well
// above this, so only a route with no signal at all stays under it.
export const SILENCE_RMS = 0.002;
const _WINDOW_MS = 3000;
const _POLL_MS = 100;

function rms(samples) {
  let sum = 0;
  for (let i = 0; i < samples.length; i++) sum += samples[i] * samples[i];
  return Math.sqrt(sum / samples.length);
}

// The name the browser gives the live input ("AirPods", "iPhone Microphone"),
// or '' when it withholds the label.
export function inputLabel(stream) {
  try {
    const tracks = stream && stream.getAudioTracks ? stream.getAudioTracks() : [];
    return (tracks[0] && tracks[0].label) || '';
  } catch (_) {
    return '';
  }
}

// Start watching `stream`. `opts.windowMs` / `opts.pollMs` / `opts.floor` exist
// for the tests. Returns stop(), which is safe to call more than once.
export function watchInputLevel(stream, handlers, opts) {
  const o = opts || {};
  const windowMs = o.windowMs || _WINDOW_MS;
  const pollMs = o.pollMs || _POLL_MS;
  const floor = o.floor || SILENCE_RMS;
  const Ctx = globalThis.AudioContext || globalThis.webkitAudioContext;
  if (!Ctx) {
    console.info('[dictation] input level not measured: no Web Audio');
    return function () {};
  }
  let ctx = null;
  let timer = null;
  let stopped = false;
  function stop() {
    if (stopped) return;
    stopped = true;
    clearInterval(timer);
    if (ctx) {
      try { ctx.close(); } catch (_) { /* best effort */ }
    }
  }
  try {
    ctx = new Ctx();
    // iOS starts a context suspended; a suspended one reads zeros, which
    // would look exactly like a silent route.
    if (ctx.resume) ctx.resume();
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 1024;
    ctx.createMediaStreamSource(stream).connect(analyser);
    const buf = new Float32Array(analyser.fftSize);
    const t0 = Date.now();
    let silent = false;
    timer = setInterval(function () {
      if (ctx.state && ctx.state !== 'running') return;
      analyser.getFloatTimeDomainData(buf);
      if (rms(buf) >= floor) {
        handlers.onSignal();
        stop();
        return;
      }
      if (!silent && Date.now() - t0 >= windowMs) {
        silent = true;
        handlers.onSilent();
      }
    }, pollMs);
  } catch (exc) {
    console.info('[dictation] input level not measured', exc);
    stop();
  }
  return stop;
}
