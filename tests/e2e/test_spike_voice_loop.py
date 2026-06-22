"""Loop-logic proof for the hands-free voice-loop spike (#246).

THROWAWAY by design, but RETAINED FOR NOW (#258) as the live reference until the
kanban/board view and the orchestrator (#245) voice mode land.

This pins the *wiring* of the continuous voice loop — the turn sequence, the
barge-in escape, the forced-gesture escape, and the per-turn latency
arithmetic — so the spike's prototype provably cycles correctly off-device.

What it does NOT (and cannot) prove is the actual viability question the spike
exists to answer: whether iOS Safari's per-gesture audio/mic gating lets a real
device run this loop hands-free. That is measured on the phone via the
prototype's on-screen instruments (docs/voice-loop-spike.md). Headless WebKit
does not enforce iOS autoplay/mic policy, so a green run here means "the state
machine sequences turns correctly", not "iOS allows it".

The loop's state machine (spike-voice-loop-fsm.js) is pure — no audio, no
network, no timers — and is exposed on ``window.__voiceloop`` by the prototype
page. The test drives it through that seam with synthetic events + injected
timestamps. Delete with the rest of the spike-voice-loop.* set once #245 + the
board view have shipped (see docs/voice-loop-spike.md for the retention decision).
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page

pytestmark = pytest.mark.smoke


def _open_spike(page: Page, base_url: str) -> None:
    page.goto(f"{base_url}/spike/voice-loop", wait_until="domcontentloaded")
    page.wait_for_function("() => !!(window.__voiceloop && window.__voiceloop.VoiceLoop)")


def test_two_turns_run_hands_free(authed_page: Page, base_url: str) -> None:
    """Two full turns (listen → transcribe → narrate → re-arm) complete with
    zero forced gestures, and each event yields the expected intent."""
    _open_spike(authed_page, base_url)
    result = authed_page.evaluate(
        """() => {
          const { VoiceLoop, INTENT, STATE } = window.__voiceloop;
          const loop = new VoiceLoop();
          const intents = [];
          const one = (t0) => {
            intents.push(loop.onSpeechStart());
            intents.push(loop.onSpeechEnd(t0 + 100));     // speech ends at +100
            intents.push(loop.onTranscript('hello fleet', t0 + 400)); // text at +400
            loop.onNarrationStart(t0 + 700);              // first audio at +700
            intents.push(loop.onNarrationEnd());
          };
          intents.push(loop.begin());
          one(1000);
          one(2000);
          return {
            intents,
            turns: loop.metrics.turns,
            forced: loop.metrics.forcedGestures,
            state: loop.state,
            listening: STATE.LISTENING,
            armIntent: INTENT.ARM_LISTEN,
            samples: loop.metrics.samples,
          };
        }"""
    )
    assert result["turns"] == 2, result
    assert result["forced"] == 0, result
    assert result["state"] == result["listening"], result
    # begin + (start-capture, transcribe, narrate, arm-listen) × 2.
    assert result["intents"] == [
        result["armIntent"],
        "start-capture", "transcribe", "narrate", result["armIntent"],
        "start-capture", "transcribe", "narrate", result["armIntent"],
    ], result["intents"]


def test_latency_arithmetic(authed_page: Page, base_url: str) -> None:
    """Speech-end→transcript and transcript→first-audio latencies are the
    differences of the injected timestamps."""
    _open_spike(authed_page, base_url)
    m = authed_page.evaluate(
        """() => {
          const { VoiceLoop } = window.__voiceloop;
          const loop = new VoiceLoop();
          loop.begin();
          loop.onSpeechStart();
          loop.onSpeechEnd(5000);
          loop.onTranscript('go', 5350);   // +350 ms to understand
          loop.onNarrationStart(5600);      // +250 ms to first audio
          loop.onNarrationEnd();
          return {
            sttt: loop.metrics.lastSpeechToTranscriptMs,
            tta: loop.metrics.lastTranscriptToAudioMs,
            sample: loop.metrics.samples[0],
          };
        }"""
    )
    assert m["sttt"] == 350, m
    assert m["tta"] == 250, m
    assert m["sample"]["speechToTranscriptMs"] == 350, m
    assert m["sample"]["transcriptToAudioMs"] == 250, m


def test_barge_in_interrupts_narration(authed_page: Page, base_url: str) -> None:
    """Speaking over the narration cuts it (STOP_NARRATION), counts a barge-in,
    drops back to listening, and does NOT count as a completed turn."""
    _open_spike(authed_page, base_url)
    r = authed_page.evaluate(
        """() => {
          const { VoiceLoop, INTENT, STATE } = window.__voiceloop;
          const loop = new VoiceLoop();
          loop.begin();
          loop.onSpeechStart();
          loop.onSpeechEnd(100);
          loop.onTranscript('x', 200);     // → narrating
          const bargeIntent = loop.onBargeIn();
          return {
            bargeIntent, stop: INTENT.STOP_NARRATION,
            barge: loop.metrics.bargeIns, turns: loop.metrics.turns,
            state: loop.state, listening: STATE.LISTENING,
          };
        }"""
    )
    assert r["bargeIntent"] == r["stop"], r
    assert r["barge"] == 1, r
    assert r["turns"] == 0, r
    assert r["state"] == r["listening"], r


def test_forced_gesture_pauses_then_resumes(authed_page: Page, base_url: str) -> None:
    """A forced gesture parks the loop (PAUSE), and Resume counts the forced
    tap and returns to the intent it was about to run."""
    _open_spike(authed_page, base_url)
    r = authed_page.evaluate(
        """() => {
          const { VoiceLoop, INTENT, STATE } = window.__voiceloop;
          const loop = new VoiceLoop();
          loop.begin();
          loop.onSpeechStart();
          loop.onSpeechEnd(100);
          loop.onTranscript('x', 200);     // → narrating
          const pause = loop.onGestureRequired(INTENT.NARRATE);
          const pausedState = loop.state;
          const resume = loop.onGestureResumed();
          return {
            pause, pauseWant: INTENT.PAUSE,
            paused: pausedState, pausedState: STATE.PAUSED,
            resume, resumeWant: INTENT.NARRATE,
            forced: loop.metrics.forcedGestures,
            state: loop.state, narrating: STATE.NARRATING,
          };
        }"""
    )
    assert r["pause"] == r["pauseWant"], r
    assert r["paused"] == r["pausedState"], r
    assert r["resume"] == r["resumeWant"], r
    assert r["forced"] == 1, r
    assert r["state"] == r["narrating"], r


def test_out_of_order_events_are_inert(authed_page: Page, base_url: str) -> None:
    """Events fired in the wrong state return NONE and don't corrupt state —
    the controller's real audio callbacks can race, so the FSM must be robust."""
    _open_spike(authed_page, base_url)
    r = authed_page.evaluate(
        """() => {
          const { VoiceLoop, INTENT, STATE } = window.__voiceloop;
          const loop = new VoiceLoop();
          // Nothing started: speech-end / transcript / narration-end are inert.
          const a = loop.onSpeechEnd(1);
          const b = loop.onTranscript('x', 2);
          const c = loop.onNarrationEnd();
          return {
            a, b, c, none: INTENT.NONE,
            state: loop.state, idle: STATE.IDLE,
            turns: loop.metrics.turns,
          };
        }"""
    )
    assert r["a"] == r["none"] and r["b"] == r["none"] and r["c"] == r["none"], r
    assert r["state"] == r["idle"], r
    assert r["turns"] == 0, r
