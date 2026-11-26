/* What the Sync all dialog and its toast say about a run.
 *
 * The server records a run as { started_at, finished_at, total, done, current,
 * error, results } (src/run_progress.py), the same progress shape the
 * History refresh uses. Plain functions of that record, so the wording and the
 * counts are pinned under Node without a browser.
 */

const NAMES_SHOWN = 3;

// Failures first, then what was left alone, then what moved, then what was
// already current: the lines that need a person come before the ones that do not.
const OUTCOME_RANK = { failed: 0, skipped: 1, pulled: 2, 'up-to-date': 3 };

function rank(outcome) {
  return outcome in OUTCOME_RANK ? OUTCOME_RANK[outcome] : OUTCOME_RANK.skipped;
}

// "alpha, beta and 2 more": a long list of projects stays one readable line.
function nameList(names) {
  if (names.length <= NAMES_SHOWN) return names.join(', ');
  return names.slice(0, NAMES_SHOWN).join(', ') + ' and ' + (names.length - NAMES_SHOWN) + ' more';
}

// The projects of one outcome, in the order the server finished them.
function byOutcome(run, outcome) {
  return ((run && run.results) || []).filter(function (r) { return r.outcome === outcome; });
}

// True while the run is in flight; false for a finished run or none at all.
export function isRunning(run) {
  return !!(run && !run.finished_at);
}

// The line while a run is in flight: "Syncing 3 of 26 · name". Before the
// server has counted the projects it only says it started.
export function syncProgress(run) {
  if (!run || !(run.total > 0)) return 'Fetching and fast-forwarding…';
  const line = 'Syncing ' + Math.min(run.done + 1, run.total) + ' of ' + run.total;
  return run.current ? line + ' · ' + run.current : line;
}

// The counts a finished run is reported by, in the words the dialog uses.
export function syncCounts(run) {
  return {
    pulled: byOutcome(run, 'pulled').length,
    current: byOutcome(run, 'up-to-date').length,
    skipped: byOutcome(run, 'skipped').length,
    failed: byOutcome(run, 'failed').length,
  };
}

// The skip reasons of a run, each once ("dirty", "not on main").
function skipReasons(run) {
  const reasons = byOutcome(run, 'skipped').map(function (r) { return r.reason; }).filter(Boolean);
  return Array.from(new Set(reasons));
}

// How a finished run reads, `{ text, kind }`: kind '' (neutral) or 'error'.
// A skipped project is counted and its reason named but is not a failure; only
// a project that failed, or a run that broke, turns it red. Null while the run
// is still going or when there is none.
export function syncSummary(run) {
  if (!run || !run.finished_at) return null;
  if (run.error) return { text: 'Sync all stopped: ' + run.error, kind: 'error' };
  const n = syncCounts(run);
  const parts = [];
  parts.push(n.pulled + ' fast-forwarded');
  parts.push(n.current + ' already current');
  if (n.skipped) {
    const reasons = skipReasons(run);
    parts.push(n.skipped + ' skipped' + (reasons.length ? ' (' + reasons.join(', ') + ')' : ''));
  }
  if (n.failed) {
    parts.push(n.failed + ' failed: ' + nameList(byOutcome(run, 'failed').map(function (r) { return r.name; })));
  }
  return { text: parts.join(' · '), kind: n.failed ? 'error' : '' };
}

// The dialog's lines: failures, skips, fast-forwards, then the current ones.
export function sortResults(results) {
  return (results || []).slice().sort(function (x, y) {
    return rank(x.outcome) - rank(y.outcome) || x.name.localeCompare(y.name);
  });
}
