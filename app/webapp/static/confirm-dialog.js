/* The confirm sheet: the vendored modal standing in for native
 * confirm() before a destructive action ("the vendored
 * dialog replaces native confirm()"). One <dialog id="confirmDialog"> in
 * index.html serves every caller: a title, one line saying what happens,
 * the header ✕ and one full-width action in the danger tint, or the primary
 * for a step that destroys nothing (the modal contract). The ✕,
 * Escape and a backdrop tap all answer "no".
 *
 * promptDialog() is the same sheet with one text field (in place of
 * native prompt()): it resolves the trimmed text, or null on a "no". The Life
 * OS renames use it too, so every sheet that asks for a name looks one way.
 *
 * Opens over another dialog too (Stop process from the Other ports sheet):
 * showModal() stacks it in the top layer.
 */

import { createSettler } from './modal-settle.js';

function dialogEl() {
  return document.getElementById('confirmDialog');
}

const settler = createSettler(dialogEl, false);
const finish = settler.finish;

// The action. A required field that is empty keeps the sheet open on the
// browser's own "fill out this field" bubble.
function accept() {
  const input = dialogEl().querySelector('#confirmDialogInput');
  if (!input.hidden && !input.reportValidity()) return;
  finish(true);
}

let wired = false;

function wire(d) {
  if (wired) return;
  wired = true;
  d.querySelector('#confirmDialogClose').addEventListener('click', function () {
    finish(false);
  });
  d.querySelector('#confirmDialogOk').addEventListener('click', accept);
  // Enter in the text field is the action, as it was in the native prompt.
  d.querySelector('#confirmDialogInput').addEventListener('keydown', function (ev) {
    if (ev.key !== 'Enter') return;
    ev.preventDefault();
    accept();
  });
  settler.wireDismiss(d);
}

// The action's look: the danger tint for a destructive step (the default),
// the solid primary for one that only starts something new (the Life OS
// handoff), which a red button would misdescribe.
const ACTION_CLASS = {
  danger: 'button-tint danger detail-save-btn',
  primary: 'button-primary detail-save-btn',
};

function open(opts, field) {
  const d = dialogEl();
  if (!d) return Promise.resolve(false);
  wire(d);
  settler.cancelPending();
  d.querySelector('#confirmDialogTitle').textContent = opts.title;
  const message = d.querySelector('#confirmDialogMessage');
  message.textContent = opts.message || '';
  message.hidden = !opts.message;
  const ok = d.querySelector('#confirmDialogOk');
  ok.textContent = opts.action;
  ok.className = ACTION_CLASS[opts.tone] || ACTION_CLASS.danger;
  const input = d.querySelector('#confirmDialogInput');
  input.hidden = !field;
  input.value = field ? field.value || '' : '';
  input.setAttribute('aria-label', field ? field.label : '');
  input.required = !!(field && field.required);
  input.placeholder = (field && field.placeholder) || '';
  if (field && field.maxLength) input.maxLength = field.maxLength;
  else input.removeAttribute('maxlength');
  // 'none' keeps the keyboard from capitalising or correcting a file name.
  const typing = (field && field.typing) || 'none';
  input.setAttribute('autocapitalize', typing);
  input.spellcheck = typing !== 'none';
  return settler.show(d, function () {
    // The starting text is selected, so typing replaces it and the arrow
    // keys edit it.
    if (field) { input.focus(); input.select(); }
  });
}

// opts: title, message (optional), action (the button's text), tone
// ('danger' or 'primary'). Resolves true only when the action was tapped.
export function confirmDialog(opts) {
  return open(opts, null);
}

// opts: as confirmDialog's (message is the help line under the title), plus
// value (the field's starting text), label (its accessible name) and
// optionally placeholder, maxLength, required (an empty field is refused) and
// typing ('none' for a name, 'sentences' for prose). Resolves the trimmed
// text, or null.
export function promptDialog(opts) {
  return open(opts, {
    value: opts.value, label: opts.label, placeholder: opts.placeholder,
    maxLength: opts.maxLength, required: opts.required, typing: opts.typing,
  }).then(function (ok) {
    if (!ok) return null;
    return dialogEl().querySelector('#confirmDialogInput').value.trim();
  });
}
