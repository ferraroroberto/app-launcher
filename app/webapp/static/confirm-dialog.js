/* The confirm sheet (#1437): the vendored modal standing in for native
 * confirm() before a destructive action (round 2 of #1432, "the vendored
 * dialog replaces native confirm()"). One <dialog id="confirmDialog"> in
 * index.html serves every caller: a title, one line saying what happens,
 * the header ✕ and one full-width action in the danger tint, or the primary
 * for a step that destroys nothing (the modal contract, #545). The ✕,
 * Escape and a backdrop tap all answer "no".
 *
 * promptDialog() is the same sheet with one text field (#1501, in place of
 * native prompt()): it resolves the trimmed text, or null on a "no".
 *
 * Opens over another dialog too (Stop process from the Other ports sheet):
 * showModal() stacks it in the top layer.
 */

let settle = null;

function dialogEl() {
  return document.getElementById('confirmDialog');
}

function finish(answer) {
  const d = dialogEl();
  const resolve = settle;
  settle = null;
  if (d && d.open) d.close();
  if (resolve) resolve(answer);
}

let wired = false;

function wire(d) {
  if (wired) return;
  wired = true;
  d.querySelector('#confirmDialogClose').addEventListener('click', function () {
    finish(false);
  });
  d.querySelector('#confirmDialogOk').addEventListener('click', function () {
    finish(true);
  });
  // Enter in the text field is the action, as it was in the native prompt.
  d.querySelector('#confirmDialogInput').addEventListener('keydown', function (ev) {
    if (ev.key !== 'Enter') return;
    ev.preventDefault();
    finish(true);
  });
  // Escape closes the dialog natively; any close that was not the action
  // is a "no". The event is queued, so one from an earlier answer can land
  // after the next confirm has opened: an open dialog ignores it.
  d.addEventListener('close', function () {
    if (!d.open) finish(false);
  });
  // A tap on the backdrop lands on the <dialog> itself, outside the card.
  d.addEventListener('click', function (ev) {
    if (ev.target === d) finish(false);
  });
}

// The action's look: the danger tint for a destructive step (the default),
// the solid primary for one that only starts something new (the Life OS
// handoff, #1439), which a red button would misdescribe.
const ACTION_CLASS = {
  danger: 'button-tint danger detail-save-btn',
  primary: 'button-primary detail-save-btn',
};

function open(opts, field) {
  const d = dialogEl();
  if (!d) return Promise.resolve(false);
  wire(d);
  if (settle) finish(false);
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
  return new Promise(function (resolve) {
    settle = resolve;
    d.showModal();
    if (field) input.select();
  });
}

// opts: title, message (optional), action (the button's text), tone
// ('danger' or 'primary'). Resolves true only when the action was tapped.
export function confirmDialog(opts) {
  return open(opts, null);
}

// opts: as confirmDialog's, plus value (the field's starting text) and
// label (its accessible name). Resolves the trimmed text, or null.
export function promptDialog(opts) {
  return open(opts, { value: opts.value, label: opts.label }).then(function (ok) {
    if (!ok) return null;
    return dialogEl().querySelector('#confirmDialogInput').value.trim();
  });
}
