/* The one wrapper for a sensitive action: stop, resume, hand off, start.
 *
 * On a slow page a tap with no visible answer gets tapped again, and a second
 * launch opens the same session twice. This wrapper does three things, in this
 * order, for every such action:
 *
 *   1. shows a pending toast in the tap's own task, before any network answer;
 *   2. marks `key` in flight, so a repeat tap on the same target sends nothing;
 *   3. replaces the pending toast with the outcome — done, failed (and why), or
 *      not confirmed.
 *
 * "Not confirmed" is its own outcome, never folded into success or failure: it
 * is a request that outlived `timeoutMs`, or one that died with no HTTP answer
 * (the server may or may not have acted). The key stays held until that
 * request really settles, so a repeat tap cannot double-send into the gap.
 */

import { AuthRequiredError, apiFailToast, toast } from '../api.js';

export const SensitiveOutcome = Object.freeze({
  DONE: 'done',
  FAILED: 'failed',
  UNCONFIRMED: 'unconfirmed',
  DROPPED: 'dropped',
});

// Long enough for a cold PTY spawn on a loaded box, short enough that a hung
// request tells the owner to go and look.
export const SENSITIVE_TIMEOUT_MS = 20000;

// A pending toast outlives the timeout by this much, so the outcome toast —
// not an expiry — is what replaces it.
const PENDING_HOLD_MS = 2000;

const inFlight = new Set();

export function isInFlight(key) {
  return inFlight.has(key);
}

// opts:
//   key         the target's identity ('stop:<sid>', 'resume:<capture path>')
//   pending     toast text shown at once ("Stopping X…")
//   icon        glyph for the pending and done toasts
//   run         () => Promise — the request; started synchronously
//   done        (result) => text (shown with `icon`), or { text, icon } for its own glyph
//   failPrefix  "Stop failed" — heads the server's reason on a failure
//   unconfirmed text for the not-confirmed toast
//   host        toast element inside an open modal <dialog>, when the tap is in one
//   doneOnPage  the dialog closes on success, so the done toast goes to the page's toast
//   timeoutMs   defaults to SENSITIVE_TIMEOUT_MS
// `notify` is toast() unless a test passes its own recorder.
// Resolves { outcome, result?, error? }; the caller runs its follow-up
// (close an overlay, open the terminal) only on SensitiveOutcome.DONE.
export async function runSensitiveAction(opts, notify = toast) {
  const timeoutMs = opts.timeoutMs || SENSITIVE_TIMEOUT_MS;
  const host = opts.host || undefined;
  if (inFlight.has(opts.key)) return { outcome: SensitiveOutcome.DROPPED };
  inFlight.add(opts.key);
  notify(opts.pending, '', {
    icon: 'hourglass', host: host, duration: timeoutMs + PENDING_HOLD_MS,
  });

  const settled = (async function () { return opts.run(); })().then(
    function (result) { return { ok: true, result: result }; },
    function (error) { return { ok: false, error: error }; }
  );
  let timer = null;
  const timedOut = new Promise(function (resolve) {
    timer = setTimeout(function () { resolve(null); }, timeoutMs);
  });
  const first = await Promise.race([settled, timedOut]);
  clearTimeout(timer);

  if (first === null) {
    settled.then(function () { inFlight.delete(opts.key); });
    notify(opts.unconfirmed, 'error', { icon: 'triangle-alert', host: host });
    return { outcome: SensitiveOutcome.UNCONFIRMED };
  }
  inFlight.delete(opts.key);

  if (first.ok) {
    const shown = opts.done ? opts.done(first.result) : opts.pending;
    const text = typeof shown === 'string' ? shown : shown.text;
    // A { text, icon } result states its own glyph, possibly none.
    const glyph = typeof shown === 'string' ? opts.icon : shown.icon;
    if (opts.doneOnPage && host) {
      // The sheet closes on success; its pending toast must not be waiting
      // there the next time it opens.
      host.hidden = true;
      notify(text, 'good', { icon: glyph });
    } else {
      notify(text, 'good', { icon: glyph, host: host });
    }
    return { outcome: SensitiveOutcome.DONE, result: first.result };
  }

  const exc = first.error;
  if (exc instanceof AuthRequiredError || (exc && exc.status === 401)) {
    // The sign-in sheet is up: replace the pending toast, don't stack a red one.
    apiFailToast(opts.failPrefix, exc);
    notify('Sign in, then try again.', 'error', { icon: 'triangle-alert', host: host });
    return { outcome: SensitiveOutcome.FAILED, error: exc };
  }
  if (exc && exc.status) {
    notify(opts.failPrefix + ': ' + (exc.message || exc.status), 'error', {
      icon: 'triangle-alert', host: host,
    });
    return { outcome: SensitiveOutcome.FAILED, error: exc };
  }
  // No HTTP answer at all (the connection dropped): the server may have acted.
  notify(opts.unconfirmed, 'error', { icon: 'triangle-alert', host: host });
  return { outcome: SensitiveOutcome.UNCONFIRMED, error: exc };
}
