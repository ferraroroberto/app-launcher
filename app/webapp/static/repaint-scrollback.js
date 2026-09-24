/* Clear the terminal's scrollback on a full-viewport repaint (#930).
 *
 * When one turn's live region grows taller than the viewport, Claude Code
 * (Ink) can no longer redraw it in place with cursor-up. It homes the
 * cursor, erases every viewport row and redraws the region's tail:
 *
 *     ESC[H  (ESC[2K ESC[1B) x N  ESC[H     with N = the viewport's rows
 *
 * ESC[2K reaches only the viewport, so whatever the redraw repeats from
 * above it lands in xterm's scrollback a second time. xterm has no API to
 * delete particular scrollback lines (#1038, #930's 2026-09-22 comment), so
 * the one lever is clearing scrollback at that moment: no duplicate, at the
 * cost of the terminal's history before a long turn. Chat mode reads the
 * conversation from the transcript and keeps all of it.
 *
 * The clear goes in right *after* the preamble, where the viewport is
 * already blank: clearing scrollback there is the same as clearing it just
 * before, and no byte ever has to be held back to decide. A preamble split
 * across socket messages is matched through a small carry of the previous
 * message's tail. Only a preamble that erases the whole viewport counts: a
 * partial erase is an ordinary redraw.
 */

export const CLEAR_SCROLLBACK = '\x1b[3J';

const PREAMBLE = /\x1b\[H((?:\x1b\[2K\x1b\[1B)+)\x1b\[H/g;
// A message tail that could still become a preamble: an unfinished opening
// ESC[H, or ESC[H with any number of complete erase-and-down pairs and an
// unfinished piece of the next pair or of the closing ESC[H.
const PARTIAL = /(?:\x1b\[H(?:\x1b\[2K\x1b\[1B)*(?:\x1b(?:\[(?:2(?:K(?:\x1b(?:\[(?:1)?)?)?)?|H?)?)?)?|\x1b\[?)$/;
const PAIR = '\x1b[2K\x1b[1B';

// Returns a filter `(data, rows) => data` holding its carry between calls:
// one per terminal, fed every output frame in order.
export function createRepaintClearer() {
  let carry = '';
  return function (data, rows) {
    if (typeof data !== 'string' || !data) return data;
    const text = carry + data;
    const skip = carry.length;
    let out = '';
    let from = 0;  // index into `data` copied up to
    PREAMBLE.lastIndex = 0;
    let m;
    while ((m = PREAMBLE.exec(text)) !== null) {
      const erased = m[1].length / PAIR.length;
      const end = m.index + m[0].length - skip;
      // A preamble that ended inside the carry was already handled when
      // that message went through; the carry never holds a complete one.
      if (end <= 0) continue;
      if (erased >= rows) {
        out += data.slice(from, end) + CLEAR_SCROLLBACK;
        from = end;
      }
      // The closing ESC[H can open the next preamble only in theory; step
      // back over it so a back-to-back pair is still seen.
      PREAMBLE.lastIndex = m.index + m[0].length - 3;
    }
    out += data.slice(from);
    const tail = PARTIAL.exec(text.slice(-(PAIR.length * (rows + 2) + 8)));
    carry = tail ? tail[0] : '';
    return out;
  };
}
