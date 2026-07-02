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
 * refreshed via the ↻ button or once on first activation when it has never
 * been filled — never on the 5 s poll.
 *
 * Cards are a view, not a control surface (v1): a live session card opens
 * the existing terminal overlay, issue/PR/done cards open GitHub, job cards
 * jump to the Jobs tab. Drill-down, reply and dispatch are #301 / #302.
 */

import { els, state } from './state.js';
import { jsonApi, toast } from './api.js';
import { setTab } from './tabs.js';
import { openTerminal } from './terminal.js';

const COLUMNS = [
  { key: 'backlog', btn: 'boardColBacklog', empty: 'No open issues cached — tap ↻ to fetch from GitHub.' },
  { key: 'claude_turn', btn: 'boardColClaude', empty: 'No sessions on Claude’s side.' },
  { key: 'your_turn', btn: 'boardColYours', empty: 'Nothing needs you right now. 🎉' },
  { key: 'done', btn: 'boardColDone', empty: 'Nothing merged or closed today yet.' },
];

let autoRefreshTried = false;
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
  if (card.alive && card.session_id && card.kind !== 'remote') {
    shell.btn.addEventListener('click', function () {
      openTerminal({ session_id: card.session_id, name: sessionLabel(card) });
    });
  } else {
    shell.btn.classList.add('inert');
    shell.btn.disabled = true;
  }
  return shell.li;
}

function renderIssueCard(card) {
  const top = [card.repo, '#' + card.number].filter(Boolean).join(' ');
  const shell = cardShell('🐛 ' + top, card.title || '', '');
  if (card.url) {
    shell.btn.addEventListener('click', function () {
      window.open(card.url, '_blank', 'noopener');
    });
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
  // Self-gate: costs nothing while another tab is up (pattern: fetchJobs).
  if (state.tab !== 'board') return;
  const body = await jsonApi('/api/board');
  state.board = body;
  renderBoard();
  // First-ever activation with an empty gh cache: fill it once so the
  // board isn't blank out of the box. Manual ↻ from then on.
  if (!autoRefreshTried && body.github && !body.github.fetched_at && !body.github.error) {
    autoRefreshTried = true;
    refreshGithub().catch(function () {});
  }
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
  const col = columnEl(key);
  if (col) {
    col.scrollIntoView({
      behavior: smooth === false ? 'auto' : 'smooth',
      block: 'nearest',
      inline: 'start',
    });
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
    fetchBoard().catch(function () {});
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
