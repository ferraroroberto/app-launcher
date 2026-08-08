/* On-screen keys D-pad popover (issue #36).
 *
 * Arrow / Esc / Tab / Enter keys for iPhone keyboards (SwiftKey etc.) that
 * lack them, so an agent's TUI prompts stay navigable from the phone. Each
 * key sends the matching VT/xterm escape sequence over the same WS `input`
 * channel as paste, and ⇧ is a sticky modifier (issue #137).
 *
 * Split out of terminal.js in issue #723, continuing the #315 split — this
 * is self-contained and orthogonal to the terminal's own lifecycle and
 * sizing.
 */

import { els, state } from './state.js';
import { bindOutsideClickToClose } from './dom-utils.js';

// On-screen keys popover (issue #36): a D-pad of arrow/Esc/Tab/Enter
// keys for iPhone keyboards (SwiftKey etc.) that lack them, so Claude's
// TUI prompts are navigable from the phone. Each key sends the matching
// VT/xterm escape sequence over the same WS `input` channel as paste.
const KEY_BYTES = {
  up: '\x1b[A', down: '\x1b[B', right: '\x1b[C', left: '\x1b[D',
  enter: '\r', esc: '\x1b', tab: '\t',
};

// Shift-modified variants (issue #137). The ⇧ key is a sticky toggle that
// simulates holding Shift, so the next key sent uses these sequences. Tab
// becomes back-tab (`\x1b[Z`) — that's Shift+Tab, the way Claude Code cycles
// permission modes — and the arrows get their xterm Shift CSI form (modifier
// 2). Esc/Enter have no standard Shift sequence, so they fall back to the
// plain KEY_BYTES entry below.
const SHIFT_KEY_BYTES = {
  tab: '\x1b[Z',
  up: '\x1b[1;2A', down: '\x1b[1;2B', right: '\x1b[1;2C', left: '\x1b[1;2D',
};

let _disposeKeysOutsideClick = null;
// Sticky-Shift state: stays engaged across taps (so ⇧ then Tab Tab Tab cycles
// modes) until ⇧ is tapped again or the popover closes.
let _shiftHeld = false;

function setShiftHeld(held) {
  _shiftHeld = held;
  if (!els.terminalKeysPopover) return;
  const btn = els.terminalKeysPopover.querySelector('.key-shift');
  if (btn) {
    btn.classList.toggle('active', held);
    btn.setAttribute('aria-pressed', held ? 'true' : 'false');
  }
}

export function closeKeysPopover() {
  if (!els.terminalKeysPopover) return;
  els.terminalKeysPopover.hidden = true;
  setShiftHeld(false);
  if (_disposeKeysOutsideClick) {
    _disposeKeysOutsideClick();
    _disposeKeysOutsideClick = null;
  }
}

function openKeysPopover() {
  if (!els.terminalKeysPopover) return;
  els.terminalKeysPopover.hidden = false;
  if (!_disposeKeysOutsideClick) {
    _disposeKeysOutsideClick = bindOutsideClickToClose(
      els.terminalKeysPopover, els.terminalKeys, closeKeysPopover
    );
  }
}

export function wireKeysPopover() {
  els.terminalKeys.addEventListener('click', function () {
    if (els.terminalKeysPopover.hidden) {
      openKeysPopover();
      // Opening the popover means the user is about to drive a prompt,
      // which lives at the tail — snap to the bottom like the ↓ button.
      const t = state.terminal;
      if (t && t.term) { try { t.term.scrollToBottom(); } catch (_) {} }
    } else {
      closeKeysPopover();
    }
  });
  // Delegated: the popover stays open across arrow/Tab taps so the user
  // can chain `↓ ↓ ↵`; Enter/Esc usually end a prompt, so they close it.
  els.terminalKeysPopover.addEventListener('click', function (ev) {
    const btn = ev.target.closest('.key-btn');
    if (!btn) return;
    const key = btn.getAttribute('data-key');
    // ⇧ toggles the sticky-Shift state and sends nothing on its own; the
    // modifier applies to the next key tap (and stays held for chaining).
    if (key === 'shift') {
      setShiftHeld(!_shiftHeld);
      const t = state.terminal;
      if (t && t.term) t.term.focus();
      return;
    }
    const bytes = (_shiftHeld && SHIFT_KEY_BYTES[key]) || KEY_BYTES[key];
    if (!bytes) return;
    const t = state.terminal;
    if (t && t.ws && t.ws.readyState === WebSocket.OPEN) {
      t.ws.send(JSON.stringify({ type: 'input', data: bytes }));
    }
    if (t && t.term) t.term.focus();
    if (bytes === '\r' || bytes === '\x1b') closeKeysPopover();
  });
}
