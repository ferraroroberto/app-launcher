/* Read the last AI reply aloud (issue #190): eyes-free / driving mode.
 *
 * The Coding tab is a raw TUI stream, not structured chat — Claude Code /
 * Codex render prose, tool calls, spinners, boxed panels and a live input
 * composer with no machine-readable "assistant message ended here" marker.
 * So "the last reply" is a heuristic over the xterm scrollback: strip the
 * trailing input composer + UI chrome, then walk UP from the bottom and
 * keep the last contiguous block of assistant prose, stopping at the first
 * boundary above it (a tool call/result or the previous user turn).
 *
 * Speaking uses the browser's Web Speech API (`speechSynthesis`) — zero
 * infra, already on iOS Safari, and the button press supplies the user
 * gesture iOS requires. A server-side hub voice is the documented phase-2
 * fallback (local-llm-hub#98), behind the same speak() seam.
 *
 * The matchers are tuned for Claude Code (the primary agent) but degrade
 * sensibly for any agent: the generic core (strip the box composer, take
 * the trailing non-chrome prose, stop at a `>`/`❯` user line) still yields
 * a reasonable block even when the assistant bullet glyph differs.
 */

// Box-drawing + block-element ranges mark TUI chrome: the input composer
// frame and any boxed panel. A line carrying one of these is never prose.
// Box-drawing + block-element glyphs that make up the input composer frame.
const RULE_CHARS_RE = /[─-▟│┄┅┈┉╌╍]/g;
// A run of ≥6 horizontal rule glyphs — catches a *titled* box border
// ("──── voice drive from mobile ────"), where the title text dilutes the
// whole-line ratio below the threshold but the rule run is unmistakable.
const RULE_RUN_RE = /[─━═┄┅┈┉╌╍]{6,}/;
// Tool *result* tree branch: ⎿ └ ╰ ⤷ ↳ — ends a reply when walking up.
const TOOL_RESULT_RE = /^\s*[⎿└╰⤷↳]/;
// User prompt / echoed user turn: leading `>` or `❯` — the previous turn.
const USER_ECHO_RE = /^\s*[>❯]\s?/;
// Recap block: Claude Code prints a "recap:" summary after a turn, closed by
// a "(disable recaps …)" line. The user reads the real reply, not the recap.
const RECAP_START_RE = /^recap\b/i;
const RECAP_END_RE = /disable\s+recaps/i;
// The per-turn timing line: "✻ Crunched for 5s · 1 shell still running",
// "Worked for 21m 17s". Claude Code picks a *random gerund* each turn
// (Worked / Crunched / Pondered / …), so match the shape — an optional
// spinner glyph, a Capitalised word, "for", then a duration — not the verb.
const TIMING_LINE_RE = /^\s*[*✶✻✽✢✱·•∗⁘]?\s*[A-Z][a-z]+ for \d+\s*[smhd]\b/;
// Footer status lines under the box: folder/branch, permission mode, token
// count, the gerund spinner ("Ruminating…", "Forming… (2m · ↓ 7k tokens)"),
// and the keyboard hints. Each is below the composer, but match them anyway
// as a belt-and-braces skip in case the box can't be located.
const STATUS_RE =
  /(\btokens?\b|shift\+tab|esc to|⏵⏵|\? for shortcuts|accept edits|bypass permissions|^\s*[*✶✻✽✢✱·•∗⁘…\s]+$)/i;

function isBlank(line) {
  return !line || !line.trim();
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
// it. The box is the lowest cluster of rule lines (top + bottom border, with
// the `>` prompt and blanks between); everything from its top border down is
// chrome. Returns the conversation slice above the box, or the input
// unchanged when no box is found (so a boxless agent still yields something).
function dropTrailingComposer(arr) {
  let lastRule = -1;
  for (let i = arr.length - 1; i >= 0; i--) {
    if (isRuleLine(arr[i])) { lastRule = i; break; }
  }
  if (lastRule < 0) return arr;
  // Extend up through the box cluster: another rule within a few lines (the
  // prompt + blank gutter) is the top border.
  let top = lastRule;
  let gap = 0;
  for (let i = lastRule - 1; i >= 0 && gap <= 4; i--) {
    if (isRuleLine(arr[i])) { top = i; gap = 0; } else { gap++; }
  }
  return arr.slice(0, top);
}

/**
 * Extract the agent's last spoken reply from already-rendered buffer lines.
 * Pure (no xterm dependency) so it is unit-testable against synthetic
 * transcripts. `lines` is top→bottom, each trailing-trimmed.
 *
 * The Coding tab is a raw TUI, so this is structural, not semantic: cut the
 * composer box + footer, skip the trailing recap / "Worked for" / status
 * noise, then collect the last prose block up to the previous user turn or
 * tool boundary. Hard 51-column wraps are de-wrapped by collapsing the block
 * to a single speakable paragraph.
 *
 * @param {string[]} lines
 * @returns {string} speakable plain text, or '' when there is no reply yet.
 */
export function extractLastReplyFromLines(lines) {
  if (!Array.isArray(lines) || !lines.length) return '';
  const arr = dropTrailingComposer(lines.slice());
  if (!arr.length) return '';

  const collected = [];
  let inRecap = false;
  let started = false;
  for (let i = arr.length - 1; i >= 0; i--) {
    const line = arr[i];
    const t = line.trim();
    if (!started) {
      // Skip the trailing noise between the reply and the composer: blank
      // gutter, the recap block (bottom-up: end line → body → "recap:"),
      // the "Worked for" timing line, and any stray status/spinner remnant.
      if (!t) continue;
      if (RECAP_END_RE.test(t)) { inRecap = true; continue; }
      if (inRecap) { if (RECAP_START_RE.test(t)) inRecap = false; continue; }
      if (RECAP_START_RE.test(t)) continue;
      if (TIMING_LINE_RE.test(t)) continue;
      if (STATUS_RE.test(line)) continue;
    }
    // Boundaries: the previous user turn, a tool result, or an earlier recap
    // all end the current reply.
    if (USER_ECHO_RE.test(line) || TOOL_RESULT_RE.test(line) ||
        RECAP_START_RE.test(t) || RECAP_END_RE.test(t)) {
      break;
    }
    started = true;
    collected.push(line);
  }
  collected.reverse();

  // Collapse to one speakable paragraph — de-wraps the column-wrapped lines
  // and squeezes the blank gutter the TUI renders between them.
  return collected.join(' ').replace(/\s+/g, ' ').trim();
}

// Read the xterm scrollback (scrollback + viewport) into trimmed lines.
function bufferToLines(term) {
  const out = [];
  try {
    const buf = term.buffer.active;
    const total = buf.length;
    for (let i = 0; i < total; i++) {
      const line = buf.getLine(i);
      out.push(line ? line.translateToString(true) : '');
    }
  } catch (_) { /* a torn-down terminal yields no reply */ }
  return out;
}

/** Extract the last reply straight from a live xterm Terminal. */
export function extractLastReply(term) {
  if (!term) return '';
  return extractLastReplyFromLines(bufferToLines(term));
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

// Test seam (#190 e2e): the extraction heuristic and speech helpers are
// standalone, so the suite drives them directly instead of synthesizing a
// live PTY buffer. Read-only — no effect on production behaviour.
if (typeof window !== 'undefined') {
  window.__readback = {
    extractLastReplyFromLines: extractLastReplyFromLines,
    extractLastReply: extractLastReply,
    speak: speak,
    cancelSpeech: cancelSpeech,
  };
}
