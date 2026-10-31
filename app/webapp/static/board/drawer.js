/* The Board's drill-down drawer: tapping a live session row opens it on the
 * card, with the last user↔assistant exchange, the shared composer, Chat and
 * Terminal, and Rename and Stop in its kebab.
 *
 * board.js draws the rows and owns the render loop; renderBoard() keeps the
 * open drawer's own <li> across a re-render (openDrawerItem) and tells this
 * module, every render, whether it kept one (syncDrawerLifecycle), so the
 * chief's exchange poll and a live mic never outlive their DOM node.
 */

import { els, state } from '../state.js';
import { apiFailToast, toast } from '../api.js';
import {
  detachedSendRefused,
  openSessionRename,
  sendOutcome,
  sendSessionMessage,
  stopSession,
} from '../sessions.js';
import { openTerminal } from '../terminal.js';
import { chatAvailable, modeReason, openSessionOverlay, terminalAvailable } from '../session-overlay.js';
import { mountComposer } from '../composer.js';
import { uploadSessionFile } from '../terminal/terminal-compose.js';
import { voiceDictationAvailable } from '../voice.js';
import { icon } from '../_vendored/icons/icons.js';
import { terminalJsonApi } from '../webauthn.js';
import { CHIEF_KILL_CONFIRM, channelKillConfirm, isChannelSession } from '../shared/session-kinds.js';
import { renderMarkdown } from '../markdown.js';
import { confirmDialog } from '../confirm-dialog.js';
import { createRowMenu } from '../_vendored/row-menu/row-menu.js';
import { isChiefCard } from './board-dispatch.js';
import { fetchBoard, noteBoardEdit, renderBoard, sessionLabel } from '../board.js';

// Chief-only exchange refresh: loadExchange is one-shot, fine for a
// worker drawer you glance at — but a chat conversation needs the chief's
// reply to *arrive*. While the chief's drawer is open, re-run loadExchange
// on a short interval. Cleared unconditionally at the top of renderBoard()
// whenever that render doesn't keep the open drawer, and every close
// path goes through it.
let chiefExchangeTimer = null;
const CHIEF_EXCHANGE_POLL_MS = 5000;

// The open drawer's shared composer (the same component the session
// overlay mounts), replacing the private reply box and its per-drawer
// mic. Same lifecycle problem as chiefExchangeTimer above: a drawer
// collapse (state.boardExpanded set to null, then renderBoard() rebuilds the
// card list) drops the composer's DOM node with no teardown, so a recording
// in flight (or a still-finalizing one) stayed live and held the app-wide
// dictation mutex indefinitely. reset() — which dispose()s the mic and closes
// the composer's popovers — runs at the top of renderBoard() under the same
// rule as chiefExchangeTimer. A kept drawer keeps this instance, its
// draft and a live recording included.
let drawerComposer = null;

// The open drawer's kebab menu (Rename, Stop). A kept drawer
// keeps its menu, open or not; renderBoard() ends it with a drawer it drops.
// It drops below the drawer's action row, right edges 4px in.
const drawerMenu = createRowMenu({
  className: 'project-menu',
  placement: { against: function (kebab) { return kebab.parentElement; }, offset: 2, inset: 4 },
});

const DRAWER_KEYS_OFF = 'Open the terminal for keys';

// What the session overlay needs to decide its modes: kind picks
// Terminal, agent picks Chat, label marks the chief.
function overlaySession(card) {
  return {
    session_id: card.session_id,
    name: sessionLabel(card),
    kind: card.kind,
    agent: card.agent,
    label: card.label,
    // The header's model pill paints on first open, before any poll.
    model: card.model,
  };
}

function composerAvailability() {
  return {
    dictate: voiceDictationAvailable(),
    ocr: !!(state.status && state.status.screenshot_ocr),
  };
}

function buildDrawerComposer(card) {
  const host = document.createElement('div');
  host.className = 'board-drawer-composer';
  drawerComposer = mountComposer(host, {
    placeholder: 'Reply to ' + (card.project || 'session') + '…',
    send: function (text) { return sendFromDrawer(card, text); },
    upload: function (file, signal, kind) {
      return uploadSessionFile(card.session_id, file, signal, kind);
    },
    // The Board never opens the PTY's WebSocket, so ⌨ has nothing to drive
    // for either kind — disabled, with the way to get keys as its reason.
    keys: null,
    keysOffReason: DRAWER_KEYS_OFF,
  });
  // The console-input gate (agents.py) holds back Send alone, as in Chat.
  if (detachedSendRefused(card)) {
    drawerComposer.setSendable(false, 'No console input for this agent: sending is off');
  }
  drawerComposer.setAvailability(composerAvailability());
  return host;
}

// Chat or Terminal, the drawer's two visible actions:
// glyph beside label. An action the session can't take stays in the row,
// aria-disabled so a tap still reaches it and toasts the reason (a phone has
// no hover) — the same convention as the overlay's mode segments — so
// the row never changes shape.
function drawerAction(cls, glyph, label, title, reason, onTap) {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'board-drawer-action ' + cls;
  btn.innerHTML = icon(glyph) + '<span class="board-drawer-action-label">' + label + '</span>';
  btn.title = reason || title;
  btn.setAttribute('aria-label', reason || title);
  if (reason) btn.setAttribute('aria-disabled', 'true');
  btn.addEventListener('click', function () {
    if (reason) {
      toast(reason, '', { icon: glyph });
      return;
    }
    onTap(btn);
  });
  return btn;
}

// The open drawer's id, which its card header's aria-controls names.
export function drawerId(card) {
  return 'board-drawer-' + card.session_id;
}

export function buildDrawer(card) {
  const drawer = document.createElement('div');
  drawer.className = 'board-drawer';
  drawer.id = drawerId(card);

  const exchange = document.createElement('div');
  exchange.className = 'board-exchange';
  exchange.dataset.state = 'loading';
  exchange.setAttribute('role', 'status');
  exchange.setAttribute('aria-live', 'polite');
  exchange.textContent = 'Reading last exchange…';
  drawer.appendChild(exchange);
  loadExchange(card, exchange);
  if (isChiefCard(card)) {
    chiefExchangeTimer = setInterval(function () {
      // Defensive: a tab switch doesn't re-render the board, so gate on
      // the drawer actually still being the visible one.
      if (state.tab !== 'board' || state.boardExpanded !== card.session_id) return;
      loadExchange(card, exchange);
    }, CHIEF_EXCHANGE_POLL_MS);
  }
  // A session that has ended has nothing to reply to or act on.
  if (!card.alive) return drawer;

  // The shared composer for both kinds: a detached session is typed
  // into its PC console through the same /input route.
  drawer.appendChild(buildDrawerComposer(card));

  // Chat, the one tint primary, with Terminal beside it,
  // then a kebab holding Rename and Stop.
  const actions = document.createElement('div');
  actions.className = 'board-drawer-actions';
  const session = overlaySession(card);
  const chat = drawerAction(
    'board-open-chat', 'messages-square', 'Chat', 'Open the chat view',
    chatAvailable(session) ? null : modeReason(session, 'chat'),
    function () {
      state.boardExpanded = null;
      openSessionOverlay(session, 'chat');
    }
  );
  chat.classList.add('is-primary');
  actions.appendChild(chat);
  actions.appendChild(drawerAction(
    'board-open-terminal', 'terminal', 'Terminal', 'Open the full terminal',
    terminalAvailable(session) ? null : modeReason(session, 'terminal'),
    function () {
      state.boardExpanded = null;
      openTerminal(session);
    }
  ));
  const kebab = document.createElement('button');
  kebab.type = 'button';
  kebab.className = 'icon-button board-drawer-kebab';
  kebab.innerHTML = icon('ellipsis-vertical');
  kebab.title = 'Session actions';
  kebab.setAttribute('aria-label', kebab.title);
  actions.appendChild(kebab);
  drawerMenu.attach(card.session_id, kebab, [
    {
      // Rename: the launcher-native override (no PTY needed). The drawer
      // stays open across a rename (unlike Chat/Terminal, which navigate
      // away), so the completion callback patches the card optimistically
      // rather than waiting for the next poll. A kept drawer can
      // outlive the payload its `card` came from, so the patch goes to the
      // card in the current payload.
      dataset: { item: 'board-rename-btn' }, glyph: 'pencil',
      label: 'Rename', ariaLabel: 'Rename this session',
      onTap: function () {
        openSessionRename(card, function (title) {
          noteBoardEdit();
          card.manual_title = title;
          const current = boardCard(card.session_id);
          if (current) current.manual_title = title;
          renderBoard();
        });
      },
    },
    {
      // Stop: the unified stop path the Coding tab's row menu uses for both
      // kinds (the agent's own quit, force-fallback server-side),
      // always behind the themed confirm.
      dataset: { item: 'board-stop-btn' }, glyph: 'octagon-x',
      label: 'Stop', ariaLabel: 'Stop and kill this session',
      danger: true,
      onTap: async function () {
        if (!(await confirmDialog(stopConfirm(card)))) return;
        // Close the drawer first — the session it belongs to is going away.
        state.boardExpanded = null;
        renderBoard();
        await stopSession({ session_id: card.session_id, name: sessionLabel(card) });
        // The host stops gracefully (quit → force) — give it the same beat
        // sessions.js gives fetchSessions before reconciling the board.
        setTimeout(function () { fetchBoard().catch(function () {}); }, 1500);
      },
    },
  ]);
  drawer.appendChild(actions);
  return drawer;
}

// Stop asks every time: the chief and a Telegram channel
// keep their own words, any other session gets a plain one.
function stopConfirm(card) {
  if (isChiefCard(card)) return CHIEF_KILL_CONFIRM;
  if (isChannelSession(card)) return channelKillConfirm(card);
  return {
    title: 'Stop ' + sessionLabel(card) + '?',
    message: 'The agent quits and its terminal closes.',
    action: 'Stop',
  };
}

async function loadExchange(card, el) {
  try {
    const body = await terminalJsonApi(
      '/api/board/sessions/' + encodeURIComponent(card.session_id) + '/exchange'
    );
    el.replaceChildren();
    if (!body.available) {
      el.dataset.state = body.reason === 'no_exchange' ? 'empty' : 'error';
      const reasons = {
        no_exchange: 'No exchange yet.',
        session_not_found: 'Session ended — refresh the Board.',
        session_host_unreachable: 'Can’t reach the session host — try again.',
        native_unavailable: 'Conversation preview unavailable — open the terminal.',
        capture_unparseable: 'Conversation preview unavailable — open the terminal.',
      };
      el.textContent = reasons[body.reason] ||
        'Conversation preview unavailable — open the terminal.';
      return;
    }
    el.dataset.state = 'ready';
    // Rendered as markdown by Chat mode's renderer, which escapes
    // the text before it builds any markup.
    if (body.user && body.user.text) {
      const u = document.createElement('div');
      u.className = 'board-exchange-user tr-md';
      u.innerHTML = renderMarkdown(body.user.text);
      el.appendChild(u);
    }
    if (body.assistant && body.assistant.text) {
      const a = document.createElement('div');
      a.className = 'board-exchange-assistant tr-md';
      a.innerHTML = renderMarkdown(body.assistant.text);
      el.appendChild(a);
    }
    el.scrollTop = el.scrollHeight;
  } catch (exc) {
    el.dataset.state = 'error';
    el.textContent = 'Conversation preview unavailable — try again.';
  }
}

// Optimistic move for sendFromDrawer(): relocate a card from Your turn into
// Claude's turn client-side, ahead of the poll that will confirm it. A reply
// just went into a live PTY sitting at its prompt, so there is no value in
// making the Board visibly wait out the hook -> state-file -> poll round trip
// for something already known. Only touches state.board.columns — the next
// fetchBoard() replaces state.board wholesale as usual, so this is a
// display-only shortcut, never a second source of truth.
function moveCardToClaudeTurn(sessionId) {
  const columns = state.board && state.board.columns;
  if (!columns) return;
  const yourTurn = columns.your_turn || [];
  const idx = yourTurn.findIndex(function (c) { return c.session_id === sessionId; });
  if (idx === -1) return;
  const card = yourTurn.splice(idx, 1)[0];
  card.status = 'working';
  columns.claude_turn = [card].concat(columns.claude_turn || []);
}

// ➤ Send from the drawer's composer: the kind-agnostic /input route,
// worded by the same verdict table as Chat mode (sessions.js::sendOutcome —
// Sent / Sent, not confirmed / Queued / Not submitted stay distinct). A thrown
// 502/409/501 toasts and resolves false so the draft stays to retry.
async function sendFromDrawer(card, text) {
  let verdict;
  try {
    verdict = await sendSessionMessage(card.session_id, text);
  } catch (exc) {
    apiFailToast('Send failed', exc);
    return false;
  }
  const outcome = sendOutcome(verdict);
  toast(outcome.text, outcome.kind, { icon: 'send-horizontal' });
  // Close the drawer and optimistically flip the card to Claude's turn right
  // away. Deliberately no immediate fetchBoard() here: the hook that
  // actually flips the server's status hasn't had time to run yet, so an
  // immediate re-poll almost always still sees the pre-reply needs-you state
  // and would revert this straight back — worse than the original lag. The
  // regular 5 s poll (already running) reconciles with ground truth as
  // always. A reply whose Enter never went in (not_submitted, the one error
  // verdict) leaves the agent waiting, so that card stays put.
  if (state.boardExpanded === card.session_id) state.boardExpanded = null;
  if (outcome.kind !== 'error') moveCardToClaudeTurn(card.session_id);
  renderBoard();
  return true;
}

function boardCard(sessionId) {
  const columns = (state.board && state.board.columns) || {};
  for (const key of Object.keys(columns)) {
    const found = (columns[key] || []).find(function (c) { return c.session_id === sessionId; });
    if (found) return found;
  }
  return null;
}

// What buildDrawer() decides its actions from. A kept drawer is only valid
// while this holds — a session that died keeps no reply box for a dead PTY.
export function drawerShape(card) {
  return String(!!card.alive) + ':' + String(card.kind || '');
}

// The open drawer's <li> as it stands in the DOM right now, if it can be kept
// for `card` (the expanded card in the payload being rendered), else null.
export function openDrawerItem(card) {
  if (!card || !els.boardColumns) return null;
  const items = els.boardColumns.querySelectorAll('li.board-item.expanded');
  return Array.from(items).find(function (li) {
    return li.dataset.sessionId === card.session_id &&
      li.dataset.drawerShape === drawerShape(card);
  }) || null;
}

// Every renderBoard() calls this with the drawer it keeps, or null.
// Drawers that aren't kept are rebuilt — the chief exchange poll must never
// outlive the DOM node it writes into. Same lifecycle rule for the drawer
// composer's mic: reset() before the rebuild drops its DOM node, so a live
// or still-finalizing recording is force-stopped and the mic mutex released
// instead of wedging every other mic in the app.
export function syncDrawerLifecycle(openItem) {
  if (!openItem) {
    if (chiefExchangeTimer) {
      clearInterval(chiefExchangeTimer);
      chiefExchangeTimer = null;
    }
    if (drawerComposer) {
      drawerComposer.reset();
      drawerComposer = null;
    }
  } else if (drawerComposer) {
    // A kept drawer's composer outlives the render that built it, so its
    // mic / OCR availability follows an /api/status that lands after it.
    drawerComposer.setAvailability(composerAvailability());
  }
}

// After the lanes are drawn: with no drawer kept, the kebab menu ends too.
export function endDrawerRender(openItem) {
  if (!openItem) drawerMenu.endRender();
}
