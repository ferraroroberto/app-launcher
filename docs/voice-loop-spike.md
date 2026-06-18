# Spike #246 — hands-free two-way voice loop in the iOS PWA: viability findings

**Status: device run complete (all four questions measured). ✅ go for screen-on; ⚠️ screen-lock survives better than expected, with one tail-truncation caveat.** This is a de-risking spike, not a build commitment. It answers one gate question — *can a continuous, eyes-free, no-tap voice conversation loop run inside the iOS Safari PWA, or must it fold into the native iOS hub (#40)?*

**Headline result (2026-06-17, real iPhone over the tunnel, screen on):** the loop ran **6 turns hands-free with 0 forced taps**, barge-in worked, and time-to-first-audio was **16 ms** (hub Orpheus TTS). The single Start gesture's blessing on one long-lived `AudioContext` + one reused mic stream **survives turn after turn** — the core bet held. That is a **go for the screen-on / foreground case.**

**Screen-lock result (2026-06-18, real iPhone):** better than the documented expectation. Locking the screen mid-narration leaves the `AudioContext` **`running`** and the mic track **`live` (muted)**; on unlock the narration **resumes and the loop continues with no tap** — the run logged **2 turns, 0 forced taps with the screen locked mid-turn each time**. The one defect: a lock *during narration* **truncates the tail by roughly the locked duration** (~2–3 s reported). Cause: the `AudioContext` clock keeps advancing while output is suspended, so the PCM buffers scheduled for the locked interval are "played in the past" (skipped) on resume. This is a **bug in the shipped read-aloud playback path** (`terminal-readback.js` `pumpPcmStream`, #206), not spike-specific — tracked separately as **#248** (see "Tail truncation" below). Net: short locks are survivable and recover hands-free; the driving end-state (screen off for long stretches, app fully backgrounded) is still the case the native hub (#40) is the safe home for.

## The end-state this de-risks

A hands-free, two-way voice conversation with the fleet: speak a goal to the orchestrator (#245) without touching the screen; it narrates back board-level state ("three working; photo-ocr needs you on the chunk-merge question; the app-launcher PR is green and waiting"); you answer by voice; a waiting worker's reply routes back to its PTY — all eyes-free, e.g. while driving.

**~80% already ships.** Voice-in per session (whisper `:8090` dictation with live streamed partials, #165/#168) and voice-out per session (read-the-last-reply-aloud with colour-block detection, Orpheus hub-TTS streaming, and summarize-and-read driving mode, #190/#197/#203/#206/#210) both work today in the Code tab. A two-way voice exchange with a *single* session is already real. What is **not** proven is the genuinely new part this spike targets: a **continuous, no-tap loop** — re-arming the mic across turns, playing narration back-to-back, barge-in, and surviving screen-lock — inside a PWA that gates audio and mic behind per-action user gestures.

## The four viability questions

1. **Mic re-arm** — can the mic stay open / re-arm across turns with no per-turn tap?
2. **Back-to-back playback** — can synthesized narration play turn after turn without a fresh user gesture each time?
3. **Barge-in** — does "user speaks → narration stops" work?
4. **Screen-lock / backgrounding** — does the loop survive the screen locking (the driving case) well enough to be useful?

## What the prototype is

A throwaway, instrumented loop served at **`/spike/voice-loop`** (files: `app/webapp/static/spike-voice-loop.html` + `spike-voice-loop.js` + `spike-voice-loop-fsm.js`, the route in `app/webapp/routers/misc.py`, the off-device test `tests/e2e/test_spike_voice_loop.py`, and this doc). Delete the set once the gate is answered.

Per the spike's chosen shape, **the narration is a pure mock** — a rotating canned board-state line — so the iOS audio-gating questions are isolated from any orchestrator (#245) latency. Only the "brain" is stubbed; the audio path is real:

- **Listen** — one `getUserMedia` stream, granted once, kept alive for the whole session. A Web Audio `AnalyserNode` does energy-based VAD (no wake-word API exists in Safari — see below); on detected speech-end the take is POSTed single-shot to the real `/api/transcribe` (whisper `:8090`). Re-using the one granted stream every turn is the whole no-tap bet.
- **Narrate** — the canned line is played through the shipped hub-TTS path (`terminal-readback.js`: `prepareHub` + `speakHubInto`, headerless PCM over Web Audio), or the Web Speech fallback when the hub is unreachable.
- **Barge-in** — during narration the analyser stays open; sustained voice cuts playback and drops back to listen.
- **Re-arm** — narration end auto-returns to listen, no tap.
- **Forced gesture** — when iOS leaves the `AudioContext` suspended outside a gesture (it can't sound), the loop parks and shows **Resume**; each Resume increments the **Forced taps** counter. *That counter is the headline result: a turn that re-arms and narrates on its own is hands-free; a turn that needs a tap is not.*

Auth reuses the production plumbing exactly — bearer token (`api.js`) + passkey terminal token (`webauthn.js`), the same pair `terminal.js` sends to `/api/transcribe` and `/api/tts/speak`.

### On-screen instruments (what you read off the phone)

| Instrument | Answers |
| --- | --- |
| **Hands-free turns** | how many full turns ran with no tap |
| **Forced taps** | how many times iOS demanded a fresh gesture (the failure signal) |
| **Barge-ins** | question 3 |
| **Speak → understood** (ms) | VAD speech-end → whisper transcript |
| **Understood → talking** (ms) | transcript → first narration audio |
| **Event & lifecycle log** | per-turn trace + `visibilitychange` / `freeze` / `resume` / `pagehide` events with `AudioContext` state + mic-track readyState at each — question 4 |

## Documented iOS Safari PWA constraints (the grounding, not guesses)

These are the *known* WebKit/iOS behaviours that frame the device run. They explain why the result is expected to split by scenario (screen-on foreground vs. screen-locked driving).

- **Audio is gesture-gated.** `AudioContext.resume()`, `<audio>.play()`, and `speechSynthesis.speak()` only produce sound when initiated within a user-activation. A context resumed *outside* a gesture stays `suspended`. The prototype detects exactly this (`ctx.state !== 'running'`) and counts it as a forced tap.
- **User activation is transient (~5 s).** `navigator.userActivation.isActive` expires a few seconds after the tap. So the **first** narration — which arrives only after the mic→whisper round-trip, often >5 s after the Start tap — may already be too late to bless a *new* context. The prototype's bet is that **one long-lived `AudioContext`, blessed once at Start and kept `running`, can schedule new playback nodes across turns without re-activation.** Whether iOS honours that across the loop is precisely what turns 2+ measure.
- **No wake-word / no continuous speech recognition.** `webkitSpeechRecognition` is **not** available on iOS Safari (only `speechSynthesis` is). There is no always-on hotword API in a PWA. Continuous listening therefore *must* be DIY VAD over `getUserMedia` — which is what the prototype does. There is no lower-power substitute available to web code.
- **MediaRecorder is mp4/AAC only on iOS.** No webm/opus. The prototype's MIME ladder already falls back to `audio/mp4`; the voice-transcriber sniffs the real container, so a truthful label is all that matters.
- **Backgrounding / screen-lock suspends the page.** On lock or app-switch iOS suspends the `AudioContext`, throttles or halts timers, and may `freeze` (Page Lifecycle API) or discard the page. Web Audio playback generally stops when backgrounded; **mic capture in the background is heavily restricted**. True background audio + capture is a native entitlement, not a web capability. This is the weakest area for a PWA and the strongest pull toward #40.
- **Standalone PWA vs. Safari tab.** Adding to the home screen (standalone) does not relax the audio/mic gates; historically standalone PWAs purge state *more* aggressively on backgrounding, not less.

### Scenario split (foreground confirmed; driving still expected to need #40)

The constraints predicted a split, and the foreground half is now confirmed on-device:

- **Screen-on, foreground (phone in a mount, awake):** **viable — confirmed.** One long-lived running `AudioContext` + one reused mic stream survive across turns without fresh activation: 6 turns, 0 forced taps on a real iPhone over the tunnel. This is the **go** case.
- **Short screen-lock mid-turn:** **survives — measured, better than expected.** The `AudioContext` stays `running`, the mic track stays `live` (muted) through the lock, and on unlock narration resumes and the loop re-arms **with no tap** (2 turns, 0 forced taps with a lock mid-turn each time). One defect: a lock *during narration* truncates the tail by ~the locked duration (see "Tail truncation"). So a brief glance-away / pocket moment does not break the loop.
- **Screen-locked / true eyes-free driving (screen off for long stretches):** *still the native hub's territory.* A short lock survives, but the documented constraints (mic muted while backgrounded → no capture; long-background freeze/discard) mean a sustained screen-off driving loop is not something a PWA can guarantee. This remains the case for **#40 (native iOS hub)**.

So the answer's shape is **go for screen-on (incl. brief locks), needs-#40 for sustained screen-off / driving** — the screen-lock leg is now measured.

## Off-device loop-logic proof (what is already green)

`tests/e2e/test_spike_voice_loop.py` drives the pure state machine (`spike-voice-loop-fsm.js`, exposed via `window.__voiceloop`) headless in Chromium + WebKit and pins:

- two full turns complete with zero forced gestures, each event yielding the right intent (listen → capture → transcribe → narrate → re-arm);
- the per-turn latency arithmetic (speech-end→transcript, transcript→first-audio) from injected timestamps;
- barge-in cuts narration, counts a barge-in, and is **not** a completed turn;
- a forced gesture parks the loop and Resume counts the tap and resumes;
- out-of-order events are inert (the controller's real audio callbacks can race).

This proves the loop **sequences turns correctly**. Headless WebKit does **not** enforce iOS autoplay/mic policy, so it says nothing about the four device questions — those need the phone.

## Device run (the part that needs the iPhone)

**How to run.** Open the **🎙️ Voice loop spike (#246)** link in the launcher's footer (it bakes in the bearer token so the page-load passes the gate over the tunnel), or go straight to `https://<host>.<tailnet>.ts.net:8445/spike/voice-loop?token=<token>`. For the truest test, **Add to Home Screen** and launch the PWA from there. Tap **Start**, grant the mic + (if prompted) the passkey, then speak a short phrase and pause. Watch the loop cycle; let it run several turns; then **lock the screen** mid-loop and unlock to read the lifecycle log. The **✕ Close** button stops the loop (releasing the mic) and returns to the launcher.

**Device results (real iPhone, over the tunnel):**

| # | Question | Measure | Result |
| --- | --- | --- | --- |
| 1 | Mic re-arm | turns before a forced tap (foreground) | ✅ **6 turns, 0 forced taps** — one reused stream re-armed every turn with no tap |
| 2 | Back-to-back playback | narration on turns 2…N without a tap? | ✅ **yes, 0 forced taps over 6 turns** — one blessed `AudioContext` reused across turns |
| 3 | Barge-in | speaking over narration stops it? | ✅ **works** — 1 barge-in observed, narration cut, dropped back to listen |
| 4 | Screen-lock | audio + mic survive lock/unlock? | ✅ **survives (short lock)** — `ctx=running`, mic `live`(muted) through the lock; on unlock narration resumes + loop re-arms **with no tap** (2 turns, 0 forced taps with a lock mid-turn). ⚠️ a lock *during narration* truncates the tail by ~the locked duration (see below) |
| — | Latency | speak→understood / understood→talking | **~900–1440 ms** (whisper round-trip dominates) / **16–17 ms** (hub TTS first audio) |

The speak→understood latency is essentially the whisper transcription round-trip; the ~16 ms understood→talking confirms the hub-TTS Web Audio streaming path starts near-instantly.

### Tail truncation on mid-narration screen-lock

Locking the screen *while narration is playing* drops roughly the locked-interval's worth of audio from the playback, so the tail is cut by ~2–3 s. The `AudioContext` clock (`currentTime`) keeps advancing while the screen is locked, but audio output is suspended; `pumpPcmStream` (`terminal-readback.js`, #206) schedules each PCM buffer on an absolute `ctx.currentTime` timeline, so on unlock the buffers whose start times elapsed during the lock are "started in the past" and skipped, and the last-buffer `onended` fires early. This affects the **shipped** read-aloud feature too (lock the screen mid-🔊-read and the tail truncates), so it is tracked as its own bug (**#248**), not patched into this throwaway. A production voice mode would need playback that pauses/reschedules on `visibilitychange` rather than scheduling blindly on the wall-clock timeline.

**Recommendation: GO for a screen-on "conversation mode" (brief screen-locks survive); needs-#40 for sustained screen-off / eyes-free driving.** The core bet held on every leg measured.

- **Go (screen-on, incl. brief locks).** A hands-free conversation loop is viable in the PWA today. v1 follow-ups (each its own issue): wire the loop to the real orchestrator (#245) PTY in place of the mock narration; drive narration from summarized board state (#164); add a real "conversation mode" entry point in the Code tab; tune the VAD thresholds against real driving-cabin noise; fix the tail-truncation in the playback path so a mid-read lock doesn't clip.
- **Needs-#40 (sustained screen-off / driving).** The eyes-free-while-driving end-state — screen off for long stretches, app fully backgrounded — requires native background audio + mic, which a PWA cannot get (the mic track mutes while backgrounded, and long-background pages freeze/discard). The pointer is recorded on **#40**.

## Related

Layers on #164 (Board) and the fleet orchestrator (#245 — the thing voice drives). Reuses #165/#168 (dictation) and #190/#197/#203/#206/#210 (read-aloud). Likely-gated-on / may-feed #40 (native iOS hub).
