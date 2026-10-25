/* Screen view's keys: the keys bar and the hidden input that raises the
 * phone keyboard, a hardware keyboard's keys, and the clipboard keys.
 * Every key goes out through screen-control.js's sendInput, so nothing is
 * sent in View mode or while the picture is not live.
 *
 * Clipboard. Two keys on the keys bar send the phone's clipboard text
 * to the PC: Paste to PC sets the PC's clipboard, Paste + Ctrl+V sets it and
 * then presses Ctrl+V. The text is read inside the tap (iOS asks for its own
 * Paste confirmation there) and goes out as one `clipboard_set` message
 * through sendInput, so it is refused in View mode and while the picture is
 * not live like any other input; if the phone refuses the read, a small sheet
 * takes the text by hand. The server answers each message with a `clipboard`
 * message (ok, or why not), shown as a short line over the picture. Nothing
 * here keeps, logs or counts what was copied.
 */

import { allowKeyRepeat, comboKey, hardwareKey } from './screen-gestures.js';
import { applyZoom, canSend, sendInput } from './screen-control.js';
import { $, view } from './screen-state.js';

// The sentinel the hidden input holds, so Backspace on an "empty" field
// still fires an input event to forward.
const KEY_SENTINEL = ' ';

// ---------------------------------------------------------------- keyboard

// The keyboard button's own toggle, which a three-finger tap shares.
export function toggleKeys() {
  const keys = $('screenKeys');
  setKeysOpen(!keys || keys.hidden);
}

export function setKeysOpen(open) {
  const keys = $('screenKeys');
  const kb = $('screenKeyboard');
  const input = $('screenKeyInput');
  const dialog = $('screenView');
  if (!keys) return;
  keys.hidden = !open;
  if (dialog) dialog.classList.toggle('keys-open', open);
  if (kb) kb.setAttribute('aria-pressed', open ? 'true' : 'false');
  if (input) {
    if (open) {
      // Inside the tap's own handler, so iOS raises the keyboard.
      input.focus({ preventScroll: true });
      resetInput(input);
    } else {
      input.blur();
    }
  }
  if (!open) {
    clearMods();
    setKeysPage('main');
  }
  fitToKeyboard();
  applyZoom();
}

// The sentinel, with the caret after it, so a Backspace always has
// something to delete and fires.
function resetInput(input) {
  input.value = KEY_SENTINEL;
  try { input.setSelectionRange(KEY_SENTINEL.length, KEY_SENTINEL.length); } catch (_) { /* not focusable yet */ }
}

function clearMods() {
  view.mods.clear();
  renderMods();
}

function renderMods() {
  const keys = $('screenKeys');
  if (!keys) return;
  keys.querySelectorAll('[data-mod]').forEach(function (btn) {
    const held = view.mods.has(btn.dataset.mod);
    btn.classList.toggle('active', held);
    btn.setAttribute('aria-pressed', held ? 'true' : 'false');
  });
  // The F-keys page has no modifier keys of its own: its way back names
  // the ones still held, so Ctrl + F5 can be checked before the F5.
  const names = MOD_ORDER.filter(function (m) { return view.mods.has(m); })
    .map(function (m) { return MOD_LABEL[m]; });
  keys.querySelectorAll('[data-held]').forEach(function (el) {
    el.textContent = names.join('+');
    el.hidden = !names.length;
  });
}

// Which page of keys is up: 'main', the original keys, or 'fn', the
// F-keys and the navigation cluster. view.mods is not touched, so a modifier
// tapped on one page is still held on the other.
function setKeysPage(page) {
  const keys = $('screenKeys');
  if (!keys) return;
  keys.querySelectorAll('[data-keys-page]').forEach(function (el) {
    el.hidden = el.dataset.keysPage !== page;
  });
}

// Client-side macros: one key message, no server change. Task Manager is
// Ctrl+Shift+Esc; Ctrl+Alt+Del is the secure attention sequence and cannot be
// sent by an app. A macro sends exactly its own modifiers, so held sticky ones
// are not mixed in; they let go with it, as they do after any key.
const MACROS = {
  taskmgr: { key: 'esc', mods: ['ctrl', 'shift'] },
};
const MOD_ORDER = ['ctrl', 'alt', 'shift', 'win'];
const MOD_LABEL = { ctrl: 'Ctrl', alt: 'Alt', shift: 'Shift', win: 'Win' };

function sendMacro(name) {
  const macro = MACROS[name];
  if (!macro) return;
  if (sendInput({ type: 'key', key: macro.key, mods: macro.mods.slice() }) && view.mods.size) clearMods();
}

// A key with the sticky modifiers, which then let go: one key each.
function sendKey(key) {
  const mods = Array.from(view.mods);
  if (sendInput({ type: 'key', key: key, mods: mods }) && mods.length) clearMods();
}

function sendTyped(text) {
  if (!text) return;
  const combo = view.mods.size ? comboKey(text) : null;
  if (combo) {
    sendKey(combo);
    return;
  }
  if (sendInput({ type: 'text', text: text })) clearMods();
}

export function wireKeyboard() {
  const kb = $('screenKeyboard');
  const keys = $('screenKeys');
  const input = $('screenKeyInput');
  if (kb) {
    kb.addEventListener('click', toggleKeys);
  }
  if (keys) {
    // Keep the phone keyboard up: a key tap must not take focus off the
    // hidden input.
    keys.addEventListener('pointerdown', function (e) {
      if (e.target.closest('.key-btn')) e.preventDefault();
    });
    keys.addEventListener('click', function (e) {
      const btn = e.target.closest('.key-btn');
      if (!btn || btn.disabled) return;
      if (input && document.activeElement !== input) {
        input.focus({ preventScroll: true });
        resetInput(input);
      }
      if (btn.dataset.mod) {
        const mod = btn.dataset.mod;
        if (view.mods.has(mod)) view.mods.delete(mod); else view.mods.add(mod);
        renderMods();
        return;
      }
      if (btn.dataset.pageTo) {
        setKeysPage(btn.dataset.pageTo);
        return;
      }
      if (btn.dataset.macro) {
        sendMacro(btn.dataset.macro);
        return;
      }
      if (btn.dataset.clip) {
        pasteToPc(btn.dataset.clip === 'paste');
        return;
      }
      if (btn.dataset.key) sendKey(btn.dataset.key);
    });
  }
  if (!input) return;
  // beforeinput, not keydown, for text: a phone keyboard's IME reports keyCode 229 for
  // every key, but names what it does here. Composed text (a dictation, a
  // CJK or swipe IME) can't be cancelled mid-flight; it goes out whole at
  // compositionend.
  input.addEventListener('beforeinput', function (e) {
    if (e.isComposing || e.inputType === 'insertCompositionText') return;
    e.preventDefault();
    switch (e.inputType) {
      case 'insertText':
        sendTyped(e.data || '');
        break;
      case 'insertLineBreak':
      case 'insertParagraph':
        sendKey('enter');
        break;
      case 'deleteContentBackward':
        sendKey('backspace');
        break;
      case 'deleteContentForward':
        sendKey('delete');
        break;
      default:
        break;
    }
  });
  input.addEventListener('compositionend', function (e) {
    sendTyped(e.data || '');
    resetInput(input);
  });
  // An edit that skipped beforeinput (some keyboards insert text without
  // one): forward what changed against the sentinel, then put it back. A
  // cancelled beforeinput fires no input event, so nothing goes twice.
  input.addEventListener('input', function (e) {
    if (e.isComposing || input.value === KEY_SENTINEL) return;
    const value = input.value;
    if (value.startsWith(KEY_SENTINEL)) sendTyped(value.slice(KEY_SENTINEL.length));
    else if (value === '') sendKey('backspace');
    resetInput(input);
  });
}

// ------------------------------------------------------ hardware keyboard

// A hardware keyboard (an iPad's, or the pop-out window's) presses keys the
// hidden input's beforeinput never sees: arrows, Esc, Tab, F-keys, Ctrl
// shortcuts. keydown carries them, where screen-gestures.js's
// hardwareKey decides what each one is; a plain printable character is left
// to beforeinput so nothing goes twice, and so is a phone IME's composition
// (keyCode 229). Only a key that was sent is cancelled, so a View-mode Esc
// still closes the dialog and an unsent F5 still reloads.
const keyRepeat = { count: 0, at: 0 };

function onHardwareKeydown(e) {
  const hit = hardwareKey(e);
  if (!hit || !canSend()) return;
  const target = e.target;
  const tag = target && target.tagName;
  // A focused field or button keeps its own typing keys; Enter and Space on a
  // button press it.
  if ((tag === 'INPUT' && target.id !== 'screenKeyInput') || tag === 'TEXTAREA' || tag === 'SELECT') return;
  if (tag === 'BUTTON' && !hit.mods.length && (hit.key === 'enter' || hit.key === 'space')) return;
  if (e.repeat) {
    keyRepeat.count += 1;
    const t = Date.now();
    const allowed = allowKeyRepeat(keyRepeat.count, t - keyRepeat.at);
    if (allowed) keyRepeat.at = t;
    e.preventDefault();
    if (!allowed) return;
  } else {
    keyRepeat.count = 0;
    keyRepeat.at = Date.now();
  }
  // The bar's sticky modifiers combine with a hardware key, as with typing.
  const mods = MOD_ORDER.filter(function (m) { return hit.mods.includes(m) || view.mods.has(m); });
  if (sendInput({ type: 'key', key: hit.key, mods: mods })) {
    e.preventDefault();
    if (view.mods.size) clearMods();
  }
}

export function wireHardwareKeyboard() {
  const dialog = $('screenView');
  if (dialog) dialog.addEventListener('keydown', onHardwareKeydown);
}

// The phone keyboard covers the bottom of the screen without resizing the
// layout viewport, so while the keys are up the view follows the visual
// viewport and the keys bar sits on top of the keyboard.
export function fitToKeyboard() {
  const dialog = $('screenView');
  const vv = window.visualViewport;
  if (!dialog) return;
  const keys = $('screenKeys');
  if (!vv || !keys || keys.hidden || !view.open) {
    dialog.style.top = '';
    dialog.style.height = '';
    return;
  }
  dialog.style.top = vv.offsetTop + 'px';
  dialog.style.height = vv.height + 'px';
}

// ----------------------------------------------------------- clipboard

// The most text one transfer carries, in UTF-8 bytes: the server's own cap
// (src/desktop_clipboard.py MAX_CLIPBOARD_BYTES), checked here first so a
// huge paste is never sent just to be refused.
const CLIPBOARD_MAX_BYTES = 64 * 1024;
const CLIPBOARD_FAILURES = {
  busy: "The PC's clipboard is busy. Try again.",
  too_large: 'That text is too long to send (64 KB at most).',
  empty: 'There was no text to send.',
  error: "The PC's clipboard refused the text.",
};
const clipQueue = [];     // one entry per clipboard_set in flight: press Ctrl+V once it lands?
let noticeTimer = null;
let pasteSheetThenPaste = false;

// A short result line over the picture. The app's toast sits behind a modal
// dialog's top layer, so the view carries its own.
function showNotice(message, isError) {
  const el = $('screenNotice');
  if (!el) return;
  el.textContent = message;
  el.classList.toggle('error', !!isError);
  el.hidden = false;
  if (noticeTimer) clearTimeout(noticeTimer);
  noticeTimer = setTimeout(function () {
    noticeTimer = null;
    el.hidden = true;
  }, isError ? 4500 : 2200);
}

// A new connection or a closed one owes no answers, and shows no old one.
export function resetClipboard() {
  clipQueue.length = 0;
  if (noticeTimer) clearTimeout(noticeTimer);
  noticeTimer = null;
  const el = $('screenNotice');
  if (el) el.hidden = true;
}

// The key's tap. readText() is called straight from the tap, before anything
// is awaited, so the phone treats it as the user's own action.
function pasteToPc(thenPaste) {
  if (!canSend()) return;
  const clip = navigator.clipboard;
  if (!clip || typeof clip.readText !== 'function') {
    openPasteSheet(thenPaste);
    return;
  }
  clip.readText().then(function (text) {
    if (!text) {
      showNotice("The phone's clipboard has no text");
      return;
    }
    sendClipboard(text, thenPaste);
  }, function () {
    // Refused (permission denied, or the page was not trusted with it).
    openPasteSheet(thenPaste);
  });
}

function sendClipboard(text, thenPaste) {
  if (new TextEncoder().encode(text).length > CLIPBOARD_MAX_BYTES) {
    showNotice(CLIPBOARD_FAILURES.too_large, true);
    return;
  }
  if (sendInput({ type: 'clipboard_set', text: text })) {
    clipQueue.push(!!thenPaste);
  } else {
    showNotice('Wait for the picture to be live, in Control, to paste.', true);
  }
}

// The server's answer to one clipboard_set, in the order they were sent.
export function onClipboard(msg) {
  const thenPaste = clipQueue.shift() === true;
  if (!msg.ok) {
    showNotice(CLIPBOARD_FAILURES[msg.reason] || CLIPBOARD_FAILURES.error, true);
    return;
  }
  // Ctrl+V only after the copy landed: a failed copy must never paste what
  // the PC's clipboard held before. Exactly Ctrl, like a macro: held sticky
  // modifiers let go with it.
  if (thenPaste && sendInput({ type: 'key', key: 'v', mods: ['ctrl'] })) {
    if (view.mods.size) clearMods();
    showNotice('Pasted on the PC');
    return;
  }
  showNotice('Sent to the PC clipboard');
}

function openPasteSheet(thenPaste) {
  const sheet = $('screenPasteSheet');
  if (!sheet) return;
  pasteSheetThenPaste = !!thenPaste;
  const field = $('screenPasteText');
  if (field) field.value = '';
  if (!sheet.open) sheet.showModal();
}

export function closePasteSheet() {
  const sheet = $('screenPasteSheet');
  if (sheet && sheet.open) sheet.close();
}

export function wirePasteSheet() {
  const sheet = $('screenPasteSheet');
  if (!sheet) return;
  const close = $('screenPasteClose');
  if (close) close.addEventListener('click', closePasteSheet);
  const form = $('screenPasteForm');
  if (form) {
    form.addEventListener('submit', function (e) {
      e.preventDefault();
      const field = $('screenPasteText');
      const text = field ? field.value : '';
      closePasteSheet();
      if (text) sendClipboard(text, pasteSheetThenPaste);
    });
  }
  // A tap on the backdrop lands on the <dialog> itself, outside the card.
  sheet.addEventListener('click', function (e) {
    if (e.target === sheet) closePasteSheet();
  });
}
