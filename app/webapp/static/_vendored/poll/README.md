# `poll` — self-scheduling, single-flight polls that pause on a hidden tab

The one way a fleet web page polls its own API. A `setInterval` poll starts a new read whether or not the last one came back and keeps ticking on a hidden tab or a locked phone; on a slow computer a few open pages fill the server's worker pools that way. This module schedules the next read when the last one settles, shares one in-flight read per shape, and arms nothing while the page is hidden. Normalized from app-launcher-lite's `shared/poll.js`. The rules and the reasons: [polling budget](../../../../../docs/polling-budget.md).

## Files

| File | Role |
| --- | --- |
| `poll.js` | `startPoll(read, ms, opts?)` and `singleFlight(read)`. No markup, no styles. |

## Contract

- **`startPoll(read, ms, opts?)`** calls `read()` `ms` after the last call settled (resolved or rejected), never on a fixed rate. A slow server stretches the gap instead of stacking requests.
- **Hidden tab.** While `document.hidden` no tick is armed; a read already out finishes and arms nothing. On the return to the foreground it reads once, straight away, then resumes the spacing. A hidden minute costs zero requests; the return costs one per poll.
- **Busy-only polls.** `opts.activeWhile` is a predicate: while it returns `false`, no tick is armed. A poll under the fleet's 2 s floor must declare it (the request budget ratchet's `poll-floor` rule reads the call for it), so a fast poll runs only while a job is in progress. `kick()` restarts it when the next job begins.
- **Handle.** It returns `{ stop, kick }`: `stop()` ends the poll and drops its listener; `kick()` reads now unless a read is already out.
- **`singleFlight(read)`** wraps a read so every caller that asks while one is out shares its promise (a poll tick, a tab tap, a retry). `.fresh()` is for a caller that just wrote: it waits for the read already out, which may predate the write, then shares the next.
- `read` reports its own failures; the poll swallows a rejection and asks again on the next tick.

## How to vendor

1. Copy this `poll/` folder **verbatim** into `app/webapp/static/_vendored/poll/`.
2. Wrap each read shape once and poll it:
   ```js
   import { startPoll, singleFlight } from './_vendored/poll/poll.js';

   const loadBoard = singleFlight(() => fetch('/api/board').then((r) => r.json()).then(render));
   startPoll(loadBoard, 5000);
   tabButton.addEventListener('click', loadBoard);          // shares a read already out
   afterRename(() => loadBoard.fresh());                     // never a read that predates the write
   const job = startPoll(loadProgress, 1000, { activeWhile: () => state.jobRunning });
   startButton.addEventListener('click', () => { startJob(); job.kick(); });
   ```
3. Put the interval in the app's Settings with a sensible default, not a constant only code can change.
4. Prove the budget in the app's browser tests with the vendored `tests/e2e/budget/_request_budget.py` probe (a minute visible, a minute hidden, requests counted by path).

## Required design tokens

None.
