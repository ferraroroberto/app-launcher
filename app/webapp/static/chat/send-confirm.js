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

// --- messages still pending ----------------------------------------------
//
// A transcript response carries the session's `pending_input`: what the
// session-host holds that the agent has not taken. The host only learns a
// queued message went in once the agent goes idle, while the transcript
// records it the moment the agent takes it, mid-turn included, so a pending
// message with a matching user turn since it was sent is already taken.

// Clock skew allowed between the host's send time and the agent's own
// timestamp on the turn: both come from this PC, so a few seconds is plenty.
const TAKEN_SKEW_MS = 5000;

function turnTime(e) {
  const t = Date.parse(e && e.timestamp);
  return Number.isNaN(t) ? null : t;
}

// The pending messages not yet visible as taken, oldest first. A slash
// command is recorded as a command entry, never as its text, so only the
// host can say it went in.
export function pendingNotTaken(pending, entries) {
  if (!Array.isArray(pending)) return [];
  const turns = (entries || []).filter(isUserTurn);
  return pending.filter(function (p) {
    if (isSlashCommand(p.text)) return true;
    const since = Number(p.at) * 1000 - TAKEN_SKEW_MS;
    return !turns.some(function (e) {
      const t = turnTime(e);
      return t != null && t >= since && sameMessage(e.text, p.text);
    });
  });
}

// Is this exact text already waiting? The check before a second send.
export function alreadyPending(pending, text) {
  const sent = normalize(text);
  return !!sent && (pending || []).some(function (p) { return normalize(p.text) === sent; });
}
