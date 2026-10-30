/* Board tab: the fleet kanban.
 *
 * Five computed columns from GET /api/board, each single-purpose, drawn as
 * lanes in this order: Backlog (open issues), the chief's plan over
 * Claude's turn (sessions working/unknown/idle/idle-finished/tool-pending/
 * waiting-on-task),
 * Your turn (stalled/awaiting-decision/awaiting-input sessions only — a split
 * of the old undifferentiated needs-you, sharpened by a
 * tool-pending carve-out), PRs and jobs (open PRs + failed/unconfirmed/stuck
 * jobs), Done today (closed issues today). Each lane
 * is a collapsible section card like every other tab's — stacked on
 * the phone, side by side on the desktop grid — with its count in the
 * summary, or a folded lane's exception ("1 failing").
 *
 * Cost discipline: fetchBoard() self-gates on the Board tab being visible
 * (pattern: fetchJobs / fetchRunningApps); the server's gh cache is only
 * refreshed via the ↻ button, on tab activation when the cache is older
 * than GH_STALE_MS, or on a poll that finds it never fetched (a webapp
 * restart empties it) — never on a poll of an already-fetched cache.
 *
 * Act-from-the-card loop: tapping a live session row opens an
 * inline drawer with the last user↔assistant exchange (passkey-gated — it
 * is transcript text, rendered as markdown), the shared composer,
 * Chat and Terminal, and Rename and Stop in its kebab;
 * backlog rows of repos present in the projects folder carry one ▶ Start
 * verb, with YOLO in the row's kebab; `?board=<sid>` deep-links onto a
 * card with its drawer open. The poll keeps running while a drawer is open
 * (the chief chat holds one open for hours), and renderBoard() keeps
 * the open drawer's own node across the re-render, so it can never wipe a
 * reply being typed. Issue/PR/done rows open GitHub, job rows (PRs and
 * jobs) jump to the Jobs tab.
 *
 * Split off a single-file module (`/codebase-audit`), the way
 * `jobs.js` and `terminal.js` already were: the lane toolbar's project
 * filter, the Backlog's "Start with" model combo and the fleet chief's
 * lifecycle live in `board-dispatch.js`; the drill-down drawer in
 * `board/drawer.js`; the Chief's plan card in `board/chief-plan.js`. This
 * module keeps card rendering, one-tap issue-start, the lanes and the poll,
 * and calls into those for the rest. The Board has no chief card:
 * the chief lives in its row under Code › Sessions, and here only as its
 * session row in Claude's turn.
 */

import { els, state } from './state.js';
import { apiFailToast, escapeHtml, jsonApi, opensInPcWindow, toast } from './api.js';
import { setTab } from './tabs.js';
import { sessionTitle } from './sessions.js';
import { applyLaunchSizePayload, openTerminal } from './terminal.js';
import { emptyStateEl } from './_vendored/empty-state/empty-state.js';
import { icon } from './_vendored/icons/icons.js';
import { terminalJsonApi } from './webauthn.js';
import { isOffBoard } from './channel-sessions.js';
import {
  brandIconEl, channelSessionName, fmtCardStamp, fmtDuration, isChannelSession, isRelaySession,
} from './dom-utils.js';
import { avatar, chip, sessionStatusBits, sessionStatusMeta } from './glance.js';
import { ACTION_ROW_MENU, actionRow } from './action-rows.js';
import { failedFetchState, setAsyncState } from './async-state.js';
import { renderBoardBadge, renderFittedHeadStatus } from './home-head.js';
import { createRowMenu } from './_vendored/row-menu/row-menu.js';
import {
  boardRepoFilter, getBoardDispatchModel, isChiefCard, matchesRepoFilter, syncDispatchBar,
  wireDispatch,
} from './board/board-dispatch.js';
import { wireChiefAnswers } from './board/board-answers.js';
import { openQuestionCount } from './board/chief-keys.js';
import {
  buildDrawer, drawerId, drawerShape, endDrawerRender, openDrawerItem, syncDrawerLifecycle,
} from './board/drawer.js';
import { agoText, renderChiefPlan } from './board/chief-plan.js';
import { doneEarlierLoaded, renderDoneEarlier } from './board/done-earlier.js';

// `gh` marks where a column's cards come from: 'all' columns are
// GitHub-only, so before the cache is loaded their count is unknown, never
// zero; 'part' (PRs and jobs) mixes open PRs with job cards, whose count
// stays real. `live` columns are built from the session-host list, so an
// unreachable session-host makes them unknown too. Lane order:
// Backlog, (the chief's plan,) Claude's turn, Your turn, PRs and jobs, Done
// today. Each empty-state glyph is the lane's own icon.
const COLUMNS = [
  // Each empty lane says why, and its empty state carries the one control
  // that fills it (as a button): Refresh for the
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

// Bumped when a rename the server has confirmed is patched into the Board
// A Board read issued before that carries the old title, and its
// reply can land after the patch (the poll also awaits the chief's plan), so
// fetchBoard() drops a payload whose read predates the last bump; the next
// poll brings the server's view.
let boardEditSeq = 0;

// A Board edit the server confirmed was patched in client-side.
export function noteBoardEdit() {
  boardEditSeq += 1;
}

// --------------------------------------------------------------- helpers

// The exact same title resolution as the Coding tab's Running-sessions list
// — board cards carry the same field names (shared_name/
// shared_name_source, live_title, prompt_title, name, project) that
// sessionTitle() reads, so a single shared function keeps both tabs
// agreeing on one session's title instead of two independently-drifting
// codepaths.
export function sessionLabel(card) {
  return sessionTitle(card) || card.project || 'session';
}


// ----------------------------------------------------------------- cards

// Every Board row is the shared action-row: one title line
// with an ellipsis on every width (not a
// two-line clamp), the full text in the title's `title` attribute.

// A PR, job or done row on the shared action-row (was its own
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
// the card's fresh header is swapped into it instead of a new drawer.
function renderSessionCard(card, openItem) {
  const chief = isChiefCard(card);
  const meta = sessionStatusMeta(card.status, chief);
  // The shared session row, the same anatomy as the Coding tab's:
  // avatar with the agent's mark and alive badge (crown for the chief), a
  // one-line title, one meta line "repo · status · age". Status is plain
  // meta; only an exception becomes a chip: stalled (danger), detached
  // (neutral), a Telegram channel's name. No left-edge status
  // border, no dimming of an idle card. The Board includes every
  // launcher-owned agent, resolved against the same registry.
  const stalled = card.status === 'stalled';
  const chips = [];
  if (isChannelSession(card)) chips.push(chip(channelSessionName(card), 'neutral', 'session-channel-tag'));
  if (isRelaySession(card)) chips.push(chip(card.label, 'neutral', 'session-relay-tag'));
  if (stalled) chips.push(chip('stalled', 'danger', 'board-stalled-chip'));
  if (card.kind === 'remote') chips.push(chip('detached', 'neutral', 'session-detached'));
  const bits = [card.project || ''].concat(
    sessionStatusBits(card.status, card.age_seconds, { chief: chief, hideStatus: stalled })
  ).filter(Boolean);
  const known = state.agents.find(function (a) { return a.id === card.agent; });
  const agentId = String(card.agent || 'claude');
  const title = sessionLabel(card);

  // On the shared action-row: the row's main button is the drawer's
  // disclosure. Tap toggles the drill-down drawer; Chat and Terminal
  // inside it are the ways into the session.
  const row = actionRow({
    className: 'board-item board-item-session' + (meta.cls ? ' ' + meta.cls : ''),
    avatar: avatar(
      brandIconEl(agentId, 'session-agent-icon board-agent-icon', known ? known.label : agentId),
      chief ? 'crown' : (card.alive == null ? 'none' : (card.alive ? 'alive' : 'down')),
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
  // The chief is marked by its crown avatar badge, not a tint.
  if (chief) li.classList.add('board-item-chief');
  if (isChannelSession(card)) li.classList.add('board-item-channel');
  if (!card.session_id) return li;
  li.dataset.sessionId = card.session_id;
  // The main button is a disclosure for the drawer (WAI-ARIA
  // disclosure pattern): every render rebuilds it, so its state is set here,
  // and a kept drawer's fresh button below carries it too.
  const expanded = state.boardExpanded === card.session_id;
  row.main.setAttribute('aria-expanded', expanded ? 'true' : 'false');
  if (!expanded) return li;
  row.main.setAttribute('aria-controls', drawerId(card));
  if (openItem) {
    // A kept drawer: only the row's main button is replaced; the
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
    // after it — display data, never reaches the command line.
    const payload = {
      repo: card.repo, number: card.number, mode: mode,
      title: card.title || '',
      // The bar's Start-model selector governs one-tap starts,
      // overriding the shared Coding model per launch.
      model: getBoardDispatchModel(),
    };
    // Desktop browsers get the PC mirror window, like every launch.
    // Phone launches carry the real terminal size so the PTY's early
    // output is authored at the width the overlay will fit() to; the route already accepts rows/cols.
    applyLaunchSizePayload(payload);
    const body = await terminalJsonApi('/api/board/issues/start', { method: 'POST', body: payload });
    toast(
      (mode === 'yolo' ? '/issue-yolo ' : '/issue-start ') + '#' +
        card.number + ' in ' + (body.repo || card.repo),
      'good',
      { icon: mode === 'yolo' ? 'zap' : 'play' }
    );
    if (body.session && body.session.kind !== 'remote' && !opensInPcWindow()) {
      openTerminal(body.session);
    }
  } catch (exc) {
    apiFailToast('Issue start failed', exc);
  } finally {
    btn.disabled = false;
  }
}

// The Backlog rows' kebab menus, one shared row-menu: the 5 s poll
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

// A Backlog row (on the shared action-row): the one-line
// title, the meta "repo #N" with an accent "in progress" chip while a lane
// holds the issue, then one trailing plain verb, ▶ Start, and the
// kebab holding YOLO (start and ship), Open on GitHub and Copy link
// (YOLO takes two taps). Git state is no longer
// drawn here; it lives on Code › Projects. A tap on the row opens the issue
// on GitHub.
function stampEl(text) {
  const el = document.createElement('span');
  el.className = 'board-card-stamp';
  el.textContent = '· ' + text;
  return el;
}

function renderIssueCard(card) {
  // Claim owner liveness: 'dead' is a provably gone lane — not in
  // progress, startable again; 'unknown' keeps the in-progress lock but says
  // the owner could not be verified.
  const isInProgress = card.in_progress === true;
  const claimUnverified = isInProgress && card.claim_state === 'unknown';
  const claimStale = card.claim_state === 'dead';
  const chips = [];
  if (claimUnverified) chips.push(chip('in progress (unverified)', 'accent', 'board-claim-chip'));
  else if (isInProgress) chips.push(chip('in progress', 'accent', 'board-claim-chip'));
  else if (claimStale) chips.push(chip('stale claim', 'neutral', 'board-claim-chip'));

  // The last-updated date: the tail of the meta text, or, beside a
  // claim chip (where the text ellipsizes), its own item that never shrinks.
  const stamp = fmtCardStamp(card.updated_at);

  // One-tap start — only for repos the Coding tab could launch in.
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
// stuck, `unconfirmed` (the run may well have delivered, but the
// scheduled-run adapter could not establish that) and `unreadable` (the
// run history itself could not be read) want a look, so attention — never the
// danger that says a run is known to have failed.
const JOB_CHIPS = {
  failed: ['failed', 'danger'],
  stuck: ['stuck', 'attention'],
  unconfirmed: ['not confirmed', 'attention'],
  unreadable: ['unreadable', 'attention'],
};

// A job in PRs and jobs that counts as failing: the header's
// "N failing" and the folded lane's meta.
function jobFailing(card) {
  return card.kind === 'job' && (card.state === 'failed' || card.state === 'stuck');
}

// A failed or unconfirmed run is a past event, so it can be dismissed once
// dealt with. Stuck and unreadable describe a live problem: no verb.
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
    noteBoardEdit();
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

// `stampRef` is the "now" the stamp is read against: the earlier days pass the
// row's own close time, so under a day header the stamp is the clock time.
export function renderDoneCard(card, stampRef) {
  // Done holds closed issues only — a merged PR that closed one is
  // already reflected here by the issue itself, so there's no PR/pairing
  // branch to render.
  return laneRow('board-item-done', card.title || '',
    [[card.repo, '#' + card.number].join(' '), card.state,
      fmtCardStamp(card.closed_at || card.updated_at, stampRef)].filter(Boolean).join(' · '), [],
    'Open issue #' + card.number + ' on GitHub', openUrl(card.url)).li;
}

function renderCard(colKey, card, openItem) {
  if (card.kind === 'issue' && colKey === 'backlog') return renderIssueCard(card);
  if (colKey === 'done') return renderDoneCard(card);
  if (card.kind === 'pr') return renderPrCard(card);
  if (card.kind === 'job') return renderJobCard(card);
  return renderSessionCard(card, openItem);
}

// Set a list's children to `nodes` without ever detaching a node that is
// already in place — moving a focused <textarea> blurs it, which on a phone
// drops the keyboard mid-reply. Only the kept drawer <li> can already
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
// genuine "fetched, nothing open".
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
// right now." is reachable only from a session list actually read.
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

// The lane's one next action, reusing the launcher's own flows:
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

// One attention banner above the lanes, shown only while a source
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

// A lane's exception: in its tone, in place of the count, while the
// lane is folded (the CSS swaps them); '' when there is none.
export function setLaneException(section, text, tone) {
  const el = section && section.querySelector('.board-lane-exception');
  if (!el) return;
  el.textContent = text;
  el.dataset.tone = tone;
  el.hidden = !text;
}

// The Backlog's footer: how fresh the GitHub cache is, only once it
// has been fetched (before that the empty state says so).
function renderBacklogFoot(body, ghLoaded) {
  const foot = els.boardColumns.querySelector('.board-lane-foot[data-col="backlog"]');
  if (!foot) return;
  const ago = ghLoaded ? agoText(body.github.fetched_at) : '';
  foot.hidden = !ago;
  foot.innerHTML = ago ? icon('globe') + '<span>GitHub · updated ' + ago + '</span>' : '';
}

export function plural(n, one, many) {
  return n + ' ' + (n === 1 ? one : many);
}

// The page header (the header rule): only the exceptions — "N
// questions" (the chief's, unanswered), "N needs you" (Your turn) and
// "N failing" (failed or stuck jobs), two at most — fleet-wide, whatever the
// project filter narrows the lanes to. With nothing wrong, the plain totals;
// a count that could not be read is said, never shown as zero:
// an unreadable plan adds no question part.
function renderBoardHead(body, ghLoaded, liveRead) {
  const columns = body.columns || {};
  const sessions = function (key) {
    return (columns[key] || []).filter(function (c) { return !isOffBoard(c); }).length;
  };
  const questions = openQuestionCount(state.chiefPlan);
  const yours = sessions('your_turn');
  const failing = (columns.other || []).filter(jobFailing).length;
  const exceptions = [];
  if (questions) exceptions.push({ text: plural(questions, 'question', 'questions'), short: questions + ' Q', tone: 'attention' });
  if (yours) exceptions.push({ text: yours + ' needs you', short: yours + ' you', tone: 'attention' });
  if (failing) exceptions.push({ text: failing + ' failing', short: failing + ' fail', tone: 'danger' });
  const live = yours + sessions('claude_turn');
  const totals = [liveRead || live ? plural(live, 'session', 'sessions') : 'sessions unknown'];
  if (ghLoaded) totals.push(plural((columns.backlog || []).length, 'issue', 'issues'));
  renderFittedHeadStatus(els.boardHeadStatus, exceptions, totals.join(' · '));
}


// The desktop grid: one column per lane that holds rows, the plan
// and Claude's turn sharing one as always; an empty lane spans the grid as
// a strip under them (CSS). The pane carries the count, so the project
// filter above stays exactly one lane wide.
function sizeLaneGrid() {
  const cells = Array.from(els.boardColumns.children).filter(function (el) {
    return !el.classList.contains('is-empty');
  }).length;
  els.boardColumns.parentElement.style.setProperty('--board-lanes', String(Math.max(1, cells)));
}

// Each lane's cards as drawn: managed sessions (Telegram, the chief) leave the
// columns, and the project filter narrows the rest.
function visibleCards(columns) {
  const repoFilter = boardRepoFilter();
  const visible = {};
  COLUMNS.forEach(function (col) {
    visible[col.key] = (columns[col.key] || []).filter(function (card) {
      return !isOffBoard(card) && matchesRepoFilter(card, repoFilter);
    });
  });
  return visible;
}

// The drawn card whose drawer is open, else null. An open drawer whose card
// is gone (session ended) or filtered out has nothing to render into;
// collapse it rather than hold an expanded id no card matches.
function expandedCardIn(visible, body) {
  if (!state.boardExpanded) return null;
  let expandedCard = null;
  COLUMNS.forEach(function (col) {
    expandedCard = expandedCard || visible[col.key].find(function (c) {
      return c.session_id === state.boardExpanded;
    }) || null;
  });
  if (body && !expandedCard) state.boardExpanded = null;
  return expandedCard;
}

// A drawer opened from outside its section — the ?board= deep link, a chief
// chat send — unfolds that section once. After that the section's open
// state is the user's again: the poll never re-opens one they folded.
function revealDrawerSection(visible, expandedCard) {
  if (!expandedCard) {
    revealedDrawer = null;
    return;
  }
  if (revealedDrawer === state.boardExpanded) return;
  revealedDrawer = state.boardExpanded;
  const home = COLUMNS.find(function (col) {
    return visible[col.key].indexOf(expandedCard) !== -1;
  });
  const section = home && els[home.section];
  if (section) section.open = true;
}

// One lane: its section's count and flags, its rows, its empty state.
function renderLane(col, cards, openItem, body, ghLoaded, liveRead) {
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
    // At the wide query an empty lane is a one-line strip.
    // Done with earlier days under it is a lane, not a strip.
    section.classList.toggle('is-empty',
      cards.length === 0 && !(col.key === 'done' && doneEarlierLoaded()));
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
  if (!empty) return;
  // The canonical empty-state block: a muted glyph over the one-line
  // reason, instead of a bare sentence in a dashed box. `empty` stays the
  // hidden/shown container the poll toggles, so nothing else about the
  // column changes. Rebuilt only when its words change: the 5 s poll would
  // otherwise replace the action button under a keyboard user's focus.
  const text = emptyText(col, body, ghLoaded, liveRead);
  const action = emptyAction(col, liveRead);
  const sig = text + '|' + action.actionLabel;
  if (empty.dataset.sig !== sig) {
    empty.dataset.sig = sig;
    empty.replaceChildren(emptyStateEl(col.glyph, text, action));
  }
  empty.hidden = cards.length > 0;
}

export function renderBoard() {
  const body = state.board;
  const visible = visibleCards((body && body.columns) || {});
  const expandedCard = expandedCardIn(visible, body);
  // The poll keeps running with a drawer open, so a render that still
  // shows the same drawer keeps its node — reply text, focus, a live mic and
  // the chief exchange poll all live on it.
  const openItem = openDrawerItem(expandedCard);
  const activeEl = openItem && openItem.contains(document.activeElement)
    ? document.activeElement : null;
  syncDrawerLifecycle(openItem);
  if (!body || !els.boardColumns) return;
  const ghLoaded = ghFetched(body);
  const liveRead = liveSessionsRead(body);

  revealDrawerSection(visible, expandedCard);
  COLUMNS.forEach(function (col) {
    renderLane(col, visible[col.key], openItem, body, ghLoaded, liveRead);
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
  endDrawerRender(openItem);
  renderBacklogFoot(body, ghLoaded);
  renderDoneEarlier(ghLoaded);
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
  // No drawer gate: the chief chat keeps a drawer open for hours, and
  // pausing on it froze the whole Board silently. renderBoard() keeps the
  // open drawer's node instead, so a poll can't wipe a reply being typed.
  if (state.tab !== 'board') return;
  if (!state.board) setAsyncState(els.boardFetchState, 'loading', { message: 'Reading the Board…' });
  const editSeq = boardEditSeq;
  // The chief's plan rides the same poll. Its failure is the card's
  // own quiet note, never the Board's error.
  let board, plan;
  try {
    [board, plan] = await Promise.all([
      jsonApi('/api/board'),
      jsonApi('/api/board/chief-plan').catch(function () { return { state: 'unavailable' }; }),
    ]);
  } catch (exc) {
    // A failed first read replaces the lanes, which would only show zeros;
    // a later one keeps them under a stale line.
    failedFetchState(els.boardFetchState, !!state.board, state.boardFetchedAt,
      'the Board', function () { fetchBoard().catch(function () {}); });
    if (els.boardColumns) els.boardColumns.hidden = !state.board;
    throw exc;
  }
  // Read before a rename landed: it would put the old title back.
  if (editSeq !== boardEditSeq) return;
  setAsyncState(els.boardFetchState, 'ready');
  if (els.boardColumns) els.boardColumns.hidden = false;
  state.board = board;
  state.boardFetchedAt = Date.now();
  state.chiefPlan = plan;
  renderBoard();
  // A never-fetched cache (the webapp restarted since the last refresh) heals
  // on the poll too, not only on tab activation — a Board left open across a
  // restart would otherwise sit on "not loaded" until ↻. Still never
  // an errored cache (that is ghStale's rule), and throttled so a refresh
  // that throws before recording its error can't become a 5 s gh loop.
  const gh = state.board && state.board.github;
  if (gh && !ghFetched(state.board) && !gh.error && Date.now() - lastAutoRefreshAt > GH_STALE_MS) {
    lastAutoRefreshAt = Date.now();
    refreshGithub().catch(function () {});
  }
}

// ?board=<sid> deep-link: land on the Board with that card's drawer
// open, its section unfolded and scrolled into view. Called from main.js at boot. `sid` may
// be a card's own session_id OR its state_sid — a Slack ping only ever
// knows the hook's transcript UUID, which is the card's
// state_sid, not its session-host session_id.
export async function openBoardCard(sid) {
  setTab('board');
  // Distinguish a failed fetch from a genuinely-missing sid: a transient
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

// While a refresh runs the ↻ spins (COLOR-02): aria-busy, never the
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

export const DESKTOP_BOARD = '(min-width: 700px) and (pointer: fine)';

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
