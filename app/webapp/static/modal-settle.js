/* The promise half of a modal sheet: one answer, settled once.
 *
 * Both promise-returning sheets (confirm-dialog.js, the Life OS rename sheet)
 * hand a caller a promise that settles when the sheet closes. A sheet's own
 * action calls finish(answer); ✕, Escape and a backdrop tap all settle with
 * `cancelAnswer`.
 */

export function createSettler(getDialog, cancelAnswer) {
  let resolveCurrent = null;

  function finish(answer) {
    const d = getDialog();
    const resolve = resolveCurrent;
    resolveCurrent = null;
    if (d && d.open) d.close();
    if (resolve) resolve(answer);
  }

  return {
    finish,
    // Settles an answer still waiting on the dialog (a second sheet opened
    // over its own) as a cancel, before the dialog is refilled.
    cancelPending() {
      if (resolveCurrent) finish(cancelAnswer);
    },
    // Opens the dialog modally; resolves with the answer finish() settles.
    show(d, afterShow) {
      return new Promise(function (resolve) {
        resolveCurrent = resolve;
        d.showModal();
        if (afterShow) afterShow();
      });
    },
    // Escape closes the dialog natively; any close that was not an action is
    // a cancel. The event is queued, so one from an earlier answer can land
    // after the next sheet has opened: an open dialog ignores it. A tap on
    // the backdrop lands on the <dialog> itself, outside the card.
    wireDismiss(d) {
      d.addEventListener('close', function () {
        if (!d.open) finish(cancelAnswer);
      });
      d.addEventListener('click', function (ev) {
        if (ev.target === d) finish(cancelAnswer);
      });
    },
  };
}
