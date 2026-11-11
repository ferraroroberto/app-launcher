/* The Chat pane's queued chip: messages this session holds that the agent
 * has not taken yet, and the sheet that lists them.
 *
 * The list rides every transcript response as `pending_input` (the
 * session-host's word, src/session_hosting/pending_input.py), minus what the
 * transcript already shows as taken (send-confirm.js). A message waits in
 * one of two places: the agent's input box, while the session-host waits
 * for the moment to press Enter, or the agent's own queue, after Enter went
 * in mid-turn. Discard asks the session-host to remove it; it presses keys
 * only when the screen says they can reach nothing but the composer, and
 * otherwise answers why not, which is what the toast says.
 */

import { els } from '../state.js';
import { apiFailToast, sessionRoute, toast } from '../api.js';
import { terminalJsonApi } from '../webauthn.js';
import { pendingNotTaken } from './send-confirm.js';
import { scheduleLive, view } from './session-transcript.js';
import { PendingState, isUnconfirmed, pendingState } from './pending-state.js';

const STATE_LABEL = {
  [PendingState.IN_COMPOSER]: 'In the agent’s input box',
  [PendingState.QUEUED]: 'Queued in the agent',
  [PendingState.DISCARDING]: 'Clearing from the input box',
};

// Why an entry has no Discard button, by state.
const FIXED_NOTE = {
  [PendingState.IN_COMPOSER]: 'Can’t be removed from here',
  [PendingState.QUEUED]: 'Can’t be removed from here',
  [PendingState.DISCARDING]: 'Being cleared, it won’t be sent',
};

// The rows the open sheet was last built from (see sheetKey).
let renderedKey = null;

function timeOf(at) {
  const d = new Date(Number(at) * 1000);
  return Number.isNaN(d.getTime())
    ? ''
    : d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

// What the pane shows: the session's pending messages not yet taken, less
// any this pane discarded, which a read already in flight at the discard
// could still list. Entry ids are never reused.
export function visiblePending() {
  if (!view) return [];
  const gone = view.discardedIds || [];
  return pendingNotTaken(view.pendingInput, view.entries).filter(function (p) {
    return gone.indexOf(p.id) === -1;
  });
}

// The chip, and the sheet's rows while it is open. Called after every read.
export function syncPendingChip() {
  const row = els.chatQueued;
  if (!row) return;
  const list = visiblePending();
  row.hidden = list.length === 0;
  if (list.length) {
    const noun = list.length === 1 ? 'message' : 'messages';
    els.chatQueuedCount.textContent = list.length + ' queued';
    els.chatQueuedChip.setAttribute(
      'aria-label', list.length + ' ' + noun + ' waiting for the agent, show them'
    );
  }
  if (els.pendingInputDialog && els.pendingInputDialog.open) renderSheet(list);
}

function rowFor(entry) {
  const li = document.createElement('li');
  li.className = 'pending-row';
  li.dataset.id = entry.id;
  const meta = document.createElement('div');
  meta.className = 'pending-meta';
  const unconfirmed = isUnconfirmed(entry);
  meta.textContent = [
    unconfirmed ? 'Sent while the agent works' : (STATE_LABEL[entry.state] || entry.state),
    timeOf(entry.at),
  ].filter(Boolean).join(' · ');
  const text = document.createElement('p');
  text.className = 'pending-text';
  text.textContent = entry.text + (entry.truncated ? '…' : '');
  li.append(meta, text);
  if (entry.removable) {
    const discard = document.createElement('button');
    discard.dataset.track = 'pending.discard';
    discard.type = 'button';
    discard.className = 'button-tint danger pending-discard';
    discard.textContent = 'Discard';
    discard.addEventListener('click', function () { discardEntry(entry, discard); });
    li.appendChild(discard);
  } else {
    const fixed = document.createElement('p');
    fixed.className = 'pending-fixed';
    fixed.textContent = unconfirmed
      ? 'Checking the agent queued it'
      : (FIXED_NOTE[pendingState(entry)] || FIXED_NOTE[PendingState.QUEUED]);
    li.appendChild(fixed);
  }
  return li;
}

// The agent holds its queue, and the input box its text, as one block: a
// discard takes every removable entry in the same place with it.
function togetherNote(list) {
  const counts = {};
  list.forEach(function (e) { if (e.removable) counts[e.state] = (counts[e.state] || 0) + 1; });
  const n = Math.max(counts[PendingState.QUEUED] || 0, counts[PendingState.IN_COMPOSER] || 0);
  return n > 1
    ? 'Messages in the same place leave the agent together: Discard removes all ' + n + '.'
    : '';
}

// What the sheet shows, as one string: a poll that returns the same rows
// leaves the DOM alone, so a long message keeps its scroll offset and its
// text selection.
function sheetKey(list) {
  return JSON.stringify(list.map(function (e) {
    return [e.id, e.state, e.confirmed, e.removable, e.truncated, e.text, e.at];
  }));
}

// A rebuilt row starts at the top, so the offsets of every element that
// scrolls inside the sheet (the dialog, its card, each message's own capped
// text box) are read before the swap and put back after it.
function renderSheet(list, force) {
  const key = sheetKey(list);
  if (!force && key === renderedKey) return;
  renderedKey = key;
  const dialog = els.pendingInputDialog;
  const frames = [dialog, dialog.querySelector('.detail-card')].filter(Boolean);
  const frameTops = frames.map(function (el) { return el.scrollTop; });
  const textTops = {};
  els.pendingInputList.querySelectorAll('.pending-row').forEach(function (li) {
    const box = li.querySelector('.pending-text');
    if (box) textTops[li.dataset.id] = box.scrollTop;
  });
  const items = list.map(rowFor);
  els.pendingInputList.replaceChildren(...items);
  els.pendingInputEmpty.hidden = list.length > 0;
  const note = togetherNote(list);
  els.pendingInputNote.textContent = note;
  els.pendingInputNote.hidden = !note;
  frames.forEach(function (el, i) { el.scrollTop = frameTops[i]; });
  items.forEach(function (li) {
    const box = li.querySelector('.pending-text');
    if (box && textTops[li.dataset.id]) box.scrollTop = textTops[li.dataset.id];
  });
}

function openSheet() {
  const d = els.pendingInputDialog;
  if (!d || !view) return;
  renderSheet(visiblePending(), true);
  if (!d.open) d.showModal();
}

async function discardEntry(entry, button) {
  const target = view;
  if (!target) return;
  button.disabled = true;
  button.textContent = 'Discarding…';
  let body;
  try {
    body = await terminalJsonApi(
      sessionRoute(target.session.session_id, '/pending-input/' + encodeURIComponent(entry.id)),
      { method: 'DELETE' }
    );
  } catch (exc) {
    if (exc && exc.status === 404) {
      // The agent took it between the read and the tap.
      forget(target, [entry.id]);
      toast('Already taken by the agent', '', { icon: 'send-horizontal' });
    } else {
      apiFailToast('Not discarded', exc, { icon: 'trash-2' });
      button.disabled = false;
      button.textContent = 'Discard';
    }
    return;
  }
  const ids = (body && body.ids) || [entry.id];
  if (body && body.reason === 'discarding') {
    (target.pendingInput || []).forEach(function (p) {
      if (ids.indexOf(p.id) !== -1) { p.state = PendingState.DISCARDING; p.removable = false; }
    });
  } else {
    forget(target, ids);
  }
  toast((body && body.detail) || 'Discarded', 'good', { icon: 'trash-2' });
  if (view === target) {
    syncPendingChip();
    scheduleLive(0);
  }
}

function forget(target, ids) {
  target.discardedIds = (target.discardedIds || []).concat(ids);
  if (view === target) syncPendingChip();
}

// The pane closed or moved to another session: nothing it listed applies.
export function closePendingSheet() {
  if (els.chatQueued) els.chatQueued.hidden = true;
  if (els.pendingInputDialog && els.pendingInputDialog.open) els.pendingInputDialog.close();
}

export function wirePendingChip() {
  if (!els.chatQueuedChip || !els.pendingInputDialog) return;
  els.chatQueuedChip.addEventListener('click', openSheet);
  const close = function () { els.pendingInputDialog.close(); };
  els.pendingInputClose.addEventListener('click', close);
  els.pendingInputDone.addEventListener('click', close);
  els.pendingInputDialog.addEventListener('click', function (ev) {
    if (ev.target === els.pendingInputDialog) close();
  });
}
