/* The page's live polls: one read in flight per shape, nothing while hidden.
 *
 * A page that overlaps its own reads fills the server's worker pools on a slow
 * computer, so every poll here follows three rules:
 *
 *   - singleFlight(): a caller that asks while the last read is still out
 *     shares that read instead of starting a second one.
 *   - startPoll(): the next tick is scheduled when the last one has settled,
 *     so a slow server stretches the gap instead of stacking requests.
 *   - A hidden tab or locked screen arms no tick. Coming back reads once,
 *     straight away, then resumes the normal spacing.
 *
 * A poll faster than the fleet's 2 s floor is allowed only while a job is in
 * progress: it declares `activeWhile`, stops by itself once that turns false,
 * and `kick()` starts it again when the next job begins.
 */

// Wrap a read so concurrent callers share its one in-flight promise.
// `.fresh()` is for a caller that just wrote: a read already out may predate
// the write, so it waits for that one and then shares the next.
export function singleFlight(read) {
  let inFlight = null;
  function shared() {
    if (inFlight) return inFlight;
    let p;
    try {
      p = Promise.resolve(read());
    } catch (exc) {
      return Promise.reject(exc);
    }
    inFlight = p;
    p.finally(function () {
      if (inFlight === p) inFlight = null;
    }).catch(function () {});
    return p;
  }
  shared.fresh = function () {
    if (!inFlight) return shared();
    return inFlight.catch(function () {}).then(shared);
  };
  return shared;
}

// Poll `read` every `ms` after it settles, paused while the page is hidden.
// opts.activeWhile: a predicate; while it returns false no tick is armed.
// Returns { stop, kick }: stop() ends the poll for good, kick() reads now.
export function startPoll(read, ms, opts) {
  const activeWhile = (opts && opts.activeWhile) || null;
  let timer = null;
  let running = false;
  let stopped = false;

  function idle() {
    return stopped || document.hidden || (activeWhile !== null && !activeWhile());
  }

  function arm() {
    window.clearTimeout(timer);
    timer = idle() ? null : window.setTimeout(tick, ms);
  }

  async function tick() {
    window.clearTimeout(timer);
    timer = null;
    if (idle()) return;
    running = true;
    try {
      await read();
    } catch (_) {
      // Each read reports its own failure; the poll just asks again later.
    } finally {
      running = false;
    }
    // Hidden mid-read: arm nothing; the return to the foreground reads.
    arm();
  }
  // An e2e fake clock names a poll by the read it runs.
  tick.pollSource = String(read);

  function onVisibility() {
    if (document.hidden) {
      window.clearTimeout(timer);
      timer = null;
    } else if (!running) {
      tick();
    }
  }
  document.addEventListener('visibilitychange', onVisibility);
  arm();

  return {
    stop: function () {
      stopped = true;
      window.clearTimeout(timer);
      timer = null;
      document.removeEventListener('visibilitychange', onVisibility);
    },
    kick: function () {
      if (!running) tick();
    },
  };
}
