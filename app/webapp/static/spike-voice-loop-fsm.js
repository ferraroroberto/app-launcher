/* Spike #246 — pure state machine for the hands-free voice loop.
 *
 * THROWAWAY. This module (and its sibling spike-voice-loop.{html,js} +
 * tests/e2e/test_spike_voice_loop.py + docs/voice-loop-spike.md) is a
 * de-risking spike: prove whether a continuous, no-tap voice conversation
 * loop is viable inside the iOS Safari PWA, or whether it has to fold into
 * the native iOS hub (#40). Delete the whole set once the gate is answered.
 *
 * The loop is one turn repeated forever, hands-free after a single Start tap:
 *
 *   listening → (speech) → capturing → (silence) → transcribing
 *     → (transcript) → narrating → (narration ends) → listening …
 *
 * with two escapes: a **barge-in** (user speaks while narration plays →
 * narration stops, jump back to listening) and a **forced gesture** (iOS
 * refuses to re-arm the mic or play audio without a fresh user tap → pause
 * until the user taps Resume). That forced-gesture count is the whole point
 * of the spike: a loop that needs a tap every turn is not hands-free.
 *
 * This file is PURE — no DOM, no audio, no network, no timers. It owns only
 * the state transitions, the per-turn latency arithmetic, and the running
 * tallies, all driven by events the controller (spike-voice-loop.js) feeds in
 * with injected timestamps. That keeps the loop *logic* deterministically
 * unit-testable (tests/e2e/test_spike_voice_loop.py drives it through the
 * window.__voiceloop seam with fakes) — the iOS audio-gating questions it
 * exists to answer can only be measured on a real device, but the wiring that
 * sequences the turns is proven here, off-device.
 *
 * Each event returns an INTENT string telling the controller what to do next
 * (arm the mic, start a capture, transcribe, narrate, stop, pause…). The FSM
 * decides *what* should happen; the controller performs the audio/network side
 * effect and reports back with the next event. One-way data flow, no I/O here.
 */

export const STATE = Object.freeze({
  IDLE: 'idle',                 // before Start / after Stop
  LISTENING: 'listening',       // mic armed, waiting for speech
  CAPTURING: 'capturing',       // speech detected, recording the take
  TRANSCRIBING: 'transcribing', // take sent to whisper, awaiting transcript
  NARRATING: 'narrating',       // playing canned board-state narration
  PAUSED: 'paused',             // iOS demanded a fresh gesture; awaiting Resume
});

export const INTENT = Object.freeze({
  NONE: 'none',
  ARM_LISTEN: 'arm-listen',         // (re-)open the mic / VAD for a new turn
  START_CAPTURE: 'start-capture',   // begin recording the take
  TRANSCRIBE: 'transcribe',         // POST the take to whisper
  NARRATE: 'narrate',               // pick a line + start TTS playback
  STOP_NARRATION: 'stop-narration', // barge-in: cut playback, then re-arm
  PAUSE: 'pause',                   // park until the user taps Resume
  STOP_ALL: 'stop-all',             // halt: tear everything down
});

// A turn that needs a fresh tap is the failure signal; one that loops on its
// own is the success signal. `forcedGestures` vs `turns` is the headline ratio.
function freshMetrics() {
  return {
    turns: 0,            // narration→re-arm cycles completed with no tap
    forcedGestures: 0,   // times iOS demanded a fresh tap to continue
    bargeIns: 0,         // narration interrupted by the user speaking
    transcripts: 0,      // takes that came back from whisper
    samples: [],         // per-turn { turn, speechToTranscriptMs, transcriptToAudioMs }
    lastSpeechToTranscriptMs: null,
    lastTranscriptToAudioMs: null,
  };
}

export class VoiceLoop {
  constructor() {
    this.state = STATE.IDLE;
    this.metrics = freshMetrics();
    this.lastTranscript = '';
    // Timestamps for the current in-flight turn (ms, from the injected clock).
    this._speechEndAt = null;
    this._transcriptAt = null;
    // Where to return after a forced-gesture pause.
    this._resumeIntent = INTENT.ARM_LISTEN;
  }

  reset() {
    this.state = STATE.IDLE;
    this.metrics = freshMetrics();
    this.lastTranscript = '';
    this._speechEndAt = null;
    this._transcriptAt = null;
    this._resumeIntent = INTENT.ARM_LISTEN;
  }

  // Start the loop from idle. → listening (controller arms the mic).
  begin() {
    if (this.state !== STATE.IDLE && this.state !== STATE.PAUSED) return INTENT.NONE;
    this.state = STATE.LISTENING;
    return INTENT.ARM_LISTEN;
  }

  // VAD heard speech begin while listening. → capturing.
  onSpeechStart() {
    if (this.state !== STATE.LISTENING) return INTENT.NONE;
    this.state = STATE.CAPTURING;
    this._speechEndAt = null;
    return INTENT.START_CAPTURE;
  }

  // VAD heard the trailing silence that ends the take. → transcribing.
  onSpeechEnd(now) {
    if (this.state !== STATE.CAPTURING) return INTENT.NONE;
    this.state = STATE.TRANSCRIBING;
    this._speechEndAt = now;
    return INTENT.TRANSCRIBE;
  }

  // Whisper returned the transcript. → narrating (controller starts TTS).
  // Records speech-end → transcript latency (the "speak → understood" cost).
  onTranscript(text, now) {
    if (this.state !== STATE.TRANSCRIBING) return INTENT.NONE;
    this.metrics.transcripts += 1;
    this.lastTranscript = text || '';
    this._transcriptAt = now;
    if (this._speechEndAt != null) {
      this.metrics.lastSpeechToTranscriptMs = now - this._speechEndAt;
    }
    this.state = STATE.NARRATING;
    return INTENT.NARRATE;
  }

  // TTS produced its first audio. Records transcript → first-audio latency
  // (the "understood → talking back" cost). State stays NARRATING.
  onNarrationStart(now) {
    if (this.state !== STATE.NARRATING) return INTENT.NONE;
    if (this._transcriptAt != null) {
      this.metrics.lastTranscriptToAudioMs = now - this._transcriptAt;
    }
    return INTENT.NONE;
  }

  // Narration finished on its own → one full hands-free turn. Re-arm the mic
  // with no tap (the behaviour the spike is testing for).
  onNarrationEnd() {
    if (this.state !== STATE.NARRATING) return INTENT.NONE;
    this.metrics.turns += 1;
    this.metrics.samples.push({
      turn: this.metrics.turns,
      speechToTranscriptMs: this.metrics.lastSpeechToTranscriptMs,
      transcriptToAudioMs: this.metrics.lastTranscriptToAudioMs,
    });
    this.state = STATE.LISTENING;
    return INTENT.ARM_LISTEN;
  }

  // User spoke over the narration → cut it and listen. Counts as a barge-in,
  // not a completed turn.
  onBargeIn() {
    if (this.state !== STATE.NARRATING) return INTENT.NONE;
    this.metrics.bargeIns += 1;
    this.state = STATE.LISTENING;
    return INTENT.STOP_NARRATION;
  }

  // iOS refused to continue without a fresh user gesture (mic re-arm blocked,
  // or audio playback blocked outside a gesture). Park until Resume; remember
  // what we were about to do so Resume can pick it back up.
  onGestureRequired(resumeIntent) {
    if (this.state === STATE.IDLE || this.state === STATE.PAUSED) return INTENT.NONE;
    this._resumeIntent = resumeIntent || INTENT.ARM_LISTEN;
    this.state = STATE.PAUSED;
    return INTENT.PAUSE;
  }

  // The user supplied the demanded tap. Count it (the failure tally) and
  // resume. A loop with forcedGestures climbing alongside turns is a no-go.
  onGestureResumed() {
    if (this.state !== STATE.PAUSED) return INTENT.NONE;
    this.metrics.forcedGestures += 1;
    const intent = this._resumeIntent || INTENT.ARM_LISTEN;
    this.state = intent === INTENT.NARRATE ? STATE.NARRATING : STATE.LISTENING;
    return intent;
  }

  // Stop everything.
  halt() {
    this.state = STATE.IDLE;
    return INTENT.STOP_ALL;
  }
}
