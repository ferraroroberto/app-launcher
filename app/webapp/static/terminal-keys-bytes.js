/* Key -> byte-sequence tables for the on-screen keys D-pad (issue #36, #986).
 *
 * Pure data, no DOM — extracted out of terminal-keys.js so the mapping is
 * pinned by a unit test that runs under plain Node, without a browser
 * (tests/js/terminal_keys_bytes.test.mjs via tests/test_terminal_keys_bytes.py).
 */

export const KEY_BYTES = {
  up: '\x1b[A', down: '\x1b[B', right: '\x1b[C', left: '\x1b[D',
  enter: '\r', esc: '\x1b', tab: '\t',
};

// Shift-modified variants (issue #137). The ⇧ key is a sticky toggle that
// simulates holding Shift, so the next key sent uses these sequences. Tab
// becomes back-tab (`\x1b[Z`) — that's Shift+Tab, the way Claude Code cycles
// permission modes — and the arrows get their xterm Shift CSI form (modifier
// 2). Esc/Enter have no standard Shift sequence, so they fall back to the
// plain KEY_BYTES entry.
export const SHIFT_KEY_BYTES = {
  tab: '\x1b[Z',
  up: '\x1b[1;2A', down: '\x1b[1;2B', right: '\x1b[1;2C', left: '\x1b[1;2D',
};

// Ctrl-modified letters (issue #986). The Ctrl key is a sticky toggle like
// ⇧ (mutually exclusive with it) that exposes C / U / X — interrupt, clear
// the line, and a Ctrl+X-style chord — each a single control byte the PTY
// accepts directly. These keys are disabled in the DOM whenever Ctrl is not
// held, so there is no plain-letter fallback to define here.
//
// Do not double `c` (issue #1024). One `\x03` per tap is correct for every
// agent: Claude Code interrupts on the first, and live ConPTY probes of Grok
// Build 1.0.34 cancel a running turn on the first too ("Turn cancelled by
// user"), including mid-tool. Grok's "press again to quit" is its *idle*
// Ctrl+C — the quit confirmation it prints when there is no turn to cancel —
// not a second press the launcher owes it. A doubled byte here would be a
// second interrupt for Claude; pinned by tests/js/terminal_keys_bytes.test.mjs
// and tests/e2e/test_keys_popover.py::test_ctrl_c_delivers_exactly_one_interrupt_byte.
export const CTRL_KEY_BYTES = {
  c: '\x03', u: '\x15', x: '\x18',
};
