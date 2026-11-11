/* The outcome of mirroring a rename between a Life OS conversation and its
 * sessions, as the phone reads it.
 *
 * The server declares the closed set once, in src/life_os/title_sync.py
 * (docs/life-os-title-sync.md has the table); a rename reply's `session_sync`
 * and `life_os_sync` verdicts carry these values. This module is the
 * browser's copy, so a reader asks syncFailed() instead of re-spelling the
 * strings. tests/unit/life_os/test_title_sync_ui.py fails when the two sets
 * drift.
 */

export const TitleSync = Object.freeze({
  SYNCED: 'synced',
  FAILED: 'failed',
});

const KNOWN = new Set(Object.values(TitleSync));

// The TitleSync member a verdict is in, or null for no verdict or a value outside the set.
export function titleSync(verdict) {
  const status = verdict && verdict.status;
  return KNOWN.has(status) ? status : null;
}

// True when the other side kept its old name; the verdict's `error` says why.
export function syncFailed(verdict) {
  return titleSync(verdict) === TitleSync.FAILED;
}
