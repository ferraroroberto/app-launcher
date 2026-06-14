/* Read an AI reply aloud (issues #190, #197): eyes-free / driving mode.
 *
 * The Coding tab is a raw TUI stream, not structured chat — but Claude Code
 * marks every block with a leading filled bullet whose COLOUR is the signal
 * the Claude Code mobile app keys on to separate reply text from tool output:
 *
 *   ● in the default / white foreground    → an assistant prose reply
 *   ● in a saturated colour (green/red/…)  → a tool call (Bash / Read / …)
 *
 * `translateToString()` throws that colour away, so the live reader pulls each
 * line's leading-cell foreground straight from the xterm cell API and tags the
 * line `assistant` / `tool` / `none`. The buffer then segments cleanly into an
 * ordered list of reply blocks — no bottom-up boundary-walk heuristics. The 🔊
 * button reads the LAST block by default; a future depth-selector ("read last
 * N", #197) is just a slice of that list.
 *
 * One small residual filter survives the colour signal: the per-turn epilogue
 * the TUI renders BELOW the final reply while/after working — the "Worked for"
 * timing line, the recap block, the live thinking spinner, and the spinner's
 * randomised "Tip:" hint (issues #193/#195). Those carry no bullet, so they
 * never open their own block, but they trail the last reply as unmarked
 * continuation; each block is truncated at the first epilogue line.
 *
 * Two voices share one button + one speaking-state machine (`setSpeaking` →
 * `onSpeakingChange`/`onSpeechEnd`):
 *  - **Hub TTS (issues #203, #206)** — the preferred path when the
 *    local-llm-hub is reachable: `speakHub()` POSTs the reply to
 *    `/api/tts/speak`, which streams **headerless PCM16** as the hub
 *    synthesizes, and plays it through the **Web Audio API** — first audio in
 *    ~1.5 s instead of waiting for the whole clip. (An `<audio>` element can't
 *    play the hub's open-ended streaming WAV progressively, so Web Audio reads
 *    the PCM stream and schedules it on an AudioContext timeline; the context
 *    is resumed in the click gesture so iOS lets it sound.)
 *  - **Web Speech API (`speechSynthesis`)** — the on-device fallback when the
 *    hub is unconfigured / down / blocks the POST: zero infra, already on iOS
 *    Safari, and the button press supplies the user gesture iOS requires.
 *
 * The 🔊 button tap is the user gesture both paths need (iOS blocks both
 * synthesized speech and `<audio>.play()` outside one) — so `speakHub` unlocks
 * its audio element synchronously, inside the click tick, before its first
 * `await`.
 */

// Bullet glyphs an agent uses to open a block. The COLOUR (not the glyph)
// decides assistant-vs-tool; the glyph just says "this line opens a block".
const BULLET_RE = /^[●⏺•◉○]$/;
// A leading assistant turn-marker bullet ("● ", "⏺ ", "• ") — strip it from the
// spoken text so speech starts on real prose (only the single leading marker;
// inner markdown bullets are untouched).
const LEAD_BULLET_RE = /^[●⏺•◉○]\s+/;
// Box-drawing + block-element glyphs that make up the input composer frame.
const RULE_CHARS_RE = /[─-▟│┄┅┈┉╌╍]/g;
// A run of ≥6 horizontal rule glyphs — catches a *titled* box border
// ("──── voice drive from mobile ────"), where the title text dilutes the
// whole-line ratio below the threshold but the rule run is unmistakable.
const RULE_RUN_RE = /[─━═┄┅┈┉╌╍]{6,}/;
// ── Per-turn epilogue (the noise below the final reply) ─────────────────────
// Recap block: Claude Code prints a "recap:" summary after a turn (closed by a
// "(disable recaps …)" line). The user reads the real reply, not the recap.
const RECAP_START_RE = /^recap\b/i;
// The per-turn timing line: "✻ Crunched for 5s · 1 shell still running",
// "Worked for 21m 17s". Claude Code picks a *random gerund* each turn, so match
// the shape — an optional spinner glyph, a Capitalised word, "for", a duration.
const TIMING_LINE_RE = /^\s*[*✶✻✽✢✱·•∗⁘]?\s*[A-Z][a-z]+ for \d+\s*[smhd]\b/;
// The *live* thinking spinner: "✻ Cogitating… (4m 39s · thinking)",
// "Ruminating… (2m 3s · ↓ 7.2k tokens)". Match the shape, not the gerund: an
// optional spinner glyph, a Capitalised gerund, a trailing ellipsis, then a
// parenthetical status (issue #193 — the "· thinking" form has no token count).
const SPINNER_LINE_RE = /^\s*[*✶✻✽✢✱·•∗⁘]?\s*[A-Z][a-z]+(?:…|\.\.\.)\s*\(/;
// The spinner's randomised help line, rendered as a tool-result child:
// "⎿  Tip: Running multiple Claude sessions? Use /color and /rename …" — its
// wrapped continuation carries no glyph, so it trails the reply (issue #195).
const TIP_RESULT_RE = /^\s*[⎿└╰⤷↳]\s*Tip\b/i;

// True once a block's prose has ended and the per-turn epilogue begins. The
// epilogue always follows the real reply, so truncating each block at its first
// epilogue line keeps the prose and drops the timing/recap/spinner/tip noise.
function isEpilogue(line) {
  const t = (line || '').trim();
  return TIMING_LINE_RE.test(t) || SPINNER_LINE_RE.test(t) ||
    RECAP_START_RE.test(t) || TIP_RESULT_RE.test(line || '');
}

// A horizontal rule / box-border line — the composer frame. True when the
// non-space content is overwhelmingly rule glyphs (so a wrapped border with a
// few title characters still doesn't qualify, but a plain ─── run does).
function isRuleLine(line) {
  // A long unbroken run of rule glyphs is a border even with a centered title.
  if (RULE_RUN_RE.test(line || '')) return true;
  const nonspace = (line || '').replace(/\s/g, '');
  if (nonspace.length < 8) return false;
  const rules = (nonspace.match(RULE_CHARS_RE) || []).length;
  return rules / nonspace.length >= 0.8;
}

// Drop the trailing input composer box and the entire status footer beneath
// it (rows is `{text, marker}[]`). The box is the lowest cluster of rule lines
// (top + bottom border, with the `>` prompt and blanks between); everything
// from its top border down is chrome. Returns the conversation slice above the
// box, or the input unchanged when no box is found (boxless agents still
// yield something).
function dropTrailingComposer(rows) {
  let lastRule = -1;
  for (let i = rows.length - 1; i >= 0; i--) {
    if (isRuleLine(rows[i].text)) { lastRule = i; break; }
  }
  if (lastRule < 0) return rows;
  // Extend up through the box cluster: another rule within a few lines (the
  // prompt + blank gutter) is the top border.
  let top = lastRule;
  let gap = 0;
  for (let i = lastRule - 1; i >= 0 && gap <= 4; i--) {
    if (isRuleLine(rows[i].text)) { top = i; gap = 0; } else { gap++; }
  }
  return rows.slice(0, top);
}

// Collapse a block's lines to one speakable paragraph: truncate at the first
// epilogue line (timing/recap/spinner/tip), de-wrap the column-wrapped prose,
// squeeze whitespace, and drop the leading assistant turn-marker bullet.
function finalizeBlock(lines) {
  const prose = [];
  for (let i = 0; i < lines.length; i++) {
    if (isEpilogue(lines[i])) break;
    prose.push(lines[i]);
  }
  return prose.join(' ').replace(/\s+/g, ' ').trim().replace(LEAD_BULLET_RE, '');
}

/**
 * Segment already-classified buffer rows into the ordered list of assistant
 * reply blocks (oldest → newest). Pure (no xterm dependency) so it is
 * unit-testable against synthetic transcripts.
 *
 * Each row is `{ text, marker }` where `marker` is `'assistant'` (a default/
 * white reply bullet), `'tool'` (a coloured tool-call bullet), or `'none'`. An
 * `assistant` row opens a block; any marker row (assistant or tool) or the
 * composer box closes it; `none` rows are continuation of an open block (or
 * ignored before the first reply / inside a tool's output).
 *
 * @param {{text: string, marker: string}[]} rows  top→bottom, trailing-trimmed
 * @returns {string[]} speakable reply blocks, in order (empty blocks dropped)
 */
export function extractReplyBlocksFromRows(rows) {
  if (!Array.isArray(rows) || !rows.length) return [];
  const arr = dropTrailingComposer(rows.slice());
  const blocks = [];
  let current = null;
  const flush = function () {
    if (current) {
      const text = finalizeBlock(current);
      if (text) blocks.push(text);
      current = null;
    }
  };
  for (let i = 0; i < arr.length; i++) {
    const row = arr[i];
    if (row.marker === 'assistant') { flush(); current = [row.text]; }
    else if (row.marker === 'tool') { flush(); }
    else if (current) { current.push(row.text); }
  }
  flush();
  return blocks;
}

// True when the leading visible glyph is a coloured (tool-call) bullet rather
// than a default/white (assistant) one. Default fg → assistant; a saturated
// hue (large channel spread) → tool. White/grey are low-spread → assistant, so
// the test is robust across themes without hard-coding the exact bullet colour.
function isToolColor(cell) {
  if (cell.isFgDefault()) return false;
  let rgb = null;
  if (cell.isFgRGB()) {
    const c = cell.getFgColor();
    rgb = [(c >> 16) & 0xff, (c >> 8) & 0xff, c & 0xff];
  } else if (cell.isFgPalette()) {
    rgb = paletteToRgb(cell.getFgColor());
  }
  if (!rgb) return false;
  return Math.max(rgb[0], rgb[1], rgb[2]) - Math.min(rgb[0], rgb[1], rgb[2]) > 60;
}

// Standard xterm 256-colour palette → [r,g,b]: the 16 base colours, the
// 6×6×6 colour cube (16–231), and the 24-step greyscale ramp (232–255).
const BASE16 = [
  [0, 0, 0], [205, 0, 0], [0, 205, 0], [205, 205, 0],
  [0, 0, 238], [205, 0, 205], [0, 205, 205], [229, 229, 229],
  [127, 127, 127], [255, 0, 0], [0, 255, 0], [255, 255, 0],
  [92, 92, 255], [255, 0, 255], [0, 255, 255], [255, 255, 255],
];
const CUBE = [0, 95, 135, 175, 215, 255];
function paletteToRgb(i) {
  if (i < 16) return BASE16[i];
  if (i < 232) {
    const n = i - 16;
    return [CUBE[Math.floor(n / 36) % 6], CUBE[Math.floor(n / 6) % 6], CUBE[n % 6]];
  }
  const g = 8 + (i - 232) * 10;
  return [g, g, g];
}

// Classify a live buffer line by its leading visible glyph + that glyph's
// foreground colour: 'assistant' | 'tool' | 'none'.
function lineMarker(line) {
  const len = line.length;
  let cell;
  for (let x = 0; x < len; x++) {
    cell = line.getCell(x, cell);
    if (!cell) continue;
    const ch = cell.getChars();
    if (ch === '' || ch === ' ') continue;   // leading indentation
    if (!BULLET_RE.test(ch)) return 'none';   // prose / box / tool-result line
    return isToolColor(cell) ? 'tool' : 'assistant';
  }
  return 'none';
}

// Read the xterm scrollback (scrollback + viewport) into classified rows.
function bufferToRows(term) {
  const out = [];
  try {
    const buf = term.buffer.active;
    const total = buf.length;
    for (let i = 0; i < total; i++) {
      const line = buf.getLine(i);
      out.push({
        text: line ? line.translateToString(true) : '',
        marker: line ? lineMarker(line) : 'none',
      });
    }
  } catch (_) { /* a torn-down terminal yields no reply */ }
  return out;
}

/** Extract every assistant reply block (oldest → newest) from a live xterm
 *  Terminal. The future depth-selector (#197) reads a slice of this list. */
export function extractReplyBlocks(term) {
  if (!term) return [];
  return extractReplyBlocksFromRows(bufferToRows(term));
}

/** Extract the agent's last spoken reply straight from a live xterm Terminal,
 *  or '' when there is no reply yet. */
export function extractLastReply(term) {
  const blocks = extractReplyBlocks(term);
  return blocks.length ? blocks[blocks.length - 1] : '';
}

// ── Speech ────────────────────────────────────────────────────────────────

export function isSpeechSupported() {
  return !!(window.speechSynthesis && window.SpeechSynthesisUtterance);
}

let _speaking = false;
let _onStateChange = null;
let _onEnd = null;
let _watchdog = null;
let _observed = false;

function setSpeaking(on) {
  _speaking = on;
  if (_onStateChange) {
    try { _onStateChange(on); } catch (_) { /* UI callback best-effort */ }
  }
}

/** Register a callback fired whenever speaking starts/stops (UI sync). */
export function onSpeakingChange(cb) { _onStateChange = cb; }

/** Register a callback fired once when speech finishes *naturally* (the queue
 *  drained on its own — not a manual stop). */
export function onSpeechEnd(cb) { _onEnd = cb; }

export function isSpeaking() { return _speaking; }

function stopWatchdog() {
  if (_watchdog) { clearInterval(_watchdog); _watchdog = null; }
}

// iOS Safari fires an utterance's `onend` unreliably, so the button could
// stick in its blue "speaking" state forever. Poll the engine instead: once
// we've seen it actually start, both `speaking` and `pending` going false is
// a natural finish.
function startWatchdog() {
  stopWatchdog();
  _observed = false;
  _watchdog = setInterval(function () {
    const s = window.speechSynthesis;
    if (!s) { finishNaturally(); return; }
    if (s.speaking) { _observed = true; return; }
    if (_observed && !s.pending) finishNaturally();
  }, 250);
}

function finishNaturally() {
  stopWatchdog();
  if (!_speaking) return;        // already finalized (e.g. by a manual cancel)
  setSpeaking(false);
  if (_onEnd) { try { _onEnd(); } catch (_) { /* best effort */ } }
}

// Split into short sentence-ish chunks. iOS truncates long single
// utterances, so each sentence becomes its own queued utterance (which also
// makes cancel() responsive mid-reply). Avoids regex lookbehind for older
// iOS Safari.
function chunkForSpeech(text) {
  const rough = text.replace(/([.!?])\s+/g, '$1\n').split(/\n+/);
  const chunks = [];
  for (let i = 0; i < rough.length; i++) {
    const s = rough[i].trim();
    if (!s) continue;
    if (s.length <= 240) {
      chunks.push(s);
    } else {
      for (let j = 0; j < s.length; j += 240) chunks.push(s.slice(j, j + 240));
    }
  }
  return chunks;
}

// Prefer a voice matching the page language, favouring the higher-quality
// "enhanced"/neural voices over the robotic compact default when present.
function pickVoice(synth, lang) {
  let voices = [];
  try { voices = synth.getVoices() || []; } catch (_) { voices = []; }
  if (!voices.length) return null;
  const pref = (lang || 'en').slice(0, 2).toLowerCase();
  const matched = voices.filter(function (v) {
    return (v.lang || '').toLowerCase().indexOf(pref) === 0;
  });
  const pool = matched.length ? matched : voices;
  const enhanced = pool.find(function (v) {
    return !/compact/i.test(v.name || '') &&
      /enhanced|premium|neural|natural|siri/i.test(v.name || '');
  });
  return enhanced || pool.find(function (v) { return v.default; }) || pool[0];
}

/**
 * Speak `text` aloud. Returns false when the browser has no speech synthesis
 * or there is nothing to say.
 *
 * iOS Safari notes baked in here, the hard way:
 *  - **Never call `cancel()` synchronously right before `speak()`** — iOS
 *    silently drops the new utterance. So we only cancel when the engine is
 *    actually busy, and the button handler turns a re-press into a pure
 *    cancel (it never re-enters speak while speaking).
 *  - The `speak()` must run **inside the user-gesture tick** (the button
 *    click) — so no setTimeout, no awaiting voices.
 *  - iOS sometimes starts the queue **paused**; an explicit `resume()` after
 *    queuing kicks it into audible playback.
 */
export function speak(text, opts) {
  const synth = window.speechSynthesis;
  if (!synth || !window.SpeechSynthesisUtterance) return false;
  const chunks = chunkForSpeech(text || '');
  if (!chunks.length) return false;
  // Only clear a genuinely in-flight queue; a blanket cancel() here is what
  // makes the very next speak() silent on iOS.
  if (synth.speaking || synth.pending) {
    try { synth.cancel(); } catch (_) { /* best effort */ }
  }
  const rate = (opts && opts.rate) || 1.3;
  const lang = (opts && opts.lang) || navigator.language || 'en-US';
  const voice = pickVoice(synth, lang);
  setSpeaking(true);
  for (let i = 0; i < chunks.length; i++) {
    const u = new window.SpeechSynthesisUtterance(chunks[i]);
    u.rate = rate;
    u.lang = lang;
    u.volume = 1;
    if (voice) u.voice = voice;
    // Fast-path finish when the last utterance's onend does fire; the
    // watchdog is the reliable backstop on iOS where it often doesn't.
    u.onend = function () {
      if (!synth.pending && !synth.speaking) finishNaturally();
    };
    u.onerror = function () {
      if (!synth.pending && !synth.speaking) finishNaturally();
    };
    synth.speak(u);
  }
  // iOS can leave the engine paused on the first speak after load.
  try { synth.resume(); } catch (_) { /* best effort */ }
  startWatchdog();
  return true;
}

/** Stop any in-flight speech and reset to idle (a manual stop — no end
 *  callback, unlike a natural finish). */
export function cancelSpeech() {
  stopWatchdog();
  const synth = window.speechSynthesis;
  if (synth) { try { synth.cancel(); } catch (_) { /* best effort */ } }
  setSpeaking(false);
}

// ── Hub TTS (issues #203, #206): high-quality Orpheus voice over local-llm-hub ─
// The hub streams *headerless PCM16* (`/api/tts/speak` → `audio/L16` +
// `X-Sample-Rate`) and we play it through the **Web Audio API** — read the
// streaming fetch with getReader(), convert each int16 chunk to float32, and
// schedule AudioBufferSourceNodes back-to-back on an AudioContext timeline.
// This is the technique the hub's own TTS UI uses (first audio in ~1.4 s).
//
// Why not <audio src>: the hub's streaming WAV carries an open-ended RIFF
// header (0xFFFFFFFF sizes) that an <audio> element can't play progressively —
// it just buffers silently (#206). Web Audio sidesteps the container entirely.
// The AudioContext is created + resumed inside the click gesture (before the
// first await) so iOS autoplay policy lets it make sound.

let _hubCtx = null;          // the AudioContext currently rendering hub audio
let _hubReader = null;       // the streaming-body reader (cancelled on stop)
let _hubAbort = null;        // AbortController for the in-flight fetch
let _hubEndTimer = null;     // fires finishHubNaturally once the last buffer ends
let _hubAvailable = null;    // tri-state: null = unprobed, true / false

// Bearer + passkey terminal token, supplied by the caller (terminal.js owns
// the token plumbing; this module stays free of api.js / webauthn.js imports).
function authHeaders(opts) {
  const h = {};
  if (opts && opts.token) h['Authorization'] = 'Bearer ' + opts.token;
  if (opts && opts.terminalToken) h['x-terminal-token'] = opts.terminalToken;
  return h;
}

/** Probe whether the hub's read-aloud voice is reachable right now and cache
 *  the result. Returns the boolean; never throws (a failure caches false). */
export async function probeHub(opts) {
  try {
    const res = await fetch('/api/tts/health', { headers: authHeaders(opts) });
    if (!res.ok) { _hubAvailable = false; return false; }
    const body = await res.json().catch(function () { return null; });
    _hubAvailable = !!(body && body.available);
  } catch (_) {
    _hubAvailable = false;
  }
  return _hubAvailable;
}

/** True once a probe has confirmed the hub voice is reachable. */
export function isHubAvailable() { return _hubAvailable === true; }

// Tear down all hub-playback resources (timer, reader, fetch, AudioContext).
// Closing the context silences any still-scheduled buffers immediately.
function hubTeardown() {
  if (_hubEndTimer) { clearTimeout(_hubEndTimer); _hubEndTimer = null; }
  if (_hubReader) {
    try { _hubReader.cancel(); } catch (_) { /* best effort */ }
    _hubReader = null;
  }
  if (_hubAbort) {
    try { _hubAbort.abort(); } catch (_) { /* best effort */ }
    _hubAbort = null;
  }
  if (_hubCtx) {
    const ctx = _hubCtx;
    _hubCtx = null;
    try { ctx.close(); } catch (_) { /* best effort */ }
  }
}

function finishHubNaturally() {
  hubTeardown();
  if (!_speaking) return;       // already finalized (e.g. by a manual cancel)
  setSpeaking(false);
  if (_onEnd) { try { _onEnd(); } catch (_) { /* best effort */ } }
}

// Stream PCM16 from `res` and schedule it on `ctx`'s timeline. Resolves once the
// WHOLE stream has been read + scheduled; the audio keeps playing until the last
// buffer's end, after which `finishHubNaturally` fires. Mirrors local-llm-hub's
// playground.js speakStream.
async function pumpPcmStream(ctx, res, ac) {
  const sampleRate =
    parseInt(res.headers.get('X-Sample-Rate') || '24000', 10) || 24000;
  let playHead = ctx.currentTime + 0.12;   // small lead-in to avoid underrun
  let leftover = new Uint8Array(0);
  const reader = res.body.getReader();
  _hubReader = reader;
  for (;;) {
    const chunk = await reader.read();
    if (chunk.done) break;
    if (ac.signal.aborted) return;
    const value = chunk.value;
    if (!value || value.length === 0) continue;
    // Merge any odd trailing byte from the previous chunk, then split into whole
    // int16 samples (carry the remainder forward).
    const merged = new Uint8Array(leftover.length + value.length);
    merged.set(leftover, 0);
    merged.set(value, leftover.length);
    const usable = merged.length - (merged.length % 2);
    leftover = merged.slice(usable);
    if (usable === 0) continue;
    const i16 = new Int16Array(merged.buffer.slice(0, usable));
    const f32 = new Float32Array(i16.length);
    for (let i = 0; i < i16.length; i++) f32[i] = i16[i] / 32768;
    const buf = ctx.createBuffer(1, f32.length, sampleRate);
    buf.copyToChannel(f32, 0);
    const node = ctx.createBufferSource();
    node.buffer = buf;
    node.connect(ctx.destination);
    node.start(playHead);
    playHead += buf.duration;
  }
  // Finalize once the last scheduled buffer has drained.
  const ms = Math.max(0, (playHead - ctx.currentTime) * 1000) + 200;
  _hubEndTimer = setTimeout(function () { finishHubNaturally(); }, ms);
}

/**
 * Speak `text` through the hub's Orpheus voice, played progressively via Web
 * Audio. Resolves true once the stream has been consumed (audio keeps playing
 * until its scheduled end); **rejects** on any failure (hub unconfigured /
 * down / blocked POST / no Web Audio) so the caller can fall back to `speak()`.
 * A manual cancel is NOT a rejection.
 *
 * `opts`: `{ token, terminalToken, voice, speed }`. Must be called directly
 * inside the button-click handler — the synchronous prologue (before the first
 * `await`) creates + resumes the AudioContext within the user gesture iOS
 * requires.
 */
export async function speakHub(text, opts) {
  const clean = (text || '').trim();
  if (!clean) return false;
  cancelHub();                 // a second press / restart cancels in-flight audio
  const AudioCtx = window.AudioContext || window.webkitAudioContext;
  if (!AudioCtx) throw new Error('Web Audio API unavailable');
  // Create + resume the context NOW, in the gesture tick, so iOS lets it sound.
  const ctx = new AudioCtx();
  _hubCtx = ctx;
  try { ctx.resume(); } catch (_) { /* best effort */ }
  const ac = new AbortController();
  _hubAbort = ac;
  setSpeaking(true);
  try {
    const res = await fetch('/api/tts/speak', {
      method: 'POST',
      headers: Object.assign(
        { 'Content-Type': 'application/json' }, authHeaders(opts)
      ),
      body: JSON.stringify({
        text: clean,
        voice: (opts && opts.voice) || undefined,
        speed: (opts && opts.speed) || undefined,
      }),
      signal: ac.signal,
    });
    if (!res.ok) throw new Error('hub tts HTTP ' + res.status);
    if (!res.body || !res.body.getReader) throw new Error('hub tts stream unsupported');
    await pumpPcmStream(ctx, res, ac);
    return true;
  } catch (err) {
    if (ac.signal.aborted) return true;   // cancelled — not a failure, no fallback
    // Only tear down if this call still owns the shared state — a newer
    // speakHub() may have aborted us and taken over (don't clobber it).
    if (_hubAbort === ac) cancelHub();
    throw err;
  }
}

/** Stop any in-flight hub read-aloud and reset to idle (a manual stop). */
export function cancelHub() {
  hubTeardown();
  setSpeaking(false);
}

// Test seam (#190/#197 e2e): the block segmentation and speech helpers are
// standalone, so the suite drives them directly. `extractReplyBlocksFromRows`
// takes synthetic `{text, marker}` rows (the marker is what the cell-colour
// reader derives live); `extractReplyBlocks`/`extractLastReply` exercise the
// real cell-colour path against a Terminal the suite writes ANSI into.
// Read-only — no effect on production behaviour.
if (typeof window !== 'undefined') {
  window.__readback = {
    extractReplyBlocksFromRows: extractReplyBlocksFromRows,
    extractReplyBlocks: extractReplyBlocks,
    extractLastReply: extractLastReply,
    speak: speak,
    cancelSpeech: cancelSpeech,
    speakHub: speakHub,
    cancelHub: cancelHub,
    probeHub: probeHub,
    isHubAvailable: isHubAvailable,
  };
}
