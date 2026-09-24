/* Board tab (issues #300 / #301 / #302 / #164 / #399 / #608): the fleet
 * kanban.
 *
 * Five computed columns from GET /api/board, each single-purpose — Backlog
 * (open issues), Claude's turn (sessions working/unknown/idle/idle-finished/
 * tool-pending), Your turn (stalled/awaiting-decision/awaiting-input
 * sessions only — #608's split of the old undifferentiated needs-you,
 * sharpened by #813's tool-pending carve-out), Other (open PRs +
 * failed/unconfirmed/stuck jobs), Done (closed issues today). Each column is a
 * collapsible section card like every other tab's (#1198) — stacked on the
 * phone, side by side on the desktop grid — with its count in the summary.
 *
 * Cost discipline: fetchBoard() self-gates on the Board tab being visible
 * (pattern: fetchJobs / fetchRunningApps); the server's gh cache is only
 * refreshed via the ↻ button, on tab activation when the cache is older
 * than GH_STALE_MS, or on a poll that finds it never fetched (a webapp
 * restart empties it, #910) — never on a poll of an already-fetched cache.
 *
 * Act-from-the-card loop (#301): tapping a live session card opens an
 * inline drawer with the last user↔assistant exchange (passkey-gated — it
 * is transcript text), the shared composer (#984) and Rename · Stop · Chat ·
 * Terminal;
 * backlog cards of repos present in the projects folder carry ▶ Start /
 * ⚡ YOLO one-tap `/issue-*` launches; `?board=<sid>` deep-links onto a
 * card with its drawer open. The poll keeps running while a drawer is open
 * (#958 — the chief chat holds one open for hours), and renderBoard() keeps
 * the open drawer's own node across the re-render, so it can never wipe a
 * reply being typed. Issue/PR/done cards open GitHub, job cards (Other
 * column) jump to the Jobs tab.
 *
 * Split off a single-file module (issue #691, `/codebase-audit`), the way
 * `jobs.js` and `terminal.js` already were: the dispatch bar above the
 * columns — free-text dispatch (#302), the repo/project combo that doubles
 * as the card filter (#337), chat mode and the whole fleet-chief lifecycle
 * plus its settings dialog (#245/#547) — lives in `board-dispatch.js`. This
 * module keeps card rendering, the drill-down drawer, one-tap issue-start
 * and the column sections, and calls into that one for the bar.
 */

import { els, state } from './state.js';
import { apiFailToast, authHeaders, escapeHtml, isDesktopClient, jsonApi, toast } from './api.js';
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
import { ensureTerminalToken } from './webauthn.js';
import { CHIEF_KILL_CONFIRM, brandIconEl, fmtDuration, renderQuotaLines } from './dom-utils.js';
import {
  boardRepoFilter,
  getBoardDispatchModel,
  isChiefCard,
  matchesRepoFilter,
  syncDispatchBar,
  wireDispatch,
} from './board-dispatch.js';

// `gh` marks where a column's cards come from (#910): 'all' columns are
// GitHub-only, so before the cache is loaded their count is unknown, never
// zero; 'part' (Other) mixes open PRs with job cards, whose count stays real.
// `live` columns are built from the session-host list, so an unreachable
// session-host makes them unknown too (#915).
const COLUMNS = [
  // Each empty sentence names the control that fills its lane (#1176).
  { key: 'backlog', section: 'boardColBacklog', empty: 'No open issues — tap Refresh to check GitHub again.', glyph: 'git-branch', gh: 'all' },
  { key: 'claude_turn', section: 'boardColClaude', empty: 'No sessions on Claude’s side — start one from the dispatch bar above.', glyph: 'hourglass', live: true },
  { key: 'your_turn', section: 'boardColYours', empty: 'Nothing needs you right now — start work from the dispatch bar above.', glyph: 'circle-check', live: true },
  { key: 'other', section: 'boardColOther', empty: 'No open PRs or stuck jobs — tap Refresh to check GitHub again.', glyph: 'git-pull-request', gh: 'part' },
  { key: 'done', section: 'boardColDone', empty: 'Nothing closed today yet — tap Refresh to check GitHub again.', glyph: 'square-check', gh: 'all' },
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


// #608: the hook-written needs-you is split server-side into five
// caller-actionable values (src/board_transcript.py::_refine_waiting_status)
// — the raw "needs-you" string itself never reaches a card's status field.
// tool-pending (#813) is a pending ordinary tool_use — genuinely ambiguous
// between "still executing" and "permission-gated", so it renders as an
// ambient working-ish state rather than an alert.
const STATUS_META = {
  working: { icon: 'zap', text: 'working', cls: 'is-working' },
  'tool-pending': { icon: 'activity', text: 'running', cls: 'is-tool-pending' },
  stalled: { icon: 'hourglass', text: 'stalled', cls: 'is-stalled' },
  'awaiting-decision': { icon: 'sparkle', text: 'awaiting decision', cls: 'is-awaiting-decision' },
  'awaiting-input': { icon: 'sparkle', text: 'needs you', cls: 'is-awaiting-input' },
  'idle-finished': { icon: 'circle-check', text: 'finished', cls: 'is-idle-finished' },
  idle: { icon: 'moon', text: 'idle', cls: 'is-idle' },
  unknown: { icon: null, text: '', cls: 'is-unknown' },
};

// The chief's Stop-hook status sits in the needs-you family for nearly all
// of its life between dispatches (#575) — the server already routes it out
// of Your turn (build_board's _is_chief_card carve-out). #608 sharpened
// what "resting state" actually means: idle-finished/awaiting-input/
// awaiting-decision are all ordinary waiting for a long-lived chat session,
// so they get the same "standing by" label. stalled is deliberately
// excluded — a chief dispatch that's been outstanding this long is a real
// anomaly worth surfacing, not hiding behind a benign label.
const CHIEF_STANDING_BY_STATUSES = new Set(['idle-finished', 'awaiting-input', 'awaiting-decision']);
const CHIEF_STANDING_BY_META = { icon: 'moon', text: 'standing by', cls: 'is-idle' };

// ----------------------------------------------------------------- cards

// Every Board card leads with its title (#1198), clamped to two lines, and
// the full text stays reachable through the element's `title` attribute.
function cardTitleEl(cls, text) {
  const title = document.createElement('span');
  title.className = cls;
  title.textContent = text;
  if (text) title.title = text;
  return title;
}

function cardShell(iconName, metaText, titleText, cls) {
  const li = document.createElement('li');
  li.className = 'app-item board-item' + (cls ? ' ' + cls : '');
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'launch-btn board-card';
  const meta = document.createElement('span');
  meta.className = 'board-card-meta';
  if (iconName) {
    const ic = document.createElement('span');
    ic.className = 'board-card-meta-icon';
    ic.innerHTML = icon(iconName);
    meta.appendChild(ic);
  }
  // Data (repo/project/session names) rides a text node — never innerHTML.
  meta.appendChild(document.createTextNode(metaText));
  btn.appendChild(cardTitleEl('board-card-title', titleText));
  btn.appendChild(meta);
  li.appendChild(btn);
  return { li: li, btn: btn };
}

// `openItem` is the open drawer's current <li> when renderBoard() can keep it
// (#958); the card's fresh header is swapped into it instead of a new drawer.
function renderSessionCard(card, openItem) {
  const meta =
    isChiefCard(card) && CHIEF_STANDING_BY_STATUSES.has(card.status)
      ? CHIEF_STANDING_BY_META
      : STATUS_META[card.status] || STATUS_META.unknown;
  const bits = [card.project || '', meta.text, fmtDuration(card.age_seconds)].filter(Boolean);
  const shell = cardShell(meta.icon, ' ' + bits.join(' · '), sessionLabel(card), meta.cls);
  // The chief's card is visually distinct (#245): accent tint + crown, so
  // the standing orchestrator never blends in with worker sessions.
  if (isChiefCard(card)) {
    shell.li.classList.add('board-item-chief');
    const crown = document.createElement('span');
    crown.className = 'board-card-meta-icon board-chief-crown';
    crown.innerHTML = icon('crown');
    const chiefMeta = shell.btn.querySelector('.board-card-meta');
    chiefMeta.insertBefore(crown, chiefMeta.firstChild);
  }
  // The Board now includes every launcher-owned agent, not only Claude Code
  // (#455). Show the same registry-backed brand identity as the Coding tab so
  // an unknown/degraded status never hides which terminal the card belongs to.
  const known = state.agents.find(function (a) { return a.id === card.agent; });
  const agentId = String(card.agent || 'claude');
  const agentIcon = brandIconEl(
    agentId, 'session-agent-icon board-agent-icon', known ? known.label : agentId
  );
  const metaLine = shell.btn.querySelector('.board-card-meta');
  metaLine.insertBefore(agentIcon, metaLine.firstChild);
  if (card.session_id) {
    shell.li.dataset.sessionId = card.session_id;
    // Tap toggles the drill-down drawer (#301); the ⚡ button inside it is
    // the way into the full terminal now.
    shell.btn.addEventListener('click', function () {
      state.boardExpanded =
        state.boardExpanded === card.session_id ? null : card.session_id;
      renderBoard();
      if (!state.boardExpanded) fetchBoard().catch(function () {});
    });
    if (state.boardExpanded === card.session_id) {
      if (openItem) {
        // The header button is always the <li>'s first child, the drawer
        // after it — only the header is replaced.
        openItem.className = shell.li.className + ' expanded';
        openItem.replaceChild(shell.btn, openItem.firstElementChild);
        return openItem;
      }
      shell.li.classList.add('expanded');
      shell.li.dataset.drawerShape = drawerShape(card);
      shell.li.appendChild(buildDrawer(card));
    }
  } else {
    shell.btn.classList.add('inert');
    shell.btn.disabled = true;
  }
  return shell.li;
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
    upload: function (file) { return uploadSessionFile(card.session_id, file); },
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

// One of the drawer's four equal actions (mockup screen 8): glyph over label.
// An action the session can't take stays in the row, aria-disabled so a tap
// still reaches it and toasts the reason (a phone has no hover) — the same
// convention as the overlay's mode segments (#982) — so the row never
// changes shape.
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

function buildDrawer(card) {
  const drawer = document.createElement('div');
  drawer.className = 'board-drawer';

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

  // One row of four equal actions — Rename · Stop · Chat · Terminal, with
  // Terminal deliberately last (#496 round 2).
  const actions = document.createElement('div');
  actions.className = 'board-drawer-actions';
  // Rename: the launcher-native override (no PTY needed). The drawer stays
  // open across a rename (unlike Chat/Terminal, which navigate away), so the
  // completion callback patches the card optimistically rather than waiting
  // for the next poll. A kept drawer (#958) can outlive the payload its
  // `card` came from, so the patch goes to the card in the current payload.
  actions.appendChild(drawerAction(
    'board-rename-btn', 'pencil', 'Rename', 'Rename this session', null,
    function () {
      openSessionRename(card, function (title) {
        card.manual_title = title;
        const current = boardCard(card.session_id);
        if (current) current.manual_title = title;
        renderBoard();
      });
    }
  ));
  // Stop: the unified stop path the Coding tab's row menu uses for both
  // kinds (#253: the agent's own quit, force-fallback server-side), one tap,
  // no confirm — except the chief (#245), the one session a mis-tap
  // shouldn't take down.
  actions.appendChild(drawerAction(
    'board-stop-btn', 'x', 'Stop', 'Stop and kill this session', null,
    async function (btn) {
      if (isChiefCard(card) && !confirm(CHIEF_KILL_CONFIRM)) return;
      btn.disabled = true;
      // Close the drawer first — the session it belongs to is going away.
      state.boardExpanded = null;
      renderBoard();
      await stopSession({ session_id: card.session_id, name: sessionLabel(card) });
      // The host stops gracefully (quit → force) — give it the same beat
      // sessions.js gives fetchSessions before reconciling the board.
      setTimeout(function () { fetchBoard().catch(function () {}); }, 1500);
    }
  ));
  const session = overlaySession(card);
  actions.appendChild(drawerAction(
    'board-open-chat', 'messages-square', 'Chat', 'Open the chat view',
    chatAvailable(session) ? null : modeReason(session, 'chat'),
    function () {
      state.boardExpanded = null;
      openSessionOverlay(session, 'chat');
    }
  ));
  actions.appendChild(drawerAction(
    'board-open-terminal', 'terminal', 'Terminal', 'Open the full terminal',
    terminalAvailable(session) ? null : modeReason(session, 'terminal'),
    function () {
      state.boardExpanded = null;
      openTerminal(session);
    }
  ));
  drawer.appendChild(actions);
  return drawer;
}

async function loadExchange(card, el) {
  try {
    const tt = await ensureTerminalToken();
    const body = await jsonApi(
      '/api/board/sessions/' + encodeURIComponent(card.session_id) + '/exchange',
      { headers: authHeaders({ terminalToken: tt }) }
    );
    el.replaceChildren();
    if (!body.available) {
      el.dataset.state = body.reason === 'no_exchange' ? 'empty' : 'error';
      const reasons = {
        no_exchange: 'No exchange yet.',
        session_not_found: 'Session ended — refresh the Board.',
        native_unavailable: 'Conversation preview unavailable — open the terminal.',
        capture_unparseable: 'Conversation preview unavailable — open the terminal.',
      };
      el.textContent = reasons[body.reason] ||
        'Conversation preview unavailable — open the terminal.';
      return;
    }
    el.dataset.state = 'ready';
    if (body.user && body.user.text) {
      const u = document.createElement('div');
      u.className = 'board-exchange-user';
      u.textContent = body.user.text;
      el.appendChild(u);
    }
    if (body.assistant && body.assistant.text) {
      const a = document.createElement('div');
      a.className = 'board-exchange-assistant';
      a.textContent = body.assistant.text;
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

// Git state for a backlog card's repo (#496 item 4), read from the SAME
// client-side cache the Coding tiles use (state.gitStatus, keyed by the
// scanner's project id — resolved here via the repo-name → project match
// repoInProjects uses). Null until the boot git fetch lands, or when the
// repo isn't in the projects folder — the card just renders unannotated.
function repoGitStatus(repo) {
  if (!repo || !state.gitStatus) return null;
  const app = (state.apps || []).find(function (a) {
    return a.kind === 'claude-code' &&
      String(a.name).toLowerCase() === String(repo).toLowerCase();
  });
  const gs = app && state.gitStatus[app.id];
  return (gs && gs.is_git) ? gs : null;
}

async function startIssue(card, mode, btn) {
  btn.disabled = true;
  try {
    const tt = await ensureTerminalToken();
    // Carry the issue title so the server can auto-name the spawned session
    // after it (#467) — display data, never reaches the command line.
    const payload = {
      repo: card.repo, number: card.number, mode: mode,
      title: card.title || '',
      // The dispatch bar's model selector governs one-tap starts too
      // (#505), overriding the shared Coding model per launch.
      model: getBoardDispatchModel(),
    };
    // Desktop browsers get the PC mirror window, like every launch (#241).
    // Phone launches carry the real terminal size so the PTY's early
    // output is authored at the width the overlay will fit() to (issue
    // #374); the route already accepts rows/cols.
    applyLaunchSizePayload(payload);
    const body = await jsonApi('/api/board/issues/start', {
      method: 'POST',
      headers: authHeaders({ terminalToken: tt, contentType: 'application/json' }),
      body: JSON.stringify(payload),
    });
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

// Backlog issue tiles (#337 follow-up, restyled #339): a flat separator
// row — no card background/border, just a bottom-border divider between
// rows (GitHub-issue-list style) — with the title first (two lines at most,
// #1198) and repo/# under it, and icon-only ▶/⚡ actions vertically centered
// against the whole row. Doesn't use cardShell() (that's the bordered-box layout the other
// card kinds keep); the <li> itself is the flex row so the text stack and the
// action icons sit side by side without nesting a <button> inside a <button>.
function renderIssueCard(card) {
  const li = document.createElement('li');
  li.className = 'app-item board-item board-item-issue';
  // Claim owner liveness (#948): 'dead' is a provably gone lane — not in
  // progress, startable again; 'unknown' keeps the in-progress lock but says
  // the owner could not be verified.
  const isInProgress = card.in_progress === true;
  const claimUnverified = isInProgress && card.claim_state === 'unknown';
  const claimStale = card.claim_state === 'dead';
  if (isInProgress) li.classList.add('is-in-progress');

  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'launch-btn board-card board-card-flat';
  const textCol = document.createElement('span');
  textCol.className = 'board-card-text';
  const meta = document.createElement('span');
  meta.className = 'board-card-meta-inline';
  meta.textContent = [card.repo, '#' + card.number].filter(Boolean).join(' ');
  if (claimUnverified) meta.textContent += ' · in progress (unverified)';
  else if (isInProgress) meta.textContent += ' · in progress';
  else if (claimStale) meta.textContent += ' · stale claim';
  // Repo-state colour (#496 item 4): red = dirty working tree, yellow =
  // parked off the default branch — "don't start this issue right now".
  // Same precedence as the Coding tiles: red wins when both apply.
  const gs = repoGitStatus(card.repo);
  if (gs) {
    if (gs.dirty) meta.classList.add('git-dirty');
    else if (gs.branch && !gs.on_default_branch) meta.classList.add('git-off-main');
    if (gs.branch && !gs.on_default_branch) {
      meta.title = 'repo on ' + gs.branch + (gs.dirty ? ' · uncommitted changes' : '');
    } else if (gs.dirty) {
      meta.title = 'repo has uncommitted changes';
    }
  }
  textCol.appendChild(cardTitleEl('board-card-title-compact', card.title || ''));
  textCol.appendChild(meta);
  btn.appendChild(textCol);
  li.appendChild(btn);
  if (card.url) {
    btn.addEventListener('click', function () {
      window.open(card.url, '_blank', 'noopener');
    });
  }

  // One-tap start (#301) — only for repos the Coding tab could launch in.
  if (card.number && repoInProjects(card.repo)) {
    const row = document.createElement('div');
    row.className = 'board-issue-actions board-issue-actions-compact';
    [['start', 'play', 'Start'], ['yolo', 'zap', 'YOLO']].forEach(function (pair) {
      const actionBtn = document.createElement('button');
      actionBtn.type = 'button';
      actionBtn.className = 'board-issue-btn icon-only';
      actionBtn.innerHTML = icon(pair[1]);
      actionBtn.disabled = isInProgress;
      actionBtn.title = isInProgress
        ? 'Issue #' + card.number + ' is already in progress' +
          (claimUnverified ? ' (owner unverified)' : '')
        : '/issue-' + pair[0] + ' ' + card.number + ' in ' + card.repo;
      actionBtn.setAttribute('aria-label', pair[2] + ' issue #' + card.number);
      actionBtn.addEventListener('click', function () {
        startIssue(card, pair[0], actionBtn);
      });
      row.appendChild(actionBtn);
    });
    li.appendChild(row);
  }
  return li;
}

function renderPrCard(card) {
  const draft = card.is_draft ? ' · draft' : '';
  const shell = cardShell('git-pull-request', ' ' + [card.repo, 'PR #' + card.number].join(' ') + draft,
    card.title || '', '');
  if (card.url) {
    shell.btn.addEventListener('click', function () {
      window.open(card.url, '_blank', 'noopener');
    });
  }
  return shell.li;
}

const JOB_CARD_ICONS = {
  // `unconfirmed` (#916): the run may well have delivered, but the
  // scheduled-run adapter could not establish that. It still wants a look, so
  // it keeps its card — with the attention glyph and accent, never the red ✗
  // that says the run is known to have failed.
  // `unreadable` (#915): the same shape one step earlier — the run history
  // itself could not be read, so whether the job needs attention is unknown.
  // It must not fall through to the red ✗ either.
  stuck: 'triangle-alert',
  unconfirmed: 'circle-help',
  unreadable: 'triangle-alert',
};

function renderJobCard(card) {
  const iconName = JOB_CARD_ICONS[card.state] || 'x';
  const label = card.state === 'unconfirmed' ? 'not confirmed' : card.state;
  const top = ' job · ' + label + (card.age_seconds != null ? ' · ' + fmtDuration(card.age_seconds) : '');
  const shell = cardShell(iconName, top, card.job_name || card.job_id || 'job', 'is-' + card.state);
  if (card.error) shell.btn.title = card.error;
  shell.btn.addEventListener('click', function () { setTab('jobs'); });
  return shell.li;
}

function renderDoneCard(card) {
  // Done holds closed issues only (#399) — a merged PR that closed one is
  // already reflected here by the issue itself, so there's no PR/pairing
  // branch to render.
  const shell = cardShell(
    'square-check',
    ' ' + [card.repo, '#' + card.number].join(' ') + ' · ' + card.state,
    card.title || '', '');
  if (card.url) {
    shell.btn.addEventListener('click', function () {
      window.open(card.url, '_blank', 'noopener');
    });
  }
  return shell.li;
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
  return failed ? 'GitHub fetch failed — tap Refresh to retry.'
    : 'Not loaded from GitHub yet — tap Refresh.';
}

function renderStatusLine(body) {
  const parts = [];
  if (body.github && body.github.error) {
    parts.push(icon('triangle-alert') + ' GitHub: ' + escapeHtml(body.github.error));
  } else if (body.github && !body.github.fetched_at) {
    parts.push('GitHub not fetched yet — tap Refresh');
  }
  if (!liveSessionsRead(body)) {
    parts.push(icon('triangle-alert') + ' session-host unreachable — live sessions unknown');
  }
  if (body.sessions_state && !body.sessions_state.available) {
    parts.push('session state unavailable (hooks not writing yet)');
  } else if (body.sessions_state && body.sessions_state.stale) {
    parts.push(icon('triangle-alert') + ' session state stale');
  }
  els.boardStatus.innerHTML = parts.join(' · ');
  els.boardStatus.hidden = parts.length === 0;
}

// Quota rows (issues #326/#860) — a separate element from boardStatus on
// purpose: that one is transient-problem text that vanishes once the problem
// clears, while these are live content that should persist (dimmed, not
// hidden) even when the cache is stale. Both agents always show, since this
// is the tab the heavier sessions get launched from and the number you need
// is the one for the agent you have *not* selected. Rendering is shared with
// the Coding tab — see dom-utils.js::renderQuotaLines.

export function renderBoard() {
  const body = state.board;
  const columns = (body && body.columns) || {};
  const repoFilter = boardRepoFilter();
  const visible = {};
  COLUMNS.forEach(function (col) {
    visible[col.key] = (columns[col.key] || []).filter(function (card) {
      return matchesRepoFilter(card, repoFilter);
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
      empty.replaceChildren(
        emptyStateEl(col.glyph, emptyText(col, body, ghLoaded, liveRead))
      );
      empty.hidden = cards.length > 0;
    }
  });

  // A card that changed column moved its kept <li> between lists, which
  // blurs; hand the focus back.
  if (activeEl && document.activeElement !== activeEl && activeEl.isConnected) {
    activeEl.focus({ preventScroll: true });
  }

  renderStatusLine(body);
  renderQuotaLines(els.boardUsage, body.quota_lines);
  // Keep the dispatch bar's repo list + mic visibility in step with state
  // that may land after the first render (/api/apps, /api/status).
  syncDispatchBar();
}

// ----------------------------------------------------------------- fetch

export async function fetchBoard() {
  // Self-gate: costs nothing while another tab is up (pattern: fetchJobs).
  // No drawer gate (#958): the chief chat keeps a drawer open for hours, and
  // pausing on it froze the whole Board silently. renderBoard() keeps the
  // open drawer's node instead, so a poll can't wipe a reply being typed.
  if (state.tab !== 'board') return;
  state.board = await jsonApi('/api/board');
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

async function refreshGithub() {
  if (refreshInFlight) return;
  refreshInFlight = true;
  els.boardRefresh.disabled = true;
  els.boardRefresh.textContent = '…';
  try {
    const github = await jsonApi('/api/board/github/refresh', { method: 'POST' });
    if (github && github.error) {
      toast('GitHub refresh failed: ' + github.error, 'error');
    }
    await fetchBoard();
  } finally {
    refreshInFlight = false;
    els.boardRefresh.disabled = false;
    els.boardRefresh.innerHTML = icon('refresh-cw');
  }
}

// --------------------------------------------------------- column sections

// ↻ has one node (board.js mutates it by id) and two homes: beside the
// project filter on the phone, where the filter is the last row of the
// Dispatch card, directly over the columns it filters; and last in the
// dispatch control row on the >=700px desktop bar (#869), where the filter
// sits at the row's far left. CSS can't move a node between containers, so
// it is re-parented on the breakpoint.
const DESKTOP_BOARD = '(min-width: 700px) and (pointer: fine)';

function dockRefresh() {
  const btn = els.boardRefresh;
  if (!btn || !window.matchMedia) return;
  const mq = window.matchMedia(DESKTOP_BOARD);
  function place() {
    const home = document.querySelector(
      mq.matches ? '.board-dispatch-row' : '.board-filter-row'
    );
    if (home && btn.parentNode !== home) home.appendChild(btn);
  }
  place();
  // Safari <14 has no addEventListener on MediaQueryList.
  if (mq.addEventListener) mq.addEventListener('change', place);
  else if (mq.addListener) mq.addListener(place);
}

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
  dockRefresh();
  openDesktopColumns();
  els.boardRefresh.addEventListener('click', function () {
    refreshGithub().catch(function (exc) {
      apiFailToast('GitHub refresh failed', exc);
    });
  });
}
