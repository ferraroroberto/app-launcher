/* On-screen keys D-pad popover (issue #36).
 *
 * Arrow / Esc / Tab / Enter keys for iPhone keyboards (SwiftKey etc.) that
 * lack them, so an agent's TUI prompts stay navigable from the phone. Each
 * key hands the matching VT/xterm escape sequence to the mounting surface's
 * `send(bytes)` (the terminal writes it over the same WS `input` channel as
 * a composed prompt), and ⇧ is a sticky modifier (issue #137).
 *
 * Split out of terminal.js in issue #723, continuing the #315 split. Since
 * #980 it is mounted by the shared composer (composer.js) — the ⌨ button is
 * the composer grid's second slot and the popover anchors above the
 * composer, where the thumb reaches it — instead of hanging under the
 * terminal bar. The popover renders its own markup so a second composer
 * mount gets its own D-pad.
 */

import { bindOutsideClickToClose } from './dom-utils.js';

const KEY_BYTES = {
  up: '\x1b[A', down: '\x1b[B', right: '\x1b[C', left: '\x1b[D',
  enter: '\r', esc: '\x1b', tab: '\t',
};

// Shift-modified variants (issue #137). The ⇧ key is a sticky toggle that
// simulates holding Shift, so the next key sent uses these sequences. Tab
// becomes back-tab (`\x1b[Z`) — that's Shift+Tab, the way Claude Code cycles
// permission modes — and the arrows get their xterm Shift CSI form (modifier
// 2). Esc/Enter have no standard Shift sequence, so they fall back to the
// plain KEY_BYTES entry.
const SHIFT_KEY_BYTES = {
  tab: '\x1b[Z',
  up: '\x1b[1;2A', down: '\x1b[1;2B', right: '\x1b[1;2C', left: '\x1b[1;2D',
};

const _KEYS_MARKUP =
  '<button type="button" class="key-btn" data-key="esc">Esc</button>' +
  '<button type="button" class="key-btn" data-key="up">↑</button>' +
  '<button type="button" class="key-btn" data-key="tab">Tab</button>' +
  '<button type="button" class="key-btn" data-key="left">←</button>' +
  '<button type="button" class="key-btn" data-key="enter">↵</button>' +
  '<button type="button" class="key-btn" data-key="right">→</button>' +
  '<button type="button" class="key-btn key-shift" data-key="shift" aria-pressed="false">⇧</button>' +
  '<button type="button" class="key-btn" data-key="down">↓</button>' +
  // Empty cell to the right of ⇧ keeps the down-arrow centred in row 3.
  '<span class="key-spacer"></span>';

// Mount one D-pad: `anchor` is the ⌨ button that toggles it, `container`
// the positioned element the popover floats inside (the composer root),
// `opts.send(bytes)` delivers a key, `opts.onOpen()` fires when the popover
// opens (the terminal snaps its scrollback to the tail — the prompt the
// user is about to drive lives there).
export function mountKeysPopover(anchor, container, opts) {
  const pop = document.createElement('div');
  pop.className = 'keys-popover';
  pop.setAttribute('role', 'group');
  pop.setAttribute('aria-label', 'Keyboard keys');
  pop.hidden = true;
  pop.innerHTML = _KEYS_MARKUP;
  container.appendChild(pop);

  let disposeOutsideClick = null;
  // Sticky-Shift state: stays engaged across taps (so ⇧ then Tab Tab Tab
  // cycles modes) until ⇧ is tapped again or the popover closes.
  let shiftHeld = false;
  const shiftBtn = pop.querySelector('.key-shift');

  function setShiftHeld(held) {
    shiftHeld = held;
    shiftBtn.classList.toggle('active', held);
    shiftBtn.setAttribute('aria-pressed', held ? 'true' : 'false');
  }

  function close() {
    pop.hidden = true;
    setShiftHeld(false);
    if (disposeOutsideClick) {
      disposeOutsideClick();
      disposeOutsideClick = null;
    }
  }

  function open() {
    pop.hidden = false;
    if (!disposeOutsideClick) {
      disposeOutsideClick = bindOutsideClickToClose(pop, anchor, close);
    }
    if (opts.onOpen) opts.onOpen();
  }

  anchor.addEventListener('click', function () {
    if (pop.hidden) open(); else close();
  });
  // Delegated: the popover stays open across arrow/Tab taps so the user
  // can chain `↓ ↓ ↵`; Enter/Esc usually end a prompt, so they close it.
  pop.addEventListener('click', function (ev) {
    const btn = ev.target.closest('.key-btn');
    if (!btn) return;
    const key = btn.getAttribute('data-key');
    // ⇧ toggles the sticky-Shift state and sends nothing on its own; the
    // modifier applies to the next key tap (and stays held for chaining).
    if (key === 'shift') {
      setShiftHeld(!shiftHeld);
      return;
    }
    const bytes = (shiftHeld && SHIFT_KEY_BYTES[key]) || KEY_BYTES[key];
    if (!bytes) return;
    opts.send(bytes);
    if (bytes === '\r' || bytes === '\x1b') close();
  });

  return {
    element: pop,
    close: close,
    isOpen: function () { return !pop.hidden; },
  };
}
