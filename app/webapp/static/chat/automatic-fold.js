/* Hide automatic — the Chat pane's fold of turns nobody typed.
 *
 * The server marks a `user` entry with `auto` when the launcher or the harness
 * wrote it (the chief's doorbell line, a compact summary). This module folds
 * each such prompt and the reply that follows into one slim divider row, so a
 * session that runs for days keeps its real conversation readable.
 *
 * A turn is an automatic prompt plus the agent turn after it. Consecutive
 * automatic turns form one group, and a turn holding something that waits on
 * the owner — a question, a plan, a file sent to them — is never folded and
 * ends the group it sits in. Anything the server did not mark stays visible.
 *
 * The group's first list item hosts its divider, so the list keeps the shape
 * the renderer appends to (the item last in the list is still the turn being
 * extended). The pass is idempotent and runs after every change to the list.
 */

const HOST = 'tr-auto-host';
const MEMBER = 'tr-auto-member';
const CLOSED = 'tr-auto-closed';
const STORE_PREFIX = 'launcher.chat.hideAutomatic.';

// Item kinds the grouping reads. `keep` marks a turn that must stay visible.
//   { kind: 'auto' }            an automatic prompt
//   { kind: 'agent', keep }     an agent turn
//   anything else               a human prompt or a marker: a barrier
export function foldGroups(items) {
  const groups = [];
  let open = null;
  let turn = null;
  function endTurn() {
    if (!turn) return;
    if (turn.keep) {
      open = null;
    } else if (open && open.end === turn.start - 1) {
      open.end = turn.end;
      open.turns += 1;
    } else {
      open = { start: turn.start, end: turn.end, turns: 1 };
      groups.push(open);
    }
    turn = null;
  }
  items.forEach(function (item, i) {
    if (item.kind === 'auto') {
      endTurn();
      turn = { start: i, end: i, keep: false };
    } else if (item.kind === 'agent' && turn) {
      turn.end = i;
      turn.keep = turn.keep || !!item.keep;
    } else {
      endTurn();
      open = null;
    }
  });
  endTurn();
  return groups;
}

// Whether the pane starts with automatic turns hidden: the saved choice for
// this session, else on for the chief and off for any other session.
export function automaticDefault(sessionId, isChief) {
  try {
    const saved = window.localStorage.getItem(STORE_PREFIX + sessionId);
    if (saved === '1') return true;
    if (saved === '0') return false;
  } catch (exc) {
    console.debug('hide-automatic preference unreadable', exc);
  }
  return !!isChief;
}

export function rememberAutomatic(sessionId, hidden) {
  try {
    window.localStorage.setItem(STORE_PREFIX + sessionId, hidden ? '1' : '0');
  } catch (exc) {
    console.debug('hide-automatic preference not saved', exc);
  }
}

function itemOf(li) {
  if (li.dataset.trAuto) return { kind: 'auto' };
  if (li.classList.contains('tr-agent-item')) {
    return {
      kind: 'agent',
      keep: !!li._trParts.querySelector(':scope > .tr-ask-item, :scope > .tr-plan-item, :scope > .tr-file-item'),
    };
  }
  return { kind: 'other' };
}

function groupKey(li) {
  const e = li._trEntry;
  if (!e) return null;
  return e.offset != null ? 'o' + e.offset : 't' + e.timestamp;
}

function dividerLabel(turns) {
  return turns + ' automatic turn' + (turns === 1 ? '' : 's');
}

function makeDivider(onToggle) {
  const btn = document.createElement('button');
  btn.dataset.track = 'chat.automatic-divider';
  btn.type = 'button';
  btn.className = 'tr-auto-divider';
  const label = document.createElement('span');
  label.className = 'tr-auto-label';
  btn.appendChild(label);
  const chev = document.createElement('span');
  chev.className = 'tr-auto-chevron';
  chev.setAttribute('aria-hidden', 'true');
  chev.textContent = '›';
  btn.appendChild(chev);
  btn.addEventListener('click', onToggle);
  return btn;
}

function clearFold(li) {
  li.classList.remove(HOST, MEMBER, CLOSED);
  if (li._trAutoDivider) {
    li._trAutoDivider.remove();
    li._trAutoDivider = null;
  }
}

// Fold `list`'s automatic groups. `hidden` is the toggle; `opened` is the set of
// group keys the reader expanded (kept by the caller across list rebuilds).
// Returns nothing; reads and writes only the list's items.
export function syncAutomatic(list, hidden, opened) {
  const lis = Array.prototype.filter.call(list.children, function (n) { return n.tagName === 'LI'; });
  if (!hidden) {
    lis.forEach(clearFold);
    return;
  }
  const groups = foldGroups(lis.map(itemOf));
  const hosts = new Map();
  groups.forEach(function (g) { hosts.set(g.start, g); });
  const covered = new Set();
  groups.forEach(function (g) {
    for (let i = g.start; i <= g.end; i += 1) covered.add(i);
  });
  lis.forEach(function (li, i) {
    if (!covered.has(i)) clearFold(li);
  });
  groups.forEach(function (g) {
    const host = lis[g.start];
    const key = groupKey(host);
    const isOpen = key != null && opened.has(key);
    for (let i = g.start; i <= g.end; i += 1) {
      const li = lis[i];
      li.classList.toggle(MEMBER, i > g.start);
      li.classList.toggle(CLOSED, !isOpen);
      li.classList.toggle(HOST, i === g.start);
      if (i > g.start && li._trAutoDivider) {
        li._trAutoDivider.remove();
        li._trAutoDivider = null;
      }
    }
    let divider = host._trAutoDivider;
    if (!divider) {
      divider = makeDivider(function () {
        const k = groupKey(host);
        if (k == null) return;
        if (opened.has(k)) opened.delete(k);
        else opened.add(k);
        syncAutomatic(list, true, opened);
      });
      host._trAutoDivider = divider;
      host.insertBefore(divider, host.firstChild);
    }
    divider.querySelector('.tr-auto-label').textContent = dividerLabel(g.turns);
    divider.setAttribute('aria-expanded', isOpen ? 'true' : 'false');
  });
}
