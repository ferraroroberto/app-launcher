/* On-screen keys D-pad popover (issue #36).
 *
 * Arrow / Esc / Tab / Enter keys for iPhone keyboards (SwiftKey etc.) that
 * lack them, so an agent's TUI prompts stay navigable from the phone. Each
 * key hands the matching VT/xterm escape sequence to the mounting surface's
 * `send(bytes)` (the terminal writes it over the same WS `input` channel as
 * a composed prompt). ⇧ and Ctrl are sticky modifiers (issue #137, #986) —
 * mutually exclusive, engaging one releases the other.
 *
 * Split out of terminal.js in issue #723, continuing the #315 split. Since
 * #980 it is mounted by the shared composer (composer.js) — the ⌨ button is
 * the composer grid's second slot and the popover anchors above the
 * composer, where the thumb reaches it — instead of hanging under the
 * terminal bar. The popover renders its own markup so a second composer
 * mount gets its own D-pad. The key -> bytes tables are pure data in
 * terminal-keys-bytes.js so they are unit-testable without a browser.
 */

import { bindOutsideClickToClose } from './dom-utils.js';
import { KEY_BYTES, SHIFT_KEY_BYTES, CTRL_KEY_BYTES } from './terminal-keys-bytes.js';

const _KEYS_MARKUP =
  '<button type="button" class="key-btn" data-key="esc">Esc</button>' +
  '<button type="button" class="key-btn" data-key="up">↑</button>' +
  '<button type="button" class="key-btn" data-key="tab">Tab</button>' +
  '<button type="button" class="key-btn key-ctrl" data-key="ctrl" aria-pressed="false">Ctrl</button>' +
  '<button type="button" class="key-btn" data-key="left">←</button>' +
  '<button type="button" class="key-btn" data-key="enter">↵</button>' +
  '<button type="button" class="key-btn" data-key="right">→</button>' +
  '<button type="button" class="key-btn" data-key="c" disabled>C</button>' +
  '<button type="button" class="key-btn key-shift" data-key="shift" aria-pressed="false">⇧</button>' +
  '<button type="button" class="key-btn" data-key="down">↓</button>' +
  '<button type="button" class="key-btn" data-key="u" disabled>U</button>' +
  '<button type="button" class="key-btn" data-key="x" disabled>X</button>';

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
  // Sticky-Shift / sticky-Ctrl state: each stays engaged across taps (so ⇧
  // then Tab Tab Tab cycles modes, or Ctrl then C C interrupts twice) until
  // its own key is tapped again, the popover closes, or the other modifier
  // is engaged (issue #986 — ⇧ and Ctrl are mutually exclusive here).
  let shiftHeld = false;
  let ctrlHeld = false;
  const shiftBtn = pop.querySelector('.key-shift');
  const ctrlBtn = pop.querySelector('.key-ctrl');
  const ctrlLetterBtns = pop.querySelectorAll('[data-key="c"], [data-key="u"], [data-key="x"]');

  function setShiftHeld(held) {
    shiftHeld = held;
    shiftBtn.classList.toggle('active', held);
    shiftBtn.setAttribute('aria-pressed', held ? 'true' : 'false');
    if (held && ctrlHeld) setCtrlHeld(false);
  }

  function setCtrlHeld(held) {
    ctrlHeld = held;
    ctrlBtn.classList.toggle('active', held);
    ctrlBtn.setAttribute('aria-pressed', held ? 'true' : 'false');
    ctrlLetterBtns.forEach(function (btn) { btn.disabled = !held; });
    if (held && shiftHeld) setShiftHeld(false);
  }

  function close() {
    pop.hidden = true;
    setShiftHeld(false);
    setCtrlHeld(false);
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
    // ⇧ / Ctrl toggle their own sticky state and send nothing on their own;
    // the modifier applies to the next key tap (and stays held for
    // chaining — e.g. Ctrl then C twice to interrupt again).
    if (key === 'shift') {
      setShiftHeld(!shiftHeld);
      return;
    }
    if (key === 'ctrl') {
      setCtrlHeld(!ctrlHeld);
      return;
    }
    const bytes = (ctrlHeld && CTRL_KEY_BYTES[key]) ||
      (shiftHeld && SHIFT_KEY_BYTES[key]) || KEY_BYTES[key];
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
