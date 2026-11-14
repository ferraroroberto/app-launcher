// The Board's one collapse-all / expand-all control. It drives the same open
// state the chevrons use: the lane cards are static <details> whose `open` the
// poll never touches, and the nested groups are foldGroup() <details> whose
// remembered choice setAllFolds() resets, so a later single tap still works
// and a rebuild after the poll keeps what the toggle set.

import { setAllFolds } from './chief-plan.js';

const COLLAPSE = { fold: 'collapse', glyph: 'chevrons-down-up', label: 'Collapse all cards' };
const EXPAND = { fold: 'expand', glyph: 'chevrons-up-down', label: 'Expand all cards' };

// A card that is open and not hidden inside a closed card or a hidden lane:
// what the owner can actually see unfolded.
function visiblyOpen(details) {
  if (!details.open || details.closest('[hidden]')) return false;
  for (let p = details.parentElement && details.parentElement.closest('details'); p;
    p = p.parentElement && p.parentElement.closest('details')) {
    if (!p.open) return false;
  }
  return true;
}

// The control offers "Collapse all" while anything on the Board is visibly
// open, "Expand all" once everything is folded.
export function syncFoldAll(button, root) {
  if (!button || !root) return;
  const anyOpen = Array.from(root.querySelectorAll('details')).some(visiblyOpen);
  const next = anyOpen ? COLLAPSE : EXPAND;
  button.dataset.fold = next.fold;
  button.title = next.label;
  button.setAttribute('aria-label', next.label);
  const use = button.querySelector('use');
  if (use) use.setAttribute('href', '#i-' + next.glyph);
}

export function wireFoldAll(button, root) {
  if (!button || !root) return;
  button.addEventListener('click', function () {
    const open = button.dataset.fold === 'expand';
    setAllFolds(open);
    root.querySelectorAll('details').forEach(function (d) { d.open = open; });
    syncFoldAll(button, root);
  });
  // `toggle` does not bubble; the capture phase still sees every chevron tap.
  root.addEventListener('toggle', function () { syncFoldAll(button, root); }, true);
  syncFoldAll(button, root);
}
