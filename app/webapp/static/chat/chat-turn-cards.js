/* The Chat pane's cards on a finished turn: the Edited N files card, and
 * the file chips in a reply that open Changed files.
 */

import { els } from '../state.js';
import { sessionTitle } from '../sessions.js';
import { foldEdits, renderEditedCard } from './edited-card.js';
import { openSessionChanges } from '../changes/changes-overlay.js';
import { markSilent } from './transcript-render.js';
import { stepDiff } from './transcript-steps.js';
import { fileParts } from './transcript-text.js';
import { chipFiles, unchipFiles } from './transcript-chips.js';
import { loadOlder, view } from './session-transcript.js';

// --- the Edited N files card -------------------------------------------
//
// It appears once the turn is over: a prompt follows it,
// or it is the newest turn and the activity line does not say the agent is
// working. An activity the server could not establish (null) counts as over:
// the card only summarises loaded steps, so showing it early misleads less
// than hiding it from a turn that has ended. It sits after the turn's parts,
// inside the turn's disclosure, so it collapses with the turn.

// Every step entry the turn holds, in order.
function turnSteps(li) {
  const out = [];
  li._trParts.querySelectorAll(':scope > .tr-run').forEach(function (run) {
    Array.prototype.push.apply(out, run._trRun || []);
  });
  return out;
}

function turnOver(li) {
  if (li.nextElementSibling) return true;
  return !(view && view.activity && view.activity.working);
}

// The turn opens the list and older pages exist, so its first steps may sit
// on one of them.
function turnStartMissing(li) {
  return !li.previousElementSibling && !!view && view.cursor != null;
}

// The files the turn edited, each named as the card names it (`base`, `dir`
// relative to the session's project).
function turnFiles(li) {
  const files = foldEdits(turnSteps(li));
  files.forEach(function (f) {
    const parts = fileParts(f.path, view && view.session);
    f.base = parts.base;
    f.dir = parts.dir;
  });
  return files;
}

// A card tap: the Changed files panel on this turn, focused on
// `focus` (a card row's file) or on none (the header). The turn's files go
// with it, named as the card names them, and how to draw one of its steps.
function openTurnChanges(files, partial, focus) {
  if (!view) return;
  const s = view.session;
  openSessionChanges(s, sessionTitle(s), {
    turn: { files: files, partial: partial, renderStep: function (e) { return stepDiff(e, true, s); } },
    focus: focus ? focus.path : null,
  });
}

// Rebuilt only when what it shows changed: a live tick lands every few
// seconds, and rebuilding an unchanged card would break a tap in flight.
// True when it did change.
function syncEditedCard(li) {
  if (!li._trEditable) return false;
  const files = turnOver(li) ? turnFiles(li) : [];
  const partial = files.length > 0 && turnStartMissing(li);
  const expanded = !!li._trEditedAll;
  const sig = files.length
    ? JSON.stringify([files.map(function (f) { return [f.key, f.status, f.additions, f.deletions]; }), partial, expanded])
    : '';
  if (sig === (li._trEditedSig || '')) return false;
  li._trEditedSig = sig;
  if (li._trEdited) li._trEdited.remove();
  li._trEdited = null;
  if (files.length) {
    li._trEdited = renderEditedCard(files, {
      expanded: expanded,
      partial: partial,
      onOpen: function (file) { openTurnChanges(files, partial, file); },
      onMore: function () {
        li._trEditedAll = true;
        syncEditedCard(li);
      },
      onLoadOlder: function () { loadOlder(); },
    });
    li.querySelector('.tr-turn').appendChild(li._trEdited);
  }
  markSilent(li);
  return true;
}

// Every turn's card, after anything that can change one: new steps, a prompt
// arriving after a turn, the activity line, an older page joining the first.
// True when any card changed.
export function syncEditedCards() {
  if (!view) return false;
  let changed = false;
  els.transcriptList.querySelectorAll(':scope > .tr-agent-item').forEach(function (li) {
    if (syncEditedCard(li)) changed = true;
  });
  return changed;
}

// --- file chips -------------------------------------------------------
//
// transcript-render.js chips a reply's file names; which files count as
// edited, and what a tap opens, are decided here. The edited set is folded
// from every step the page holds, so a file a later tick edits, or an older
// page shows was edited, links when it arrives. A reply's chips are undone
// and redone only when that set changes, never on a tick that brought no new
// file.

// A tap on a chip: if the chip's own turn edited the file, the panel on that
// turn, focused on it, as the turn's card row opens it; otherwise the
// whole session's list, focused on it.
function openFileChip(chip, file) {
  if (!view) return;
  const li = chip.closest('.tr-agent-item');
  const files = li ? turnFiles(li) : [];
  const own = files.find(function (f) { return f.key === file.key; });
  if (own) {
    openTurnChanges(files, turnStartMissing(li), own);
    return;
  }
  const s = view.session;
  openSessionChanges(s, sessionTitle(s), { focus: file.path });
}

// Every reply in the Chat list, against the files the loaded steps edited.
export function syncFileChips() {
  if (!view) return;
  const files = foldEdits(view.entries || []);
  const sig = files.map(function (f) { return f.key; }).sort().join('\n');
  els.transcriptList.querySelectorAll('.tr-reply > .tr-md').forEach(function (body) {
    if (body._trChipSig === sig) return;
    if (body._trChipSig != null) unchipFiles(body);
    body._trChipSig = sig;
    if (files.length) chipFiles(body, files, openFileChip);
  });
}
