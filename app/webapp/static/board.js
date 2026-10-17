/* Board tab (issues #300 / #301 / #302 / #164 / #399 / #608, reshaped by
 * #1436): the fleet kanban.
 *
 * Five computed columns from GET /api/board, each single-purpose, drawn as
 * lanes in this order (#1436): Backlog (open issues), the chief's plan over
 * Claude's turn (sessions working/unknown/idle/idle-finished/tool-pending),
 * Your turn (stalled/awaiting-decision/awaiting-input sessions only — #608's
 * split of the old undifferentiated needs-you, sharpened by #813's
 * tool-pending carve-out), PRs and jobs (open PRs + failed/unconfirmed/stuck
 * jobs), Done today (closed issues today). Each lane
 * is a collapsible section card like every other tab's (#1198) — stacked on
 * the phone, side by side on the desktop grid — with its count in the
 * summary, or a folded lane's exception ("1 failing").
 *
 * Cost discipline: fetchBoard() self-gates on the Board tab being visible
 * (pattern: fetchJobs / fetchRunningApps); the server's gh cache is only
 * refreshed via the ↻ button, on tab activation when the cache is older
 * than GH_STALE_MS, or on a poll that finds it never fetched (a webapp
 * restart empties it, #910) — never on a poll of an already-fetched cache.
 *
 * Act-from-the-card loop (#301): tapping a live session row opens an
 * inline drawer with the last user↔assistant exchange (passkey-gated — it
 * is transcript text, rendered as markdown), the shared composer (#984),
 * Chat and Terminal, and Rename and Stop in its kebab (#1502);
 * backlog rows of repos present in the projects folder carry one ▶ Start
 * verb, with YOLO in the row's kebab (#1436); `?board=<sid>` deep-links onto a
 * card with its drawer open. The poll keeps running while a drawer is open
 * (#958 — the chief chat holds one open for hours), and renderBoard() keeps
 * the open drawer's own node across the re-render, so it can never wipe a
 * reply being typed. Issue/PR/done rows open GitHub, job rows (PRs and
 * jobs) jump to the Jobs tab.
 *
 * Split off a single-file module (issue #691, `/codebase-audit`), the way
 * `jobs.js` and `terminal.js` already were: the lane toolbar's project
 * filter (#337), the Backlog's "Start with" model combo and the fleet chief's
 * lifecycle live in `board-dispatch.js`. This module keeps card rendering,
 * the drill-down drawer, one-tap issue-start and the lanes, and calls into
 * that one for the rest. The Board has no chief card (#1436 decision log):
 * the chief lives in its row under Code › Sessions, and here only as its
 * session row in Claude's turn.
 */

import { els, state } from './state.js';
import { apiFailToast, escapeHtml, isDesktopClient, jsonApi, toast } from './api.js';
import { setTab } from './tabs.js';
import {
  detachedSendRefused,
  openSessionRename,
  sendOutcome,
  sendSessionMessage,
  sessionTitle,
  stopSession,
} from './sessions.js';
import { applyLaunchSizePayload, openTerminal } from './terminal.js';
import { chatAvailable, modeReason, openSessionOverlay, terminalAvailable } from './session-overlay.js';
import { mountComposer } from './composer.js';
import { uploadSessionFile } from './terminal-compose.js';
import { voiceDictationAvailable } from './voice.js';
import { emptyStateEl } from './_vendored/empty-state/empty-state.js';
import { icon } from './_vendored/icons/icons.js';
import { terminalJsonApi } from './webauthn.js';
import { isHiddenChannel } from './channel-sessions.js';
import {
  CHIEF_KILL_CONFIRM, brandIconEl, channelKillConfirm, channelSessionName, fmtCardStamp, fmtDuration,
  isChannelSession,
} from './dom-utils.js';
import { renderMarkdown } from './markdown.js';
import { avatar, chip, sessionStatusBits, sessionStatusMeta } from './glance.js';
import { ACTION_ROW_MENU, actionRow } from './action-rows.js';
import { confirmDialog } from './confirm-dialog.js';
import { failedFetchState, setAsyncState } from './async-state.js';
import { renderBoardBadge } from './home-head.js';
import { renderHeadStatus } from './_vendored/home-head/home-head.js';
import { createRowMenu } from './_vendored/row-menu/row-menu.js';
import {
  boardRepoFilter,
  getBoardDispatchModel,
  isChiefCard,
  matchesRepoFilter,
  syncDispatchBar,
  wireDispatch,
} from './board-dispatch.js';
import { openChiefAnswers, syncChiefAnswers, wireChiefAnswers } from './board-answers.js';
import { answeredKeys, itemKey, openQuestionCount } from './chief-keys.js';

// `gh` marks where a column's cards come from (#910): 'all' columns are
// GitHub-only, so before the cache is loaded their count is unknown, never
// zero; 'part' (PRs and jobs) mixes open PRs with job cards, whose count
// stays real. `live` columns are built from the session-host list, so an
// unreachable session-host makes them unknown too (#915). Lane order (#1436):
// Backlog, (the chief's plan,) Claude's turn, Your turn, PRs and jobs, Done
// today. Each empty-state glyph is the lane's own icon.
const COLUMNS = [
  // Each empty lane says why, and its empty state carries the one control
  // that fills it (#1176, as a button since #1238 J-09): Refresh for the
  // GitHub lanes, Start work (the Code tab, where sessions start) for the
  // session lanes.
  { key: 'backlog', section: 'boardColBacklog', empty: 'No open issues on GitHub.', glyph: 'git-branch', gh: 'all' },
  { key: 'claude_turn', section: 'boardColClaude', empty: 'No sessions on Claude’s side.', glyph: 'hourglass', live: true },
  { key: 'your_turn', section: 'boardColYours', empty: 'Nothing needs you right now.', glyph: 'bell', live: true },
  { key: 'other', section: 'boardColOther', empty: 'No open PRs or stuck jobs.', glyph: 'git-pull-request', gh: 'part' },
  { key: 'done', section: 'boardColDone', empty: 'Nothing closed today yet.', glyph: 'circle-check', gh: 'all' },
];

const GH_STALE_MS = 2 * 60 * 1000;

let refreshInFlight = false;
let lastAutoRefreshAt = 0;
// The drawer whose section renderBoard() last unfolded (see there).
let revealedDrawer = null;

// --------------------------------------------------------------- helpers

// The exact same title resolution as the Coding tab's Running-sessions list
// (#396) — board cards carry the same field names (shared_name/
// shared_name_source, live_title, prompt_title, name, project) that
// sessionTitle() reads, so a single shared function keeps both tabs
// agreeing on one session's title instead of two independently-drifting
// codepaths (#383 review round first duplicated a smaller version of this).
function sessionLabel(card) {
  return sessionTitle(card) || card.project || 'session';
}


// ----------------------------------------------------------------- cards

// Every Board row is the shared action-row (#1501, #1502): one title line
// with an ellipsis on every width (decision 3 of #1500, reversing #339's
// two-line clamp), the full text in the title's `title` attribute.

// A PR, job or done row on the shared action-row (#1501, was its own
// cardShell): a one-line title, one muted meta line, and a chip only for an
// exception (glance.js tone map), after the meta text so it never
// ellipsizes away. No leading glyph and no left-edge status border: the lane
// says what kind of row it is, the chip what is wrong. With nothing to open
// (`onMain` null) the row is inert rather than a dead button.
function laneRow(cls, titleText, metaText, chips, label, onMain) {
  return actionRow({
    className: 'board-item ' + cls,
    title: titleText,
    meta: metaText,
    chips: chips,
    label: label,
    onMain: onMain,
    inert: !onMain,
  });
}

function openUrl(url) {
  return url ? function () { window.open(url, '_blank', 'noopener'); } : null;
}

// `openItem` is the open drawer's current <li> when renderBoard() can keep it
// (#958); the card's fresh header is swapped into it instead of a new drawer.
function renderSessionCard(card, openItem) {
  const chief = isChiefCard(card);
  const meta = sessionStatusMeta(card.status, chief);
  // The shared session row (#1433), the same anatomy as the Coding tab's:
  // avatar with the agent's mark and alive badge (crown for the chief), a
  // one-line title, one meta line "repo · status · age". Status is plain
  // meta; only an exception becomes a chip: stalled (danger), detached
  // (neutral), a Telegram channel's name (#1366). No left-edge status
  // border, no dimming of an idle card. The Board includes every
  // launcher-owned agent (#455), resolved against the same registry.
  const stalled = card.status === 'stalled';
  const chips = [];
  if (isChannelSession(card)) chips.push(chip(channelSessionName(card), 'neutral', 'session-channel-tag'));
  if (stalled) chips.push(chip('stalled', 'danger', 'board-stalled-chip'));
  if (card.kind === 'remote') chips.push(chip('detached', 'neutral', 'session-detached'));
  const bits = [card.project || ''].concat(
    sessionStatusBits(card.status, card.age_seconds, { chief: chief, hideStatus: stalled })
  ).filter(Boolean);
  const known = state.agents.find(function (a) { return a.id === card.agent; });
  const agentId = String(card.agent || 'claude');
  const title = sessionLabel(card);

  // On the shared action-row (#1502): the row's main button is the drawer's
  // disclosure. Tap toggles the drill-down drawer (#301); Chat and Terminal
  // inside it are the ways into the session.
  const row = actionRow({
    className: 'board-item board-item-session' + (meta.cls ? ' ' + meta.cls : ''),
    avatar: avatar(
      brandIconEl(agentId, 'session-agent-icon board-agent-icon', known ? known.label : agentId),
      chief ? 'crown' : (card.alive === false ? 'down' : 'alive'),
      'session-avatar'
    ),
    title: title,
    meta: bits.join(' · '),
    chips: chips,
    label: [title].concat(bits, chips.map(function (c) { return c.textContent; })).join(', '),
    onMain: function () {
      state.boardExpanded =
        state.boardExpanded === card.session_id ? null : card.session_id;
      renderBoard();
      if (!state.boardExpanded) fetchBoard().catch(function () {});
    },
    disabled: !card.session_id,
  });
  // `board-card*` stay as the hooks the poll's tests have always used.
  row.main.classList.add('board-card', 'board-card-session');
  row.title.classList.add('board-card-title');
  if (row.meta) row.meta.classList.add('board-card-meta');
  const li = row.li;
  // The chief is marked by its crown avatar badge, not a tint (#1449).
  if (chief) li.classList.add('board-item-chief');
  if (isChannelSession(card)) li.classList.add('board-item-channel');
  if (!card.session_id) return li;
  li.dataset.sessionId = card.session_id;
  // The main button is a disclosure for the drawer (#1259, WAI-ARIA
  // disclosure pattern): every render rebuilds it, so its state is set here,
  // and a kept drawer's fresh button below carries it too.
  const expanded = state.boardExpanded === card.session_id;
  row.main.setAttribute('aria-expanded', expanded ? 'true' : 'false');
  if (!expanded) return li;
  row.main.setAttribute('aria-controls', drawerId(card));
  if (openItem) {
    // A kept drawer (#958): only the row's main button is replaced; the
    // drawer after it, with its draft, focus and mic, stays.
    openItem.className = li.className + ' expanded';
    openItem.replaceChild(row.main, openItem.querySelector(':scope > .action-row-main'));
    return openItem;
  }
  li.classList.add('expanded');
  li.dataset.drawerShape = drawerShape(card);
  li.appendChild(buildDrawer(card));
  return li;
}

// ------------------------------------------------------ drill-down drawer

// Chief-only exchange refresh (#245): loadExchange is one-shot, fine for a
// worker drawer you glance at — but a chat conversation needs the chief's
// reply to *arrive*. While the chief's drawer is open, re-run loadExchange
// on a short interval. Cleared unconditionally at the top of renderBoard()
// whenever that render doesn't keep the open drawer (#958), and every close
// path goes through it.
let chiefExchangeTimer = null;
const CHIEF_EXCHANGE_POLL_MS = 5000;

// The open drawer's shared composer (#984 — the same component the session
// overlay mounts, #980), replacing the private reply box and its per-drawer
// mic. Same lifecycle problem as chiefExchangeTimer above (#755): a drawer
// collapse (state.boardExpanded set to null, then renderBoard() rebuilds the
// card list) drops the composer's DOM node with no teardown, so a recording
// in flight (or a still-finalizing one) stayed live and held the app-wide
// dictation mutex indefinitely. reset() — which dispose()s the mic and closes
// the composer's popovers — runs at the top of renderBoard() under the same
// rule as chiefExchangeTimer. A kept drawer (#958) keeps this instance, its
// draft and a live recording included.
let drawerComposer = null;

// The open drawer's kebab menu (#1502: Rename, Stop). A kept drawer (#958)
// keeps its menu, open or not; renderBoard() ends it with a drawer it drops.
// It drops below the drawer's action row, right edges 4px in.
const drawerMenu = createRowMenu({
  className: 'project-menu',
  placement: { against: function (kebab) { return kebab.parentElement; }, offset: 2, inset: 4 },
});

// Bumped when a rename the server has confirmed is patched into the Board
// (#1573). A Board read issued before that carries the old title, and its
// reply can land after the patch (the poll also awaits the chief's plan), so
// fetchBoard() drops a payload whose read predates the last bump; the next
// poll brings the server's view.
let boardEditSeq = 0;

const DRAWER_KEYS_OFF = 'Open the terminal for keys';

// What the session overlay needs to decide its modes (#982): kind picks
// Terminal, agent picks Chat, label marks the chief (#547).
function overlaySession(card) {
  return {
    session_id: card.session_id,
    name: sessionLabel(card),
    kind: card.kind,
    agent: card.agent,
    label: card.label,
    // The header's model pill (#1383) paints on first open, before any poll.
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

// Chat or Terminal, the drawer's two visible actions (decision 4 of #1500):
// glyph beside label. An action the session can't take stays in the row,
// aria-disabled so a tap still reaches it and toasts the reason (a phone has
// no hover) — the same convention as the overlay's mode segments (#982) — so
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

// The open drawer's id, which its card header's aria-controls names (#1259).
function drawerId(card) {
  return 'board-drawer-' + card.session_id;
}

function buildDrawer(card) {
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

  // The shared composer (#984) for both kinds: a detached session is typed
  // into its PC console through the same /input route (#967).
  drawer.appendChild(buildDrawerComposer(card));

  // Decision 4 of #1500: Chat, the one tint primary, with Terminal beside it,
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
      // rather than waiting for the next poll. A kept drawer (#958) can
      // outlive the payload its `card` came from, so the patch goes to the
      // card in the current payload.
      dataset: { item: 'board-rename-btn' }, glyph: 'pencil',
      label: 'Rename', ariaLabel: 'Rename this session',
      onTap: function () {
        openSessionRename(card, function (title) {
          boardEditSeq += 1;
          card.manual_title = title;
          const current = boardCard(card.session_id);
          if (current) current.manual_title = title;
          renderBoard();
        });
      },
    },
    {
      // Stop: the unified stop path the Coding tab's row menu uses for both
      // kinds (#253: the agent's own quit, force-fallback server-side),
      // always behind the themed confirm (#1502).
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

// Stop asks every time (#1502): the chief (#245) and a Telegram channel
// (#1366) keep their own words, any other session gets a plain one.
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
    // Rendered as markdown by Chat mode's renderer (#1502), which escapes
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

// Optimistic move for sendFromDrawer() (#461): relocate a card from Your turn into
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

// ➤ Send from the drawer's composer (#984): the kind-agnostic /input route,
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
  // away. Deliberately no immediate fetchBoard() here (#461): the hook that
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

// ---------------------------------------------------- one-tap issue start

function repoInProjects(repo) {
  return (state.apps || []).some(function (a) {
    return a.kind === 'claude-code' &&
      String(a.name).toLowerCase() === String(repo || '').toLowerCase();
  });
}

async function startIssue(card, mode, btn) {
  btn.disabled = true;
  try {
    // Carry the issue title so the server can auto-name the spawned session
    // after it (#467) — display data, never reaches the command line.
    const payload = {
      repo: card.repo, number: card.number, mode: mode,
      title: card.title || '',
      // The bar's Start-model selector governs one-tap starts (#505),
      // overriding the shared Coding model per launch.
      model: getBoardDispatchModel(),
    };
    // Desktop browsers get the PC mirror window, like every launch (#241).
    // Phone launches carry the real terminal size so the PTY's early
    // output is authored at the width the overlay will fit() to (issue
    // #374); the route already accepts rows/cols.
    applyLaunchSizePayload(payload);
    const body = await terminalJsonApi('/api/board/issues/start', { method: 'POST', body: payload });
    toast(
      (mode === 'yolo' ? '/issue-yolo ' : '/issue-start ') + '#' +
        card.number + ' in ' + (body.repo || card.repo),
      'good',
      { icon: mode === 'yolo' ? 'zap' : 'play' }
    );
    if (body.session && body.session.kind !== 'remote' && !isDesktopClient()) {
      openTerminal(body.session);
    }
  } catch (exc) {
    apiFailToast('Issue start failed', exc);
  } finally {
    btn.disabled = false;
  }
}

// The Backlog rows' kebab menus (#1436), one shared row-menu: the 5 s poll
// rebuilds every row, and attach() reopens the open one on its rebuilt row.
// It drops below the row, right-aligned, as every action-row's menu does.
const issueMenu = createRowMenu({ className: 'project-menu', placement: ACTION_ROW_MENU });

async function copyLink(url) {
  try {
    await navigator.clipboard.writeText(url);
    toast('Link copied', 'good', { icon: 'copy' });
  } catch (exc) {
    apiFailToast('Could not copy the link', exc);
  }
}

// A Backlog row (#1436, on the shared action-row since #1502): the one-line
// title, the meta "repo #N" with an accent "in progress" chip while a lane
// holds the issue, then one trailing plain verb (#1501), ▶ Start, and the
// kebab holding YOLO (start and ship), Open on GitHub and Copy link
// (decision 3 of #1432: YOLO takes two taps now). Git state is no longer
// drawn here; it lives on Code › Projects. A tap on the row opens the issue
// on GitHub.
function stampEl(text) {
  const el = document.createElement('span');
  el.className = 'board-card-stamp';
  el.textContent = '· ' + text;
  return el;
}

function renderIssueCard(card) {
  // Claim owner liveness (#948): 'dead' is a provably gone lane — not in
  // progress, startable again; 'unknown' keeps the in-progress lock but says
  // the owner could not be verified.
  const isInProgress = card.in_progress === true;
  const claimUnverified = isInProgress && card.claim_state === 'unknown';
  const claimStale = card.claim_state === 'dead';
  const chips = [];
  if (claimUnverified) chips.push(chip('in progress (unverified)', 'accent', 'board-claim-chip'));
  else if (isInProgress) chips.push(chip('in progress', 'accent', 'board-claim-chip'));
  else if (claimStale) chips.push(chip('stale claim', 'neutral', 'board-claim-chip'));

  // The last-updated date (#1638): the tail of the meta text, or, beside a
  // claim chip (where the text ellipsizes), its own item that never shrinks.
  const stamp = fmtCardStamp(card.updated_at);

  // One-tap start (#301) — only for repos the Coding tab could launch in.
  // While a lane holds the issue the verb steps aside: the chip says why.
  const startable = !!(card.number && repoInProjects(card.repo));
  let start = null;
  if (startable && !isInProgress) {
    start = document.createElement('button');
    start.type = 'button';
    start.className = 'icon-button action-row-verb board-issue-btn board-issue-start';
    start.innerHTML = icon('play');
    start.title = '/issue-start ' + card.number + ' in ' + card.repo;
    start.setAttribute('aria-label', 'Start issue #' + card.number);
    start.addEventListener('click', function () { startIssue(card, 'start', start); });
  }
  const row = actionRow({
    className: 'board-item board-item-issue' + (isInProgress ? ' is-in-progress' : ''),
    title: card.title || '',
    meta: [card.repo, '#' + card.number].filter(Boolean).join(' ') + (stamp && !chips.length ? ' · ' + stamp : ''),
    chips: stamp && chips.length ? [stampEl(stamp)].concat(chips) : chips,
    label: 'Open issue #' + card.number + ' on GitHub',
    onMain: openUrl(card.url),
    inert: !card.url,
    trailing: start,
    kebabLabel: startable || card.url ? 'Issue #' + card.number + ' actions' : null,
    kebabClass: 'board-issue-kebab',
  });
  if (!row.kebab) return row.li;
  const inProgressTitle = 'Issue #' + card.number + ' is already in progress' +
    (claimUnverified ? ' (owner unverified)' : '');
  const kebab = row.kebab;
  issueMenu.attach(card.repo + '#' + card.number, kebab, [
    {
      dataset: { item: 'board-issue-yolo' }, glyph: 'zap',
      label: 'YOLO (start and ship)', ariaLabel: 'YOLO issue #' + card.number + ' (start and ship)',
      hidden: !startable,
      disabled: isInProgress,
      hint: inProgressTitle,
      onTap: function () { startIssue(card, 'yolo', kebab); },
    },
    {
      dataset: { item: 'board-issue-open' }, glyph: 'globe',
      label: 'Open on GitHub', ariaLabel: 'Open issue #' + card.number + ' on GitHub',
      hidden: !card.url,
      onTap: function () { window.open(card.url, '_blank', 'noopener'); },
    },
    {
      dataset: { item: 'board-issue-copy' }, glyph: 'copy',
      label: 'Copy link', ariaLabel: 'Copy the link to issue #' + card.number,
      hidden: !card.url,
      onTap: function () { copyLink(card.url); },
    },
  ]);
  return row.li;
}

function renderPrCard(card) {
  const draft = card.is_draft ? ' · draft' : '';
  return laneRow('board-item-pr', card.title || '',
    [[card.repo, 'PR #' + card.number].join(' ') + draft, fmtCardStamp(card.updated_at)]
      .filter(Boolean).join(' · '), [],
    'Open PR #' + card.number + ' on GitHub', openUrl(card.url)).li;
}

// A job row's chip by run state, on the tone map: a failed run is danger;
// stuck, `unconfirmed` (#916: the run may well have delivered, but the
// scheduled-run adapter could not establish that) and `unreadable` (#915: the
// run history itself could not be read) want a look, so attention — never the
// danger that says a run is known to have failed.
const JOB_CHIPS = {
  failed: ['failed', 'danger'],
  stuck: ['stuck', 'attention'],
  unconfirmed: ['not confirmed', 'attention'],
  unreadable: ['unreadable', 'attention'],
};

// A job in PRs and jobs that counts as failing (#1436): the header's
// "N failing" and the folded lane's meta.
function jobFailing(card) {
  return card.kind === 'job' && (card.state === 'failed' || card.state === 'stuck');
}

// A failed or unconfirmed run is a past event, so it can be dismissed once
// dealt with (#1669). Stuck and unreadable describe a live problem: no verb.
function jobDismissible(card) {
  return card.kind === 'job' && !!card.run_id &&
    (card.state === 'failed' || card.state === 'unconfirmed');
}

// Drop the card at once rather than wait out the 5 s poll. Bumping the edit
// seq discards a Board read already in flight, which would put it back.
async function dismissJob(card, btn) {
  btn.disabled = true;
  try {
    await jsonApi('/api/jobs/' + encodeURIComponent(card.job_id) + '/runs/' +
      encodeURIComponent(card.run_id) + '/dismiss', { method: 'POST' });
    boardEditSeq += 1;
    const other = state.board && state.board.columns && state.board.columns.other;
    if (other) {
      state.board.columns.other = other.filter(function (c) {
        return !(c.kind === 'job' && c.job_id === card.job_id && c.run_id === card.run_id);
      });
      renderBoard();
    }
    fetchBoard().catch(function () {});
  } catch (exc) {
    apiFailToast('Could not dismiss the alert', exc);
    btn.disabled = false;
  }
}

function renderJobCard(card) {
  const c = JOB_CHIPS[card.state] || [String(card.state || 'failed'), 'danger'];
  const meta = ['job', card.age_seconds != null ? fmtDuration(card.age_seconds) : '']
    .filter(Boolean).join(' · ');
  const name = card.job_name || card.job_id || 'job';
  let dismiss = null;
  if (jobDismissible(card)) {
    dismiss = document.createElement('button');
    dismiss.type = 'button';
    dismiss.className = 'icon-button action-row-verb board-job-dismiss';
    dismiss.innerHTML = icon('circle-check');
    dismiss.title = 'Dismiss this alert';
    dismiss.setAttribute('aria-label', 'Dismiss the ' + name + ' alert');
    dismiss.addEventListener('click', function () { dismissJob(card, dismiss); });
  }
  const row = actionRow({
    className: 'board-item board-item-job is-' + card.state,
    title: name,
    meta: meta,
    chips: [chip(c[0], c[1], 'board-job-chip')],
    label: 'Open ' + name + ' in Jobs',
    onMain: function () { setTab('jobs'); },
    trailing: dismiss,
  });
  if (card.error) row.main.title = card.error;
  return row.li;
}

function renderDoneCard(card) {
  // Done holds closed issues only (#399) — a merged PR that closed one is
  // already reflected here by the issue itself, so there's no PR/pairing
  // branch to render.
  return laneRow('board-item-done', card.title || '',
    [[card.repo, '#' + card.number].join(' '), card.state,
      fmtCardStamp(card.closed_at || card.updated_at)].filter(Boolean).join(' · '), [],
    'Open issue #' + card.number + ' on GitHub', openUrl(card.url)).li;
}

function renderCard(colKey, card, openItem) {
  if (card.kind === 'issue' && colKey === 'backlog') return renderIssueCard(card);
  if (colKey === 'done') return renderDoneCard(card);
  if (card.kind === 'pr') return renderPrCard(card);
  if (card.kind === 'job') return renderJobCard(card);
  return renderSessionCard(card, openItem);
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
function drawerShape(card) {
  return String(!!card.alive) + ':' + String(card.kind || '');
}

// The open drawer's <li> as it stands in the DOM right now, if it can be kept
// for `card` (the expanded card in the payload being rendered), else null.
function openDrawerItem(card) {
  if (!card || !els.boardColumns) return null;
  const items = els.boardColumns.querySelectorAll('li.board-item.expanded');
  return Array.from(items).find(function (li) {
    return li.dataset.sessionId === card.session_id &&
      li.dataset.drawerShape === drawerShape(card);
  }) || null;
}

// Set a list's children to `nodes` without ever detaching a node that is
// already in place — moving a focused <textarea> blurs it, which on a phone
// drops the keyboard mid-reply (#958). Only the kept drawer <li> can already
// be a child; everything else is freshly built.
function placeChildren(list, nodes) {
  Array.from(list.children).forEach(function (child) {
    if (nodes.indexOf(child) === -1) child.remove();
  });
  let ref = list.firstChild;
  nodes.forEach(function (node) {
    if (node === ref) ref = ref.nextSibling;
    else list.insertBefore(node, ref);
  });
}

// ---------------------------------------------------------------- render

// The GitHub cache is process memory, so a webapp restart empties it:
// `fetched_at` null means "never fetched", which must never render as the
// genuine "fetched, nothing open" (#910).
function ghFetched(body) {
  return !!(body && body.github && body.github.fetched_at);
}

// Only an explicit `available: false` is unknown — a payload without the
// section (a stubbed test body) keeps today's render.
function liveSessionsRead(body) {
  return !(body && body.live_sessions && body.live_sessions.available === false);
}

// Distinct text per condition: a real zero, a not-yet-fetched cache, and a
// first fetch that failed are three different answers. "Nothing needs you
// right now." is reachable only from a session list actually read (#915).
function emptyText(col, body, ghLoaded, liveRead) {
  if (col.live) {
    return liveRead ? col.empty : 'Session-host unreachable — sessions unknown.';
  }
  if (!col.gh || ghLoaded) return col.empty;
  const failed = !!(body.github && body.github.error);
  if (col.gh === 'part') {
    return failed ? 'No stuck jobs — open PRs unavailable (GitHub fetch failed).'
      : 'No stuck jobs — open PRs not loaded yet.';
  }
  return failed ? 'GitHub fetch failed.' : 'Not loaded from GitHub yet.';
}

// The lane's one next action (#1238 J-09), reusing the launcher's own flows:
// a session lane starts work on the Code tab, where sessions launch (or
// re-reads the session-host it couldn't reach); a GitHub lane refreshes.
function emptyAction(col, liveRead) {
  if (col.live && !liveRead) {
    return { actionLabel: 'Retry', onAction: function () { fetchBoard().catch(function () {}); } };
  }
  if (col.live) return { actionLabel: 'Start work', onAction: function () { setTab('claude'); } };
  return {
    actionLabel: 'Refresh',
    onAction: function () {
      refreshGithub().catch(function (exc) { apiFailToast('GitHub refresh failed', exc); });
    },
  };
}

// One attention banner above the lanes (#1436), shown only while a source
// is down or stale: every problem in one line behind one glyph.
function renderStatusLine(body) {
  const parts = [];
  if (body.github && body.github.error) {
    parts.push('GitHub: ' + body.github.error);
  } else if (body.github && !body.github.fetched_at) {
    parts.push('GitHub not fetched yet — tap Refresh');
  }
  if (!liveSessionsRead(body)) {
    parts.push('session-host unreachable — live sessions unknown');
  }
  if (body.sessions_state && !body.sessions_state.available) {
    parts.push('session state unavailable (hooks not writing yet)');
  } else if (body.sessions_state && body.sessions_state.stale) {
    parts.push('session state stale');
  }
  els.boardStatus.innerHTML = parts.length
    ? icon('triangle-alert') + '<span>' + escapeHtml(parts.join(' · ')) + '</span>'
    : '';
  els.boardStatus.hidden = parts.length === 0;
}

// A lane's exception (#1436): in its tone, in place of the count, while the
// lane is folded (the CSS swaps them); '' when there is none.
function setLaneException(section, text, tone) {
  const el = section && section.querySelector('.board-lane-exception');
  if (!el) return;
  el.textContent = text;
  el.dataset.tone = tone;
  el.hidden = !text;
}

// The Backlog's footer (#1436): how fresh the GitHub cache is, only once it
// has been fetched (before that the empty state says so).
function renderBacklogFoot(body, ghLoaded) {
  const foot = els.boardColumns.querySelector('.board-lane-foot[data-col="backlog"]');
  if (!foot) return;
  const ago = ghLoaded ? agoText(body.github.fetched_at) : '';
  foot.hidden = !ago;
  foot.innerHTML = ago ? icon('globe') + '<span>GitHub · updated ' + ago + '</span>' : '';
}

function plural(n, one, many) {
  return n + ' ' + (n === 1 ? one : many);
}

// The page header (#1436, the header rule): only the exceptions — "N
// questions" (the chief's, unanswered, #1502), "N needs you" (Your turn) and
// "N failing" (failed or stuck jobs), two at most — fleet-wide, whatever the
// project filter narrows the lanes to. With nothing wrong, the plain totals;
// a count that could not be read is said, never shown as zero (#910, #915):
// an unreadable plan adds no question part.
function renderBoardHead(body, ghLoaded, liveRead) {
  const columns = body.columns || {};
  const sessions = function (key) {
    return (columns[key] || []).filter(function (c) { return !isHiddenChannel(c); }).length;
  };
  const questions = openQuestionCount(state.chiefPlan);
  const yours = sessions('your_turn');
  const failing = (columns.other || []).filter(jobFailing).length;
  const exceptions = [];
  if (questions) exceptions.push({ text: plural(questions, 'question', 'questions'), tone: 'attention' });
  if (yours) exceptions.push({ text: yours + ' needs you', tone: 'attention' });
  if (failing) exceptions.push({ text: failing + ' failing', tone: 'danger' });
  const live = yours + sessions('claude_turn');
  const totals = [liveRead || live ? plural(live, 'session', 'sessions') : 'sessions unknown'];
  if (ghLoaded) totals.push(plural((columns.backlog || []).length, 'issue', 'issues'));
  renderHeadStatus(els.boardHeadStatus, exceptions, totals.join(' · '));
}


// ------------------------------------------------------- chief's plan (#1279)
//
// The fleet chief records its lanes, its queue and what waits on Roberto in a
// plan file (format v1, fleet-config's `chief_ops.py plan`); the server reads
// it tolerantly (src/chief_plan.py) and this card shows it under Claude's
// turn. No plan, the card stays blank. A plan the server could not trust adds
// one quiet note, never a toast. Whether the chief is running comes from the
// session cards /api/board already carries, and is said on its own line: a
// plan with no chief behind it must not read as current. What waits on
// Roberto carries an "Answer N questions" button into the one-shot answer
// sheet (#1295, board-answers.js).

// Status -> chip tone, on the tone map (#1436): work in progress is accent,
// waiting on you is attention, merged and anything unlisted (an unknown status
// included) is neutral.
const PLAN_TONES = {
  building: 'accent', gate: 'accent',
  waiting: 'attention', 'waiting-roberto': 'attention',
  merged: 'neutral',
};

// "just now" / "12 min ago" / "3 h ago" / "2 d ago", or '' for a time that
// can't be read: the plan's age and the Backlog footer's.
function agoText(iso) {
  const t = Date.parse(iso || '');
  if (isNaN(t)) return '';
  const min = Math.floor((Date.now() - t) / 60000);
  if (min < 1) return 'just now';
  if (min < 60) return min + ' min ago';
  const h = Math.floor(min / 60);
  if (h < 48) return h + ' h ago';
  return Math.floor(h / 24) + ' d ago';
}

function planAge(updatedAt) {
  const ago = agoText(updatedAt);
  return ago ? 'Updated ' + ago : 'Update time unknown';
}

// Running / not running / unknown are three answers: an unreadable
// session-host can't prove the chief is gone.
function chiefRunState(body, liveRead) {
  const columns = (body && body.columns) || {};
  const running = Object.keys(columns).some(function (key) {
    return (columns[key] || []).some(function (c) {
      return isChiefCard(c) && c.alive !== false;
    });
  });
  if (running) return 'running';
  return liveRead ? 'stopped' : 'unknown';
}

function planLine(cls, text) {
  const p = document.createElement('p');
  p.className = 'board-plan-line muted small' + (cls ? ' ' + cls : '');
  p.textContent = text;
  return p;
}

// A plan section (#1578): its heading carries the row count, "Lanes (2)", and
// an optional muted line under it (`note`) says what the number counts.
function planGroup(heading, rows, cls, note) {
  const wrap = document.createElement('div');
  wrap.className = 'board-plan-group' + (cls ? ' ' + cls : '');
  const h = document.createElement('h4');
  h.className = 'board-plan-heading';
  h.textContent = heading;
  wrap.appendChild(h);
  if (note) wrap.appendChild(planLine('board-plan-section-note', note));
  const list = document.createElement('ul');
  list.className = 'card-list board-list';
  rows.forEach(function (row) { list.appendChild(row); });
  wrap.appendChild(list);
  return wrap;
}

// Parked (#1578): the same section as a <details>, so a long parked list does
// not push the queue off a phone. Collapsed by default on a phone, open on the
// desktop board; once the reader toggles it the choice holds across rebuilds.
let parkedOpen = null;

function parkedGroup(rows) {
  const open = parkedOpen === null
    ? !!(window.matchMedia && window.matchMedia(DESKTOP_BOARD).matches)
    : parkedOpen;
  const wrap = document.createElement('details');
  wrap.className = 'board-plan-group board-plan-parked';
  wrap.open = open;
  const sum = document.createElement('summary');
  sum.className = 'board-plan-heading board-plan-parked-summary';
  const label = document.createElement('span');
  label.textContent = 'Parked (' + rows.length + ')';
  const mark = document.createElement('span');
  mark.className = 'board-plan-parked-chevron';
  mark.setAttribute('aria-hidden', 'true');
  mark.innerHTML = icon('chevron-right');
  sum.append(label, mark);
  const list = document.createElement('ul');
  list.className = 'card-list board-list';
  rows.forEach(function (row) { list.appendChild(row); });
  wrap.append(sum, list);
  wrap.addEventListener('toggle', function () { parkedOpen = wrap.open; });
  return wrap;
}

// `shown` is the plan positions the repo filter keeps (#1332) that no
// device has answered yet (#1487): the button counts and opens only those.
function answerButton(plan, run, shown) {
  const n = shown.length;
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'button-tint board-plan-answer';
  btn.innerHTML = icon('messages-square') + ' ';
  btn.appendChild(document.createTextNode('Answer ' + n + (n === 1 ? ' question' : ' questions')));
  btn.addEventListener('click', function () { openChiefAnswers(plan, run, shown); });
  return btn;
}

// The Board's repo filter narrows the plan too (#1332), so a filtered Board
// is one project's whole picture. A row's repo is its own `repo`, else the
// repo half of a `repo#N` ref. A row with neither is fleet-wide context and
// stays under any filter: hiding it could hide a question that needs an
// answer.
const PLAN_REF_REPO = /^([A-Za-z0-9._-]+)#\d+$/;

function planRowRepo(row) {
  if (row.repo) return row.repo;
  const m = PLAN_REF_REPO.exec(row.ref || '');
  return m ? m[1] : null;
}

function planRowShown(row, filter) {
  const repo = planRowRepo(row);
  return !repo || matchesRepoFilter({ repo: repo }, filter);
}

// What the chief has set aside (#1578): shown under Parked, never in the
// queue. Every other status is work in the queue's order.
const PARKED_STATUSES = ['parked', 'waiting-roberto'];

function isParked(q) {
  return PARKED_STATUSES.indexOf(q.status) !== -1;
}

// A queue row (#1297): the issue title leads, the meta is `repo#N`, then the
// chief's note. An item without a title is named by its label and keeps just
// the note here.
function queueRow(q) {
  const ref = q.ref && q.ref.charAt(0) === '#' && q.repo ? q.repo + q.ref : q.ref;
  const label = ref || q.repo;
  const meta = [];
  if (q.title && label) meta.push(label);
  if (q.note) meta.push(q.note);
  return planRow({
    title: q.title || label,
    meta: meta,
    status: q.status,
    model: q.model,
    url: q.ref_url,
    ref: label,
  });
}

// A lane (#1578): the repo is its title, the item (`#N`) its meta.
function planLaneRow(l) {
  return planRow({
    title: l.repo || 'lane',
    meta: l.item ? [l.item] : [],
    status: l.status,
    model: l.model,
    url: l.ref_url,
    ref: l.item ? (l.repo || '') + l.item : '',
  });
}

// A lane, queue or parked row (#1578, on the shared action-row as the Backlog
// card's rows are): line 1 the title, on its own so it reads in full; line 2
// the meta pieces joined by " · " with the chief's optional model word (#1352)
// among them, the status chip at the right end of that line. A row with a
// link (`url`, resolved by the reader from its `repo#N`) is a tap that opens
// it and carries the trailing chevron that says so; a row without one reports
// and does nothing.
function planRow(o) {
  const pieces = o.meta.slice();
  if (o.model) {
    const tag = document.createElement('span');
    tag.className = 'board-plan-model';
    tag.textContent = o.model.charAt(0).toUpperCase() + o.model.slice(1);
    pieces.splice(Math.min(1, pieces.length), 0, tag);
  }
  const open = o.url ? function () { window.open(o.url, '_blank', 'noopener'); } : null;
  const row = actionRow({
    className: 'board-item board-plan-row' + (open ? ' is-link' : ''),
    title: o.title,
    meta: pieces.length ? ' ' : '',
    chips: o.status ? [chip(o.status, PLAN_TONES[o.status] || 'neutral', 'board-plan-chip')] : [],
    label: 'Open ' + (o.ref || o.title) + ' on GitHub',
    onMain: open,
    inert: !open,
    trailing: open ? chevron('chevron-right', open) : null,
  });
  // The pieces take the place of the meta text, so the line still
  // ellipsizes as one and the chip never does.
  const text = row.meta && row.meta.querySelector('.action-row-meta-text');
  if (text) {
    text.textContent = '';
    pieces.forEach(function (p, i) {
      if (i) text.appendChild(document.createTextNode(' · '));
      text.appendChild(typeof p === 'string' ? document.createTextNode(p) : p);
    });
  }
  return row.li;
}

// A row's trailing chevron (design.md action-row: a row that opens a sheet
// carries one). It sits outside the main button, so it takes the same tap.
function chevron(glyph, onTap) {
  const el = document.createElement('span');
  el.className = 'board-plan-chevron';
  el.setAttribute('aria-hidden', 'true');
  el.innerHTML = icon(glyph);
  el.addEventListener('click', onTap);
  return el;
}

// The questions still waiting on you that the repo filter shows, unanswered:
// the folded plan card's "N questions for you" (#1436).
function pendingQuestions(plan, answered, filter) {
  if (plan.state !== 'ok') return 0;
  return (plan.waiting_on_roberto || []).filter(function (w, i) {
    return planRowShown(w, filter) && answered.indexOf(itemKey(w, i)) === -1;
  }).length;
}

// Decision 2 of #1500: the first QUESTIONS_SHOWN questions, then one "N more"
// row that unfolds the rest in place (for as long as the page lives), then
// the one Answer button.
const QUESTIONS_SHOWN = 3;
let questionsUnfolded = false;

// One waiting question (#1502): its one-line text (the sheet asks the full
// question), its ref as the meta, a chevron; a tap opens the answer sheet on
// it. One already answered says so and does nothing on tap: the sheet skips
// it.
function questionRow(plan, run, open, i, answered) {
  const w = plan.waiting_on_roberto[i];
  const sent = answered.indexOf(itemKey(w, i)) !== -1;
  const title = w.text || w.question || w.ref;
  const ref = (w.text || w.question) ? (w.ref || w.repo) : '';
  const openSheet = function () { openChiefAnswers(plan, run, open, i); };
  return actionRow({
    className: 'board-item board-plan-row board-plan-waiting' + (sent ? ' is-answered' : ''),
    title: title,
    meta: [ref, sent ? 'answered, waiting for the chief' : ''].filter(Boolean).join(' · '),
    density: 'one-line',
    label: 'Answer: ' + title,
    onMain: openSheet,
    inert: sent,
    trailing: sent ? null : chevron('chevron-right', openSheet),
  }).li;
}

function moreRow(n) {
  const unfold = function () {
    questionsUnfolded = true;
    renderBoard();
  };
  const text = plural(n, 'more question', 'more questions');
  return actionRow({
    className: 'board-item board-plan-more',
    title: text,
    label: 'Show ' + text,
    density: 'one-line',
    onMain: unfold,
    trailing: chevron('chevron-down', unfold),
  }).li;
}

function renderChiefPlan(body, liveRead) {
  const host = els.boardColumns && els.boardColumns.querySelector('.board-plan-body');
  if (!host) return;
  const plan = state.chiefPlan || { state: 'empty' };
  const run = plan.state === 'ok' ? chiefRunState(body, liveRead) : '';
  const age = plan.state === 'ok' ? planAge(plan.updated_at) : '';
  // An open answer sheet follows whether the chief runs, every poll — read
  // from the sessions, whatever state the plan itself is in.
  syncChiefAnswers(chiefRunState(body, liveRead));
  const answered = plan.state === 'ok' ? answeredKeys(plan) : [];
  const filter = boardRepoFilter();
  const pending = pendingQuestions(plan, answered, filter);
  setLaneException(els.boardChiefPlan,
    pending ? plural(pending, 'question', 'questions') + ' for you' : '',
    'attention');
  // Rebuilt only when what it shows changes, not on every 5 s poll.
  const sig = JSON.stringify([plan, run, age, answered, filter, questionsUnfolded]);
  if (host.dataset.sig === sig) return;
  host.dataset.sig = sig;
  host.dataset.state = plan.state;
  const nodes = [];
  if (plan.state === 'unreadable') {
    nodes.push(planLine('board-plan-note', 'Plan unreadable.'));
  } else if (plan.state === 'unavailable') {
    nodes.push(planLine('board-plan-note', 'Plan not loaded — the webapp did not answer.'));
  } else if (plan.state === 'ok') {
    if (run === 'stopped') {
      nodes.push(planLine('board-plan-chief', 'Chief not running — this plan may be out of date.'));
    } else if (run === 'unknown') {
      nodes.push(planLine('board-plan-chief', 'Chief status unknown — session-host unreachable.'));
    }
    nodes.push(planLine('board-plan-age', age));
    const waiting = plan.waiting_on_roberto || [];
    // Positions, not items: an answer's key is its place in the whole list.
    const shown = [];
    waiting.forEach(function (w, i) { if (planRowShown(w, filter)) shown.push(i); });
    const hidden = waiting.length - shown.length;
    if (shown.length) {
      // Answered from any device (#1487): not counted, not asked again.
      const open = shown.filter(function (i) {
        return answered.indexOf(itemKey(waiting[i], i)) === -1;
      });
      // "1 more" would take a row to save one: fold only from two hidden.
      const folded = !questionsUnfolded && shown.length > QUESTIONS_SHOWN + 1;
      const listed = folded ? shown.slice(0, QUESTIONS_SHOWN) : shown;
      const rows = listed.map(function (i) { return questionRow(plan, run, open, i, answered); });
      if (folded) rows.push(moreRow(shown.length - listed.length));
      const group = planGroup('Waiting on you', rows, 'board-plan-group-waiting');
      if (open.length) group.appendChild(answerButton(plan, run, open));
      nodes.push(group);
    }
    // A pending decision is never invisible: say what the filter hides.
    if (hidden) {
      nodes.push(planLine('board-plan-hidden', hidden + ' more question'
        + (hidden === 1 ? '' : 's') + ' in other projects'));
    }
    const lanes = (plan.lanes || []).filter(function (l) { return planRowShown(l, filter); });
    if (lanes.length) {
      nodes.push(planGroup('Lanes (' + lanes.length + ')', lanes.map(planLaneRow)));
    }
    // Unfiltered, the Queue heading shows even when empty, as it always has;
    // a filter that empties it hides it, as the columns do. Parked items
    // (#1578) leave the queue for their own section below it.
    const planned = (plan.queue || []).filter(function (q) { return planRowShown(q, filter); });
    const queue = planned.filter(function (q) { return !isParked(q); });
    const parked = planned.filter(isParked);
    if (queue.length || !filter) {
      nodes.push(planGroup('Queue (' + queue.length + ')', queue.map(queueRow), '',
        'Only what the chief has scheduled; the Backlog counts every open issue.'));
    }
    if (parked.length) nodes.push(parkedGroup(parked.map(queueRow)));
  }
  host.replaceChildren.apply(host, nodes);
}

// The desktop grid (#1502): one column per lane that holds rows, the plan
// and Claude's turn sharing one as always; an empty lane spans the grid as
// a strip under them (CSS). The pane carries the count, so the project
// filter above stays exactly one lane wide (#1464).
function sizeLaneGrid() {
  const cells = Array.from(els.boardColumns.children).filter(function (el) {
    return !el.classList.contains('is-empty');
  }).length;
  els.boardColumns.parentElement.style.setProperty('--board-lanes', String(Math.max(1, cells)));
}

export function renderBoard() {
  const body = state.board;
  const columns = (body && body.columns) || {};
  const repoFilter = boardRepoFilter();
  const visible = {};
  COLUMNS.forEach(function (col) {
    visible[col.key] = (columns[col.key] || []).filter(function (card) {
      // Telegram channel sessions leave the columns for the summary line (#1384).
      return !isHiddenChannel(card) && matchesRepoFilter(card, repoFilter);
    });
  });
  let expandedCard = null;
  if (state.boardExpanded) {
    COLUMNS.forEach(function (col) {
      expandedCard = expandedCard || visible[col.key].find(function (c) {
        return c.session_id === state.boardExpanded;
      }) || null;
    });
    // An open drawer whose card is gone (session ended) or filtered out has
    // nothing to render into; collapse it rather than hold an expanded id no
    // card matches.
    if (body && !expandedCard) state.boardExpanded = null;
  }
  // The poll keeps running with a drawer open (#958), so a render that still
  // shows the same drawer keeps its node — reply text, focus, a live mic and
  // the chief exchange poll all live on it.
  const openItem = openDrawerItem(expandedCard);
  const activeEl = openItem && openItem.contains(document.activeElement)
    ? document.activeElement : null;
  if (!openItem) {
    // Drawers that aren't kept are rebuilt — the chief exchange poll (#245)
    // must never outlive the DOM node it writes into.
    if (chiefExchangeTimer) {
      clearInterval(chiefExchangeTimer);
      chiefExchangeTimer = null;
    }
    // Same lifecycle rule for the drawer composer's mic (#755): reset()
    // before the rebuild below drops its DOM node, so a live or
    // still-finalizing recording is force-stopped and the mic mutex released
    // instead of wedging every other mic in the app.
    if (drawerComposer) {
      drawerComposer.reset();
      drawerComposer = null;
    }
  } else if (drawerComposer) {
    // A kept drawer's composer outlives the render that built it, so its
    // mic / OCR availability follows an /api/status that lands after it.
    drawerComposer.setAvailability(composerAvailability());
  }
  if (!body || !els.boardColumns) return;
  const ghLoaded = ghFetched(body);
  const liveRead = liveSessionsRead(body);

  // A drawer opened from outside its section — the ?board= deep link, a chief
  // chat send — unfolds that section once. After that the section's open
  // state is the user's again: the poll never re-opens one they folded.
  if (!expandedCard) {
    revealedDrawer = null;
  } else if (revealedDrawer !== state.boardExpanded) {
    revealedDrawer = state.boardExpanded;
    const home = COLUMNS.find(function (col) {
      return visible[col.key].indexOf(expandedCard) !== -1;
    });
    const section = home && els[home.section];
    if (section) section.open = true;
  }

  COLUMNS.forEach(function (col) {
    const cards = visible[col.key];
    // A live column can still hold external cards (hook state + fresh
    // transcript) with the session-host down; those make a real lower bound,
    // so only an empty one reads as unknown.
    const unknown = (col.gh === 'all' && !ghLoaded)
      || (col.live && !liveRead && cards.length === 0);
    const shown = unknown ? '—' : String(cards.length);
    const section = els[col.section];
    if (section) {
      const count = section.querySelector('.board-count');
      if (count) count.textContent = shown;
      section.classList.toggle('attention', col.key === 'your_turn' && cards.length > 0);
      // At the wide query an empty lane is a one-line strip (#1502).
      section.classList.toggle('is-empty', cards.length === 0);
      // PRs and jobs, folded, says what is wrong instead of its count.
      if (col.key === 'other') {
        const failing = cards.filter(jobFailing).length;
        setLaneException(section, failing ? failing + ' failing' : '', 'danger');
      }
    }
    const list = els.boardColumns.querySelector('.board-list[data-col="' + col.key + '"]');
    const empty = els.boardColumns.querySelector('.board-empty[data-col="' + col.key + '"]');
    if (!list) return;
    placeChildren(list, cards.map(function (card) {
      return renderCard(col.key, card, openItem);
    }));
    if (empty) {
      // The canonical empty-state block (#1133), rebuilt each render: a
      // muted glyph over the one-line reason, instead of a bare sentence in
      // a dashed box. `empty` stays the hidden/shown container the poll
      // toggles, so nothing else about the column changes.
      // Rebuilt only when its words change: the 5 s poll would otherwise
      // replace the action button under a keyboard user's focus.
      const text = emptyText(col, body, ghLoaded, liveRead);
      const action = emptyAction(col, liveRead);
      const sig = text + '|' + action.actionLabel;
      if (empty.dataset.sig !== sig) {
        empty.dataset.sig = sig;
        empty.replaceChildren(emptyStateEl(col.glyph, text, action));
      }
      empty.hidden = cards.length > 0;
    }
  });

  sizeLaneGrid();

  // A card that changed column moved its kept <li> between lists, which
  // blurs; hand the focus back.
  if (activeEl && document.activeElement !== activeEl && activeEl.isConnected) {
    activeEl.focus({ preventScroll: true });
  }

  // The open Backlog kebab's row is gone (issue closed, filtered out).
  issueMenu.endRender();
  // A kept drawer keeps its menu; one rebuilt re-attached it; none, ends it.
  if (!openItem) drawerMenu.endRender();
  renderBacklogFoot(body, ghLoaded);
  renderChiefPlan(body, liveRead);
  renderStatusLine(body);
  renderBoardHead(body, ghLoaded, liveRead);
  renderBoardBadge();
  // Keep the lane toolbar's project list in step with a project list that
  // may land after the first render (/api/apps).
  syncDispatchBar();
}

// ----------------------------------------------------------------- fetch

export async function fetchBoard() {
  // Self-gate: costs nothing while another tab is up (pattern: fetchJobs).
  // No drawer gate (#958): the chief chat keeps a drawer open for hours, and
  // pausing on it froze the whole Board silently. renderBoard() keeps the
  // open drawer's node instead, so a poll can't wipe a reply being typed.
  if (state.tab !== 'board') return;
  if (!state.board) setAsyncState(els.boardFetchState, 'loading', { message: 'Reading the Board…' });
  const editSeq = boardEditSeq;
  // The chief's plan rides the same poll (#1279). Its failure is the card's
  // own quiet note, never the Board's error.
  let board, plan;
  try {
    [board, plan] = await Promise.all([
      jsonApi('/api/board'),
      jsonApi('/api/board/chief-plan').catch(function () { return { state: 'unavailable' }; }),
    ]);
  } catch (exc) {
    // A failed first read replaces the lanes, which would only show zeros;
    // a later one keeps them under a stale line (#1501).
    failedFetchState(els.boardFetchState, !!state.board, state.boardFetchedAt,
      'the Board', function () { fetchBoard().catch(function () {}); });
    if (els.boardColumns) els.boardColumns.hidden = !state.board;
    throw exc;
  }
  // Read before a rename landed: it would put the old title back (#1573).
  if (editSeq !== boardEditSeq) return;
  setAsyncState(els.boardFetchState, 'ready');
  if (els.boardColumns) els.boardColumns.hidden = false;
  state.board = board;
  state.boardFetchedAt = Date.now();
  state.chiefPlan = plan;
  renderBoard();
  // A never-fetched cache (the webapp restarted since the last refresh) heals
  // on the poll too, not only on tab activation — a Board left open across a
  // restart would otherwise sit on "not loaded" until ↻ (#910). Still never
  // an errored cache (that is ghStale's rule), and throttled so a refresh
  // that throws before recording its error can't become a 5 s gh loop.
  const gh = state.board && state.board.github;
  if (gh && !ghFetched(state.board) && !gh.error && Date.now() - lastAutoRefreshAt > GH_STALE_MS) {
    lastAutoRefreshAt = Date.now();
    refreshGithub().catch(function () {});
  }
}

// ?board=<sid> deep-link (#301): land on the Board with that card's drawer
// open, its section unfolded and scrolled into view. Called from main.js at boot. `sid` may
// be a card's own session_id OR its state_sid (#307) — a Slack ping only ever
// knows the hook's transcript UUID (fleet-config#242), which is the card's
// state_sid, not its session-host session_id.
export async function openBoardCard(sid) {
  setTab('board');
  // Distinguish a failed fetch from a genuinely-missing sid (#316): a transient
  // fetchBoard() failure (auth flip, gh cache warming, backend restart) leaves
  // columns empty, which must NOT read as "session gone". Retry once, then, if
  // still failing, surface a distinct "refresh failed" toast.
  let fetchOk = true;
  try {
    await fetchBoard();
  } catch (_) {
    try {
      await fetchBoard();
    } catch (_2) {
      fetchOk = false;
    }
  }
  if (!fetchOk) {
    toast('Board refresh failed — tap Refresh to retry.', 'error');
    return;
  }
  const columns = (state.board && state.board.columns) || {};
  let matchedCard = null;
  const colKey = Object.keys(columns).find(function (key) {
    return (columns[key] || []).some(function (c) {
      if (c.session_id === sid || c.state_sid === sid) {
        matchedCard = c;
        return true;
      }
      return false;
    });
  });
  if (!colKey || !matchedCard) {
    // Fetch succeeded but the sid isn't there — session genuinely gone
    // (stopped between the ping and the tap). Leave the board browsable.
    toast('Session not on the board any more.', 'error');
    return;
  }
  // Expand by the card's real session_id — every other read of boardExpanded
  // (the card-click toggle, the drawer-open check) compares against
  // card.session_id, so expanding by a state_sid would never match.
  state.boardExpanded = matchedCard.session_id;
  renderBoard();
  requestAnimationFrame(function () {
    const item = els.boardColumns.querySelector('li.board-item.expanded');
    if (item) item.scrollIntoView({ block: 'nearest' });
  });
}

// Stale = never fetched, or older than GH_STALE_MS. An errored cache is
// never auto-retried — that would hammer a broken gh; ↻ stays manual.
function ghStale(body) {
  if (!body || !body.github || body.github.error) return false;
  const t = Date.parse(body.github.fetched_at || '');
  return isNaN(t) || Date.now() - t > GH_STALE_MS;
}

// While a refresh runs the ↻ spins (#1502, COLOR-02): aria-busy, never the
// disabled look, whose line colour measured 1.55:1 on dark. A second tap is
// a no-op (refreshInFlight).
function setRefreshBusy(busy) {
  els.boardRefresh.setAttribute('aria-busy', busy ? 'true' : 'false');
  if (busy) els.boardRefresh.setAttribute('aria-disabled', 'true');
  else els.boardRefresh.removeAttribute('aria-disabled');
}

async function refreshGithub() {
  if (refreshInFlight) return;
  refreshInFlight = true;
  setRefreshBusy(true);
  try {
    const github = await jsonApi('/api/board/github/refresh', { method: 'POST' });
    if (github && github.error) {
      toast('GitHub refresh failed: ' + github.error, 'error');
    }
    await fetchBoard();
  } finally {
    refreshInFlight = false;
    setRefreshBusy(false);
  }
}

// --------------------------------------------------------- column sections

const DESKTOP_BOARD = '(min-width: 700px) and (pointer: fine)';

// The desktop grid is a kanban at a glance, so all five sections start open
// there; the phone keeps the markup's default (the two live columns open).
// Boot only — after that a section's open state is the user's.
function openDesktopColumns() {
  if (!window.matchMedia || !window.matchMedia(DESKTOP_BOARD).matches) return;
  COLUMNS.forEach(function (col) {
    const section = els[col.section];
    if (section) section.open = true;
  });
}

// ------------------------------------------------------------------ wire

export function wireBoard() {
  if (!els.tabBoard) return;
  els.tabBoard.addEventListener('click', function () {
    syncDispatchBar();
    fetchBoard().then(function () {
      // Opening the tab with a stale (or never-filled) gh cache refreshes
      // it once; while the tab just sits open only the free poll runs.
      if (ghStale(state.board)) refreshGithub().catch(function () {});
    }).catch(function () {});
  });
  wireDispatch();
  wireChiefAnswers();
  openDesktopColumns();
  els.boardRefresh.addEventListener('click', function () {
    refreshGithub().catch(function (exc) {
      apiFailToast('GitHub refresh failed', exc);
    });
  });
}
