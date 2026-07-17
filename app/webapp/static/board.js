/* Board tab (issues #300 / #301 / #302 / #164 / #399): the fleet kanban.
 *
 * Five computed columns from GET /api/board, each single-purpose — Backlog
 * (open issues), Claude's turn (sessions working/unknown/idle), Your turn
 * (needs-you sessions only), Other (open PRs + failed/stuck jobs), Done
 * (closed issues today). Phone-first: the columns container is a scroll-snap
 * carousel (one column per swipe) and the strip above it doubles as column
 * switcher + counts.
 *
 * Cost discipline: fetchBoard() self-gates on the Board tab being visible
 * (pattern: fetchJobs / fetchRunningApps); the server's gh cache is only
 * refreshed via the ↻ button or on tab activation when the cache is older
 * than GH_STALE_MS — never on the 5 s poll, never while just looking at it.
 *
 * Act-from-the-card loop (#301): tapping a live session card opens an
 * inline drawer with the last user↔assistant exchange (passkey-gated — it
 * is transcript text) and a reply box that writes straight into the PTY;
 * backlog cards of repos present in the projects folder carry ▶ Start /
 * ⚡ YOLO one-tap `/issue-*` launches; `?board=<sid>` deep-links onto a
 * card with its drawer open. While a drawer is open the poll pauses, so a
 * re-render can never wipe a reply being typed. Issue/PR/done cards open
 * GitHub, job cards (Other column) jump to the Jobs tab.
 *
 * Free-text dispatch (#302): the bar pinned above the columns speaks/types
 * a goal into a fresh /issue-add | /issue-add now | /issue-yolo session in
 * any repo of the projects folder. The goal rides POST /api/board/dispatch,
 * which spawns prompt-free and *types* the command into the PTY server-side
 * (spawn-then-type — free text never touches the spawn command line). The
 * bar keeps its text after a send so rapid multi-dispatch works; ✕ clears.
 * The bar is static markup renderBoard() never touches, so the 5 s poll
 * can't wipe a goal being typed. Dictation mics (shared voice.js) mount on
 * the goal box and on each drawer reply box.
 */

import { els, state } from './state.js';
import { apiFailToast, authHeaders, isDesktopClient, jsonApi, toast } from './api.js';
import { setTab } from './tabs.js';
import { openSessionRename, sessionTitle, stopSession } from './sessions.js';
import { applyLaunchSizePayload, openTerminal } from './terminal.js';
import { createDictation, startWorkTimer, voiceDictationAvailable } from './voice.js';
import { icon } from './_vendored/icons/icons.js';
import { ensureTerminalToken } from './webauthn.js';
import { iconUrl, renderUsageBadgeRow } from './dom-utils.js';

const COLUMNS = [
  { key: 'backlog', btn: 'boardColBacklog', empty: 'No open issues cached — tap ↻ to fetch from GitHub.' },
  { key: 'claude_turn', btn: 'boardColClaude', empty: 'No sessions on Claude’s side.' },
  { key: 'your_turn', btn: 'boardColYours', empty: 'Nothing needs you right now.' },
  { key: 'other', btn: 'boardColOther', empty: 'No open PRs or stuck jobs.' },
  { key: 'done', btn: 'boardColDone', empty: 'Nothing closed today yet.' },
];

const GH_STALE_MS = 2 * 60 * 1000;

let refreshInFlight = false;

// --------------------------------------------------------------- helpers

function fmtAge(seconds) {
  if (seconds == null || isNaN(seconds)) return '';
  if (seconds < 60) return 'now';
  if (seconds < 3600) return Math.floor(seconds / 60) + 'm';
  if (seconds < 86400) return Math.floor(seconds / 3600) + 'h';
  return Math.floor(seconds / 86400) + 'd';
}

// The exact same title resolution as the Coding tab's Running-sessions list
// (#396) — board cards carry the same field names (shared_name/
// shared_name_source, live_title, prompt_title, name, project) that
// sessionTitle() reads, so a single shared function keeps both tabs
// agreeing on one session's title instead of two independently-drifting
// codepaths (#383 review round first duplicated a smaller version of this).
function sessionLabel(card) {
  return sessionTitle(card) || card.project || 'session';
}

const STATUS_META = {
  working: { icon: 'zap', text: 'working', cls: 'is-working' },
  'needs-you': { icon: 'sparkle', text: 'needs you', cls: 'is-needs-you' },
  idle: { icon: 'moon', text: 'idle', cls: 'is-idle' },
  unknown: { icon: null, text: '', cls: 'is-unknown' },
};

// ----------------------------------------------------------------- cards

function cardShell(iconName, topText, titleText, cls) {
  const li = document.createElement('li');
  li.className = 'app-item board-item' + (cls ? ' ' + cls : '');
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'launch-btn board-card';
  const top = document.createElement('span');
  top.className = 'board-card-top';
  if (iconName) {
    const ic = document.createElement('span');
    ic.className = 'board-card-top-icon';
    ic.innerHTML = icon(iconName);
    top.appendChild(ic);
  }
  // Data (repo/project/session names) rides a text node — never innerHTML.
  top.appendChild(document.createTextNode(topText));
  const title = document.createElement('span');
  title.className = 'board-card-title';
  title.textContent = titleText;
  btn.appendChild(top);
  btn.appendChild(title);
  li.appendChild(btn);
  return { li: li, btn: btn };
}

function renderSessionCard(card) {
  const meta = STATUS_META[card.status] || STATUS_META.unknown;
  const bits = [card.project || '', meta.text, fmtAge(card.age_seconds)].filter(Boolean);
  const shell = cardShell(meta.icon, ' ' + bits.join(' · '), sessionLabel(card), meta.cls);
  // The Board now includes every launcher-owned agent, not only Claude Code
  // (#455). Show the same registry-backed brand identity as the Coding tab so
  // an unknown/degraded status never hides which terminal the card belongs to.
  const known = state.agents.find(function (a) { return a.id === card.agent; });
  const agentId = String(card.agent || 'claude');
  const agentIcon = document.createElement('img');
  agentIcon.className = 'session-agent-icon board-agent-icon';
  agentIcon.src = iconUrl(agentId);
  agentIcon.alt = known ? known.label : agentId;
  agentIcon.title = agentIcon.alt;
  const top = shell.btn.querySelector('.board-card-top');
  top.insertBefore(agentIcon, top.firstChild);
  if (card.session_id) {
    // Tap toggles the drill-down drawer (#301); the ⚡ button inside it is
    // the way into the full terminal now.
    shell.btn.addEventListener('click', function () {
      state.boardExpanded =
        state.boardExpanded === card.session_id ? null : card.session_id;
      renderBoard();
      if (!state.boardExpanded) fetchBoard().catch(function () {});
    });
    if (state.boardExpanded === card.session_id) {
      shell.li.classList.add('expanded');
      shell.li.appendChild(buildDrawer(card));
    }
  } else {
    shell.btn.classList.add('inert');
    shell.btn.disabled = true;
  }
  return shell.li;
}

// ------------------------------------------------------ drill-down drawer

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

  const actions = document.createElement('div');
  actions.className = 'board-drawer-actions';
  // Reply straight into the PTY — only for live launcher-owned sessions;
  // detached consoles and state-only cards have no reachable stdin.
  const canReply = card.alive && card.kind === 'pty';
  if (canReply) {
    const input = document.createElement('textarea');
    input.className = 'board-reply-input';
    input.rows = 2;
    input.placeholder = 'Reply to ' + (card.project || 'session') + '…';
    actions.appendChild(input);
    // Voice-reply (#302): a per-drawer dictation instance — the drawer is
    // rebuilt on every render, so the mic and its state live and die with it.
    if (voiceDictationAvailable()) {
      const mic = document.createElement('button');
      mic.type = 'button';
      mic.className = 'compose-record board-reply-record';
      mic.innerHTML = icon('mic');
      mic.title = 'Dictate (voice → text)';
      mic.setAttribute('aria-pressed', 'false');
      const dictation = createDictation({
        button: mic,
        getTextarea: function () { return input; },
      });
      mic.addEventListener('click', dictation.toggle);
      actions.appendChild(mic);
    }
    const send = document.createElement('button');
    send.type = 'button';
    send.className = 'board-reply-send';
    send.innerHTML = icon('send-horizontal');
    send.title = 'Send into the session';
    send.addEventListener('click', function () {
      sendReply(card, input, send);
    });
    actions.appendChild(send);
  }
  // Rename first, icon-only (#496 item 5 — was after Terminal, labelled).
  // Same launcher-native override as the Coding tab's row rename button,
  // reachable for detached sessions too (no PTY needed). The drawer stays
  // open across a rename (unlike Terminal, which navigates away), so the
  // completion callback patches this card in place rather than calling
  // fetchBoard() — that would no-op under its own drawer-open self-gate
  // (see fetchBoard()).
  if (card.alive) {
    const rename = document.createElement('button');
    rename.type = 'button';
    rename.className = 'board-rename-btn';
    rename.innerHTML = icon('pencil');
    rename.title = 'Rename this session';
    rename.setAttribute('aria-label', 'Rename this session');
    rename.addEventListener('click', function () {
      openSessionRename(card, function (title) {
        card.manual_title = title;
        renderBoard();
      });
    });
    actions.appendChild(rename);
  }
  // Stop (#496 item 5, ordered before Terminal in round 2): kill a live
  // PTY session straight from the Board — the same unified stop path as
  // the Coding tab (#253: the agent's own quit, force-fallback
  // server-side), one tap, no confirm. Detached consoles keep going
  // through the Coding tab's row button.
  if (card.alive && card.kind === 'pty') {
    const stop = document.createElement('button');
    stop.type = 'button';
    stop.className = 'board-stop-btn';
    stop.innerHTML = icon('x');
    stop.title = 'Stop and kill this session';
    stop.setAttribute('aria-label', 'Stop and kill this session');
    stop.addEventListener('click', async function () {
      stop.disabled = true;
      // Close the drawer first — fetchBoard() self-gates while it's open,
      // so a stop with the drawer up would never see the card clear.
      state.boardExpanded = null;
      renderBoard();
      await stopSession({ session_id: card.session_id, name: sessionLabel(card) });
      // The host stops gracefully (quit → force) — give it the same beat
      // sessions.js gives fetchSessions before reconciling the board.
      setTimeout(function () { fetchBoard().catch(function () {}); }, 1500);
    });
    actions.appendChild(stop);
  }
  // Terminal is deliberately the LAST button in the row (#496 round 2).
  if (card.alive && card.kind !== 'remote') {
    const open = document.createElement('button');
    open.type = 'button';
    open.className = 'board-open-terminal';
    open.innerHTML = icon('zap') + ' Terminal';
    open.title = 'Open the full terminal';
    open.addEventListener('click', function () {
      state.boardExpanded = null;
      openTerminal({ session_id: card.session_id, name: sessionLabel(card) });
    });
    actions.appendChild(open);
  }
  if (actions.childElementCount) drawer.appendChild(actions);
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

// Optimistic move for sendReply() (#461): relocate a card from Your turn into
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

async function sendReply(card, input, btn) {
  const text = input.value.trim();
  if (!text) return;
  btn.disabled = true;
  try {
    const tt = await ensureTerminalToken();
    await jsonApi(
      '/api/claude-code/sessions/' + encodeURIComponent(card.session_id) + '/input',
      {
        method: 'POST',
        headers: authHeaders({ terminalToken: tt, contentType: 'application/json' }),
        body: JSON.stringify({ data: text, submit: true }),
      }
    );
    toast('➤ Sent to ' + (card.project || 'session'), 'good');
    input.value = '';
    // Close the drawer and optimistically flip the card to Claude's turn
    // right away. Deliberately no immediate fetchBoard() here (#461): the
    // hook that actually flips the server's status hasn't had time to run
    // yet, so an immediate re-poll almost always still sees the pre-reply
    // needs-you state and would revert this straight back — worse than the
    // original lag. The regular 5 s poll (already running) reconciles with
    // ground truth as always.
    state.boardExpanded = null;
    moveCardToClaudeTurn(card.session_id);
    renderBoard();
  } catch (exc) {
    apiFailToast('Reply failed', exc);
  } finally {
    btn.disabled = false;
  }
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
      model: (els.boardDispatchModel && els.boardDispatchModel.value) || 'sonnet',
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
      (mode === 'yolo' ? '⚡ /issue-yolo ' : '▶ /issue-start ') + '#' +
        card.number + ' in ' + (body.repo || card.repo),
      'good'
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
// rows (GitHub-issue-list style) — with repo/# on one line and the title on
// the line below, each independently truncated, and icon-only ▶/⚡ actions
// vertically centered against the whole row. Doesn't use cardShell() (that's
// the bordered-box layout the other card kinds keep); the <li> itself is the
// flex row so the text stack and the action icons sit side by side without
// nesting a <button> inside a <button>.
function renderIssueCard(card) {
  const li = document.createElement('li');
  li.className = 'app-item board-item board-item-issue';
  const isInProgress = card.in_progress === true;
  if (isInProgress) li.classList.add('is-in-progress');

  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'launch-btn board-card board-card-flat';
  const textCol = document.createElement('span');
  textCol.className = 'board-card-text';
  const meta = document.createElement('span');
  meta.className = 'board-card-meta-inline';
  meta.textContent = [card.repo, '#' + card.number].filter(Boolean).join(' ');
  if (isInProgress) meta.textContent += ' · in progress';
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
  const title = document.createElement('span');
  title.className = 'board-card-title-compact';
  title.textContent = card.title || '';
  textCol.appendChild(meta);
  textCol.appendChild(title);
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
        ? 'Issue #' + card.number + ' is already in progress'
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

function renderJobCard(card) {
  const iconName = card.state === 'stuck' ? 'triangle-alert' : 'x';
  const top = ' job · ' + card.state + (card.age_seconds != null ? ' · ' + fmtAge(card.age_seconds) : '');
  const shell = cardShell(iconName, top, card.job_name || card.job_id || 'job', 'is-' + card.state);
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

function renderCard(colKey, card) {
  if (card.kind === 'issue' && colKey === 'backlog') return renderIssueCard(card);
  if (colKey === 'done') return renderDoneCard(card);
  if (card.kind === 'pr') return renderPrCard(card);
  if (card.kind === 'job') return renderJobCard(card);
  return renderSessionCard(card);
}

// ---------------------------------------------------------------- render

function renderStatusLine(body) {
  const parts = [];
  if (body.github && body.github.error) {
    parts.push('⚠️ GitHub: ' + body.github.error);
  } else if (body.github && !body.github.fetched_at) {
    parts.push('GitHub not fetched yet — tap ↻');
  }
  if (body.sessions_state && !body.sessions_state.available) {
    parts.push('session state unavailable (hooks not writing yet)');
  } else if (body.sessions_state && body.sessions_state.stale) {
    parts.push('⚠️ session state stale');
  }
  els.boardStatus.textContent = parts.join(' · ');
  els.boardStatus.hidden = parts.length === 0;
}

// Claude 5h/7d usage badges (issue #326) — a separate element from
// boardStatus on purpose: that one is transient-problem text that vanishes
// once the problem clears, while these are live content that should persist
// (dimmed, not hidden) even when the cache is stale. Sourced from
// fleet-config's statusline cache (fleet-config#259); hidden entirely until
// that writer exists or the cache goes missing/corrupt (rate_limits.available
// false) — the same degrade-to-nothing contract sessions_state already uses.
// Rendering itself is shared with the Coding tab's own usage badges — see
// dom-utils.js::renderUsageBadgeRow.

export function renderBoard() {
  const body = state.board;
  if (!body || !els.boardColumns) return;
  const columns = body.columns || {};
  const repoFilter = boardRepoFilter();

  COLUMNS.forEach(function (col) {
    const cards = (columns[col.key] || []).filter(function (card) {
      return matchesRepoFilter(card, repoFilter);
    });
    const btn = els[col.btn];
    if (btn) {
      const count = btn.querySelector('.board-count');
      if (count) count.textContent = String(cards.length);
      btn.classList.toggle('attention', col.key === 'your_turn' && cards.length > 0);
    }
    const list = els.boardColumns.querySelector('.board-list[data-col="' + col.key + '"]');
    const empty = els.boardColumns.querySelector('.board-empty[data-col="' + col.key + '"]');
    if (!list) return;
    list.replaceChildren();
    cards.forEach(function (card) {
      list.appendChild(renderCard(col.key, card));
    });
    if (empty) {
      empty.textContent = col.empty;
      empty.hidden = cards.length > 0;
    }
  });

  renderStatusLine(body);
  renderUsageBadgeRow(els.boardUsage, els.boardUsageSession, els.boardUsageWeekly, body.rate_limits);
  // Keep the dispatch bar's repo list + mic visibility in step with state
  // that may land after the first render (/api/apps, /api/status).
  syncDispatchBar();
  syncStripActive();
}

// ----------------------------------------------------------------- fetch

export async function fetchBoard() {
  // Self-gate: costs nothing while another tab is up (pattern: fetchJobs),
  // and pauses while a drawer is open so the re-render can't wipe a reply
  // being typed (pattern: the terminal pausing the session poll).
  if (state.tab !== 'board' || state.boardExpanded) return;
  const body = await jsonApi('/api/board');
  state.board = body;
  renderBoard();
}

// ?board=<sid> deep-link (#301): land on the Board with that card's drawer
// open, carousel on the card's column. Called from main.js at boot. `sid` may
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
    toast('Board refresh failed — tap ↻ to retry.', 'error');
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
    // (stopped between the ping and the tap). Leave the board browsable; an
    // expanded id with no card would pause the poll forever.
    toast('Session not on the board any more.', 'error');
    return;
  }
  // Expand by the card's real session_id — every other read of boardExpanded
  // (the card-click toggle, the drawer-open check) compares against
  // card.session_id, so expanding by a state_sid would never match.
  state.boardExpanded = matchedCard.session_id;
  renderBoard();
  requestAnimationFrame(function () { showColumn(colKey, false); });
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
    els.boardRefresh.textContent = '↻';
  }
}

// ------------------------------------------------------- column carousel

function columnEl(key) {
  return els.boardColumns.querySelector('.board-col[data-col="' + key + '"]');
}

function showColumn(key, smooth) {
  state.boardCol = key;
  const wrap = els.boardColumns;
  const col = columnEl(key);
  if (wrap && col) {
    // Scroll only the carousel container. scrollIntoView also scrolls the
    // *page* vertically when the column overflows the viewport, yanking
    // the whole tab upward on every strip tap (phone-verify bug, #300).
    const left = col.getBoundingClientRect().left
      - wrap.getBoundingClientRect().left + wrap.scrollLeft;
    wrap.scrollTo({ left: left, behavior: smooth === false ? 'auto' : 'smooth' });
  }
  syncStripActive();
}

function nearestColumnKey() {
  const wrap = els.boardColumns;
  const cols = wrap.querySelectorAll('.board-col');
  if (!cols.length) return state.boardCol;
  const index = Math.min(
    cols.length - 1,
    Math.max(0, Math.round(wrap.scrollLeft / Math.max(1, cols[0].offsetWidth)))
  );
  return cols[index].dataset.col;
}

function syncStripActive() {
  COLUMNS.forEach(function (col) {
    const btn = els[col.btn];
    if (!btn) return;
    const active = col.key === state.boardCol;
    btn.classList.toggle('active', active);
    btn.setAttribute('aria-selected', active ? 'true' : 'false');
  });
}

// -------------------------------------------------------- dispatch (#302)

let dispatchMode = 'add';

// Repo/project dropdown (#337) ← the same live claude-code listing the
// Coding tab renders (state.apps). It is a plain tap-to-open/tap-to-select
// dropdown, not a typable field — a button trigger, not an <input>. It does
// double duty: the real dispatch target lives in the hidden
// #boardDispatchRepo input dispatchGoal() reads, AND the current selection
// (or "All projects", the default) filters which cards renderBoard() shows
// in every column via boardRepoFilter()/cardRepoOf() below.
// Re-synced on tab activation and on every board render (so a boot /api/apps
// fetch that lands late still populates it), but the underlying name list is
// only rebuilt when it actually changed — a rebuild mid-browse would
// otherwise reset a dropdown the user has open.
let _repoSig = null;
let _repoNames = [];

const ALL_PROJECTS_LABEL = 'All projects';

function repoListOpen() {
  const list = els.boardDispatchRepoList;
  return !!list && !list.hidden;
}

function repoDisplayLabel(name) {
  return name || ALL_PROJECTS_LABEL;
}

function renderRepoList() {
  const list = els.boardDispatchRepoList;
  const hidden = els.boardDispatchRepo;
  if (!list) return;
  list.replaceChildren();
  const allLi = document.createElement('li');
  allLi.textContent = ALL_PROJECTS_LABEL;
  allLi.dataset.repo = '';
  allLi.setAttribute('role', 'option');
  allLi.setAttribute('aria-selected', hidden.value === '' ? 'true' : 'false');
  list.appendChild(allLi);
  _repoNames.forEach(function (name) {
    const li = document.createElement('li');
    li.textContent = name;
    li.dataset.repo = name;
    li.setAttribute('role', 'option');
    li.setAttribute('aria-selected', name === hidden.value ? 'true' : 'false');
    list.appendChild(li);
  });
}

function openRepoList() {
  const list = els.boardDispatchRepoList;
  const btn = els.boardDispatchRepoBtn;
  if (!list || !btn) return;
  renderRepoList();
  list.hidden = false;
  btn.setAttribute('aria-expanded', 'true');
}

function closeRepoList() {
  const list = els.boardDispatchRepoList;
  const btn = els.boardDispatchRepoBtn;
  if (!list || !btn) return;
  list.hidden = true;
  btn.setAttribute('aria-expanded', 'false');
}

function selectRepo(name) {
  els.boardDispatchRepo.value = name;
  els.boardDispatchRepoBtn.textContent = repoDisplayLabel(name);
  closeRepoList();
  // The same selection scopes the visible kanban cards (#337) — apply it
  // immediately rather than waiting for the next 5 s poll.
  renderBoard();
}

function syncDispatchRepos() {
  const hidden = els.boardDispatchRepo;
  const btn = els.boardDispatchRepoBtn;
  if (!hidden || !btn) return;
  const repos = (state.apps || [])
    .filter(function (a) { return a.kind === 'claude-code'; })
    .map(function (a) { return String(a.name); });
  const sig = repos.join('\n');
  if (sig === _repoSig) return;
  _repoSig = sig;
  _repoNames = repos;
  // '' ("All projects") is always a valid selection — only reset a specific
  // repo pick back to "All" if that repo dropped out of the live list.
  const current = hidden.value;
  const next = (!current || repos.indexOf(current) >= 0) ? current : '';
  hidden.value = next;
  btn.textContent = repoDisplayLabel(next);
  if (repoListOpen()) renderRepoList();
}

// The repo/project identity of a card, whatever kind it is — issue/PR/done
// cards carry `repo`, live session cards carry `project`. Job cards carry
// neither (they aren't tied to a coding project) and are hidden by a
// specific-project filter, same as any other non-matching card.
function cardRepoOf(card) {
  return card.repo || card.project || null;
}

function boardRepoFilter() {
  return els.boardDispatchRepo ? els.boardDispatchRepo.value : '';
}

function matchesRepoFilter(card, filter) {
  if (!filter) return true;
  const repo = cardRepoOf(card);
  return !!repo && String(repo).toLowerCase() === String(filter).toLowerCase();
}

function setDispatchMode(mode) {
  dispatchMode = mode;
  els.boardDispatchModes.querySelectorAll('.board-mode-btn')
    .forEach(function (btn) {
      const active = btn.dataset.mode === mode;
      btn.classList.toggle('active', active);
      btn.setAttribute('aria-checked', active ? 'true' : 'false');
    });
}

async function dispatchGoal() {
  const goal = els.boardDispatchGoal.value.trim();
  if (!goal) {
    toast('Type or dictate a goal first', 'error');
    return;
  }
  const repo = els.boardDispatchRepo.value;
  if (!repo) {
    toast('No repo to dispatch to', 'error');
    return;
  }
  const btn = els.boardDispatchSend;
  btn.disabled = true;
  // The server waits for the agent's first output before typing the goal
  // in (spawn-then-type), so this call legitimately takes seconds — tick.
  const stopTimer = startWorkTimer(btn, icon('send-horizontal'));
  try {
    const tt = await ensureTerminalToken();
    const payload = {
      repo: repo,
      goal: goal,
      mode: dispatchMode,
      model: (els.boardDispatchModel && els.boardDispatchModel.value) || 'sonnet',
    };
    // Same size contract as startIssue (issue #374).
    applyLaunchSizePayload(payload);
    const body = await jsonApi('/api/board/dispatch', {
      method: 'POST',
      headers: authHeaders({ terminalToken: tt, contentType: 'application/json' }),
      body: JSON.stringify(payload),
    });
    toast('🚀 ' + (body.launched || dispatchMode) + ' → ' + (body.repo || repo), 'good');
    // The goal stays in the bar for rapid multi-dispatch ("create more");
    // ✕ clears it. The new card lands in Claude's turn on the next poll.
    fetchBoard().catch(function () {});
  } catch (exc) {
    apiFailToast('Dispatch failed', exc);
  } finally {
    stopTimer();
    btn.disabled = false;
  }
}

function syncDispatchBar() {
  syncDispatchRepos();
  if (els.boardDispatchRecord) {
    els.boardDispatchRecord.hidden = !voiceDictationAvailable();
  }
}

function wireRepoCombo() {
  const btn = els.boardDispatchRepoBtn;
  const list = els.boardDispatchRepoList;
  const combo = btn && btn.closest('.board-repo-combo');
  if (!btn || !list || !combo) return;
  btn.addEventListener('click', function () {
    if (repoListOpen()) closeRepoList(); else openRepoList();
  });
  btn.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') closeRepoList();
  });
  list.addEventListener('click', function (e) {
    const li = e.target.closest('li[data-repo]');
    if (!li) return;
    selectRepo(li.dataset.repo);
  });
  // Tapping anywhere outside the trigger/list closes it — there's no text
  // focus to lose (it's a button, not an input), so a plain outside-click
  // check is enough; no blur-race handling needed.
  document.addEventListener('click', function (e) {
    if (repoListOpen() && !combo.contains(e.target)) closeRepoList();
  });
}

function wireDispatch() {
  if (!els.boardDispatchSend) return;
  wireRepoCombo();
  els.boardDispatchModes.querySelectorAll('.board-mode-btn')
    .forEach(function (btn) {
      btn.addEventListener('click', function () {
        setDispatchMode(btn.dataset.mode);
      });
    });
  // The model <select> (#500) is a plain client-side control (issue #355
  // pattern) — no server config, just read at dispatch time above.
  els.boardDispatchSend.addEventListener('click', function () {
    dispatchGoal();
  });
  els.boardDispatchClear.addEventListener('click', function () {
    els.boardDispatchGoal.value = '';
    els.boardDispatchGoal.focus();
  });
  const dictation = createDictation({
    button: els.boardDispatchRecord,
    getTextarea: function () { return els.boardDispatchGoal; },
  });
  els.boardDispatchRecord.addEventListener('click', dictation.toggle);
  syncDispatchBar();
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
    // The pane was hidden until this click — position the carousel on the
    // remembered column now that it has layout (no animation on arrival).
    requestAnimationFrame(function () { showColumn(state.boardCol, false); });
  });
  wireDispatch();
  els.boardRefresh.addEventListener('click', function () {
    refreshGithub().catch(function (exc) {
      apiFailToast('GitHub refresh failed', exc);
    });
  });
  COLUMNS.forEach(function (col) {
    const btn = els[col.btn];
    if (btn) btn.addEventListener('click', function () { showColumn(col.key); });
  });
  let scrollTimer = null;
  els.boardColumns.addEventListener('scroll', function () {
    if (scrollTimer) clearTimeout(scrollTimer);
    scrollTimer = setTimeout(function () {
      state.boardCol = nearestColumnKey();
      syncStripActive();
    }, 80);
  }, { passive: true });
  // Land on Your turn — the only number that matters when the tab opens.
  syncStripActive();
}
