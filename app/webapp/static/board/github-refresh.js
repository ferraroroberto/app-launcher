/* When the open Board refreshes its GitHub data by itself.
 *
 * The 5 s Board poll only reads the server's cache; the `gh` searches run in
 * POST /api/board/github/refresh. This gate says when that call is due with
 * nobody tapping ↻: the Board tab is up in a foreground page, no refresh is
 * running, the cache is older than the age (or was never filled), and no
 * attempt was made inside the age. The last rule is what keeps a failing `gh`
 * (the cache keeps its old stamp, the error rides beside it) from becoming a
 * retry loop. A pure function so a plain-Node test can pin it.
 */

export const GH_AUTO_REFRESH_AGE_MS = 5 * 60 * 1000;

export function shouldAutoRefreshGithub({ github, now, hidden, tab, inFlight, lastAttemptAt }) {
  if (hidden || tab !== 'board' || inFlight || !github) return false;
  if (now - lastAttemptAt < GH_AUTO_REFRESH_AGE_MS) return false;
  const fetched = Date.parse(github.fetched_at || '');
  return isNaN(fetched) || now - fetched > GH_AUTO_REFRESH_AGE_MS;
}
