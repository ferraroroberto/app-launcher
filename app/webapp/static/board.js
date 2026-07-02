/* Board tab (issue #300 / #164): read-only fleet kanban.
 *
 * Four computed columns from GET /api/board — Backlog (open issues),
 * Claude's turn (sessions working/unknown/idle), Your turn (needs-you
 * sessions + open PRs + failed/stuck jobs), Done (merged/closed today).
 * Phone-first: the columns container is a scroll-snap carousel (one column
 * per swipe) and the strip above it doubles as column switcher + counts.
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
 * GitHub, job cards jump to the Jobs tab. Free-text dispatch is #302.
 */

import { els, state } from './state.js';
import { isDesktopClient, jsonApi, toast } from './api.js';
import { setTab } from './tabs.js';
import { openTerminal } from './terminal.js';
import { ensureTerminalToken } from './webauthn.js';

const COLUMNS = [
  { key: 'backlog', btn: 'boardColBacklog', empty: 'No open issues cached — tap ↻ to fetch from GitHub.' },
  { key: 'claude_turn', btn: 'boardColClaude', empty: 'No sessions on Claude’s side.' },
  { key: 'your_turn', btn: 'boardColYours', empty: 'Nothing needs you right now. 🎉' },
  { key: 'done', btn: 'boardColDone', empty: 'Nothing merged or closed today yet.' },
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

function sessionLabel(card) {
  return card.live_title || card.prompt_title || card.name || card.project || 'session';
}

const STATUS_META = {
  working: { icon: '⚡', text: 'working', cls: 'is-working' },
  'needs-you': { icon: '✳️', text: 'needs you', cls: 'is-needs-you' },
  idle: { icon: '💤', text: 'idle', cls: 'is-idle' },
  unknown: { icon: '·', text: '', cls: 'is-unknown' },
};

// ----------------------------------------------------------------- cards

function cardShell(topText, titleText, cls) {
  const li = document.createElement('li');
  li.className = 'app-item board-item' + (cls ? ' ' + cls : '');
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'launch-btn board-card';
  const top = document.createElement('span');
  top.className = 'board-card-top';
  top.textContent = topText;
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
  const shell = cardShell(meta.icon + ' ' + bits.join(' · '), sessionLabel(card), meta.cls);
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

function terminalHeaders(tt, json) {
  const h = json ? { 'Content-Type': 'application/json' } : {};
  if (tt) h['x-terminal-token'] = tt;
  return h;
}

function buildDrawer(card) {
  const drawer = document.createElement('div');
  drawer.className = 'board-drawer';

  const exchange = document.createElement('div');
  exchange.className = 'board-exchange';
  exchange.textContent = 'Loading last exchange…';
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
    const send = document.createElement('button');
    send.type = 'button';
    send.className = 'icon-btn board-reply-send';
    send.textContent = '➤';
    send.title = 'Send into the session';
    send.addEventListener('click', function () {
      sendReply(card, input, send);
    });
    actions.appendChild(input);
    actions.appendChild(send);
  }
  if (card.alive && card.kind !== 'remote') {
    const open = document.createElement('button');
    open.type = 'button';
    open.className = 'icon-btn board-open-terminal';
    open.textContent = '⚡';
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
      { headers: terminalHeaders(tt, false) }
    );
    el.replaceChildren();
    if (!body.available) {
      el.textContent = 'No exchange yet — the transcript isn’t linked to this session.';
      return;
    }
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
    el.textContent = '⚠️ ' + (exc.message || exc);
  }
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
        headers: terminalHeaders(tt, true),
        body: JSON.stringify({ data: text, submit: true }),
      }
    );
    toast('➤ Sent to ' + (card.project || 'session'), 'good');
    input.value = '';
    // Close the drawer and resume the poll — the prompt-submit hook plus
    // the transcript overlay flip the card to working within a cycle.
    state.boardExpanded = null;
    renderBoard();
    fetchBoard().catch(function () {});
  } catch (exc) {
    toast('Reply failed: ' + (exc.message || exc), 'error');
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

async function startIssue(card, mode, btn) {
  btn.disabled = true;
  try {
    const tt = await ensureTerminalToken();
    const payload = { repo: card.repo, number: card.number, mode: mode };
    // Desktop browsers get the PC mirror window, like every launch (#241).
    if (isDesktopClient()) payload.desktop = true;
    const body = await jsonApi('/api/board/issues/start', {
      method: 'POST',
      headers: terminalHeaders(tt, true),
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
    toast('Issue start failed: ' + (exc.message || exc), 'error');
  } finally {
    btn.disabled = false;
  }
}

function renderIssueCard(card) {
  const top = [card.repo, '#' + card.number].filter(Boolean).join(' ');
  const shell = cardShell(top, card.title || '', '');
  if (card.url) {
    shell.btn.addEventListener('click', function () {
      window.open(card.url, '_blank', 'noopener');
    });
  }
  // One-tap start (#301) — only for repos the Coding tab could launch in.
  // Buttons live on the <li>, outside the card <button> (no nesting).
  if (card.number && repoInProjects(card.repo)) {
    const row = document.createElement('div');
    row.className = 'board-issue-actions';
    [['start', '▶ Start'], ['yolo', '⚡ YOLO']].forEach(function (pair) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'icon-btn board-issue-btn';
      btn.textContent = pair[1];
      btn.title = '/issue-' + pair[0] + ' ' + card.number + ' in ' + card.repo;
      btn.addEventListener('click', function () {
        startIssue(card, pair[0], btn);
      });
      row.appendChild(btn);
    });
    shell.li.appendChild(row);
  }
  return shell.li;
}

function renderPrCard(card) {
  const draft = card.is_draft ? ' · draft' : '';
  const shell = cardShell('🔀 ' + [card.repo, 'PR #' + card.number].join(' ') + draft,
    card.title || '', '');
  if (card.url) {
    shell.btn.addEventListener('click', function () {
      window.open(card.url, '_blank', 'noopener');
    });
  }
  return shell.li;
}

function renderJobCard(card) {
  const icon = card.state === 'stuck' ? '⚠️' : '❌';
  const top = icon + ' job · ' + card.state + (card.age_seconds != null ? ' · ' + fmtAge(card.age_seconds) : '');
  const shell = cardShell(top, card.job_name || card.job_id || 'job', 'is-' + card.state);
  shell.btn.addEventListener('click', function () { setTab('jobs'); });
  return shell.li;
}

function renderDoneCard(card) {
  const icon = card.state === 'merged' ? '✅' : '☑️';
  const noun = card.kind === 'pr' ? 'PR #' : '#';
  const shell = cardShell(icon + ' ' + [card.repo, noun + card.number].join(' ') + ' · ' + card.state,
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

export function renderBoard() {
  const body = state.board;
  if (!body || !els.boardColumns) return;
  const columns = body.columns || {};

  COLUMNS.forEach(function (col) {
    const cards = columns[col.key] || [];
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
// open, carousel on the card's column. Called from main.js at boot.
export async function openBoardCard(sid) {
  setTab('board');
  try {
    await fetchBoard();
  } catch (_) { /* render whatever we have */ }
  const columns = (state.board && state.board.columns) || {};
  const colKey = Object.keys(columns).find(function (key) {
    return (columns[key] || []).some(function (c) { return c.session_id === sid; });
  });
  if (!colKey) {
    // Session already gone (stopped between the ping and the tap) — leave
    // the board browsable; an expanded id with no card would pause the
    // poll forever.
    toast('Session not on the board any more.', 'error');
    return;
  }
  state.boardExpanded = sid;
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

// ------------------------------------------------------------------ wire

export function wireBoard() {
  if (!els.tabBoard) return;
  els.tabBoard.addEventListener('click', function () {
    fetchBoard().then(function () {
      // Opening the tab with a stale (or never-filled) gh cache refreshes
      // it once; while the tab just sits open only the free poll runs.
      if (ghStale(state.board)) refreshGithub().catch(function () {});
    }).catch(function () {});
    // The pane was hidden until this click — position the carousel on the
    // remembered column now that it has layout (no animation on arrival).
    requestAnimationFrame(function () { showColumn(state.boardCol, false); });
  });
  els.boardRefresh.addEventListener('click', function () {
    refreshGithub().catch(function (exc) {
      toast('GitHub refresh failed: ' + (exc.message || exc), 'error');
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
