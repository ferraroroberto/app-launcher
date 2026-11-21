/* Last-known data for the first paint: the session list and the Board are
 * drawn from what this device last saw before any request goes out, then the
 * live fetch replaces them. A slow or failing server therefore never leaves
 * either list blank; a failed refresh shows the usual stale line with the
 * snapshot's time.
 *
 * One localStorage entry per list, holding the response body and when it was
 * read. Every storage access is guarded: blocked storage, a full quota or a
 * corrupt entry all read as "no snapshot", never an error. Lists stay on this
 * device; nothing here is sent anywhere.
 */

// Bump when a saved body would no longer render: older entries are ignored.
const PREFIX = 'app-launcher.snapshot.v1.';
// A snapshot from yesterday misleads more than a loading line helps.
export const MAX_AGE_MS = 24 * 3600 * 1000;
// A body this large is not worth the main-thread time to write on every poll.
const MAX_CHARS = 300000;

export function saveSnapshot(key, body) {
  try {
    const text = JSON.stringify({ at: Date.now(), body: body });
    if (text.length > MAX_CHARS) return;
    localStorage.setItem(PREFIX + key, text);
  } catch (_) { /* storage blocked or full: the next load just waits */ }
}

// `{ at, body }`, or null when there is none, it is too old, or unreadable.
export function readSnapshot(key, now) {
  let entry;
  try {
    entry = JSON.parse(localStorage.getItem(PREFIX + key) || 'null');
  } catch (_) {
    return null;
  }
  if (!entry || typeof entry.at !== 'number' || entry.body == null) return null;
  const age = (now || Date.now()) - entry.at;
  if (age < 0 || age > MAX_AGE_MS) return null;
  return entry;
}
