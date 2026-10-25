/* Did the agent take a message Chat just sent?
 *
 * `/input` answers `submit_state: 'unconfirmed'` for a short payload or a bare
 * submit: Enter went in and nothing checked it. Nearly every such send does
 * land, so a warning on each one trained the owner to ignore it. Chat now says
 * "Sent", then looks for evidence in the transcript it already polls: a new
 * user turn carrying the text, or the agent going busy. Only a window that
 * closes with neither says "not picked up", as its own state, and the text is
 * given back to the box.
 *
 * Pure and DOM-free so Node can pin it (tests/js/send_confirm.test.mjs); the
 * wiring (when to look, what to toast) is session-transcript.js's.
 */

// How long the agent has to show it took the message. Past a few live ticks
// (3 s each) to ride out a slow transcript flush, short enough that a message
// that really was lost is noticed while the owner is still looking.
export const CONFIRM_WINDOW_MS = 15000;

// The characters compared. Entry text is capped server-side, and an attached
// image appends a path, so a prefix is the stable part of a message.
const MATCH_CHARS = 40;

function normalize(text) {
  return String(text == null ? '' : text).replace(/\s+/g, ' ').trim();
}

function isUserTurn(e) {
  return !!e && e.kind === 'user' && !e.sidechain;
}

// A slash command (/compact, /clear) is recorded by the agent as a command
// entry, not as the typed text, so only "some new user turn" can vouch for it.
function isSlashCommand(sent) {
  return normalize(sent).charAt(0) === '/';
}

function sameMessage(entryText, sent) {
  const a = normalize(entryText);
  const b = normalize(sent);
  if (!b) return false;
  const n = Math.min(MATCH_CHARS, a.length, b.length);
  return n > 0 && a.slice(0, n) === b.slice(0, n);
}

// How many turns in `entries` could be this send. Counted at send time and
// again later: sending "yes" twice must not be vouched for by the first "yes".
export function countMatchingTurns(entries, sent) {
  const slash = isSlashCommand(sent);
  let n = 0;
  (entries || []).forEach(function (e) {
    if (!isUserTurn(e)) return;
    if (slash || sameMessage(e.text, sent)) n += 1;
  });
  return n;
}

// The watch a send starts. `entries` and `working` are the pane's state at the
// moment the send was answered.
export function startWatch(sent, entries, working, now) {
  return {
    sent: sent,
    seen: countMatchingTurns(entries, sent),
    wasWorking: !!working,
    deadline: now + CONFIRM_WINDOW_MS,
  };
}

// 'picked-up' once there is evidence, 'waiting' while the window is open,
// 'not-picked-up' once it has closed without any. Busy only counts when it is
// news: an agent already working at send time says nothing about this message.
export function judgeWatch(watch, entries, working, now) {
  if (countMatchingTurns(entries, watch.sent) > watch.seen) return 'picked-up';
  if (working && !watch.wasWorking) return 'picked-up';
  return now >= watch.deadline ? 'not-picked-up' : 'waiting';
}
