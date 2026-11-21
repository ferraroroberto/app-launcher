/* Screen-first replay for an inline agent's terminal.
 *
 * The socket asks the session-host for `order=screen`. A host that knows it
 * sends the ring's tail first, which draws the current screen on its own,
 * then a `{"type":"history"}` marker, then the full replay it always sent.
 * A host that predates it ignores the parameter and sends the full replay
 * alone, which this module never sees as history and writes as usual.
 *
 * The full replay starts with a clear, so written the normal way xterm would
 * wipe the screen just shown and visibly scroll through the whole history
 * in per-frame slices. It is parsed in one synchronous pass instead: the
 * screen stays up until the next frame paints the same screen with its
 * history above it. Output that arrives meanwhile is held and written after
 * it, in order.
 */

export const SCREEN_FIRST = 'screen';

export function isHistoryMarker(data) {
  if (typeof data !== 'string' || data.charCodeAt(0) !== 0x7b) return false;
  try {
    const message = JSON.parse(data);
    return !!message && typeof message === 'object' && message.type === 'history';
  } catch (_) {
    return false;
  }
}

// xterm 5.5 has no public synchronous write. Its core's writeSync drains
// the write queue in order, so it runs only once that queue is empty: from
// inside a write callback it would replay the chunk being processed.
function parseNow(term, data) {
  const core = term._core;
  if (core && typeof core.writeSync === 'function') {
    try {
      core.writeSync(data);
      return;
    } catch (_) { /* fall back to the queued write below */ }
  }
  term.write(data);
}

// Called with the frame that follows the marker. `onDone` runs once the
// history has been applied; `writeLive(frame)` then writes each frame held
// in the meantime the normal way.
export function applyHistory(terminal, data, writeLive, onDone) {
  const term = terminal.term;
  const gen = terminal.historyGen;
  terminal.historyHold = [];
  // An empty write's callback fires once every earlier write has parsed;
  // the timeout leaves xterm's write loop before the synchronous pass.
  term.write('', function () {
    setTimeout(function () {
      // A reconnect or teardown since then owns the terminal now.
      if (terminal.term !== term || terminal.historyGen !== gen) return;
      parseNow(term, data);
      const held = terminal.historyHold || [];
      terminal.historyHold = null;
      if (onDone) onDone();
      held.forEach(writeLive);
    }, 0);
  });
}
