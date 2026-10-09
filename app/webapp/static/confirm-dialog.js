/* The confirm sheet (#1437): the vendored modal standing in for native
 * confirm() before a destructive action (round 2 of #1432, "the vendored
 * dialog replaces native confirm()"). One <dialog id="confirmDialog"> in
 * index.html serves every caller: a title, one line saying what happens,
 * the header ✕ and one full-width action in the danger tint (the modal
 * contract, #545). The ✕, Escape and a backdrop tap all answer "no".
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

// opts: title, message, action (the button's text). Resolves true only when
// the action button was tapped.
export function confirmDialog(opts) {
  const d = dialogEl();
  if (!d || !d.showModal) return Promise.resolve(false);
  wire(d);
  if (settle) finish(false);
  d.querySelector('#confirmDialogTitle').textContent = opts.title;
  d.querySelector('#confirmDialogMessage').textContent = opts.message || '';
  d.querySelector('#confirmDialogOk').textContent = opts.action;
  return new Promise(function (resolve) {
    settle = resolve;
    d.showModal();
  });
}
