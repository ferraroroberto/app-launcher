/* Running Claude Code sessions panel: list, stop, refresh.
 *
 * A row is one tap into the session overlay, plus a kebab (⋮) pinned right
 * and centred against the full row height, which opens the row's floating
 * action menu (Terminal · Chat · Rename · Stop).
 *
 * The kebab replaces the gear, not the action set: the gear becomes a
 * kebab (⋮) in the same 52px slot, carrying the same menu — contents,
 * order and behaviour unchanged, only the anchor's glyph differs. No
 * disclosure chevron is rendered anywhere on the row; a chevron in place
 * of the kebab deletes the row's whole action set instead of relocating
 * it, since the actions would then be reachable only by opening the
 * overlay first.
 *
 * stopSession() below is still the one "Stop and kill" path,
 * same for both kinds, called from this menu, the overlay's ⋮ menu and the
 * Board drawer. The session-host types the agent's own quit command (Claude's /quit,
 * Copilot's /exit, …), waits briefly for a clean exit so shutdown hooks
 * run, then force-terminates as a fallback — and the window always closes.
 * Detached (remote) rows have no PTY to type into, so the host force-kills
 * the console directly. One exception to the no-confirm design: the fleet
 * chief — see stopSession()'s isChiefSession() guard.
 */

import { els, state } from './state.js';
import { apiFailToast, isDesktopClient, jsonApi, logPollFailure, opensInPcWindow, toast } from './api.js';
import { terminalJsonApi } from './webauthn.js';
import { renderHomeHead } from './home-head.js';
import { isHiddenChannel, isOffBoard, renderChannelSummaries } from './channel-sessions.js';
// The session overlay's two modes. Circular with this module by
// design (session-overlay.js → terminal.js / session-transcript.js → here
// for sessionTitle and the send helpers); nothing runs at import time.
import { closeSessionOverlay, openSessionOverlay, resolveSessionMode } from './session-overlay.js';
import { CHIEF_KILL_CONFIRM, CHIEF_RESTART_CONFIRM, channelKillConfirm, channelSessionName, isChannelSession, fmtDuration, isChiefSession, revealInCard } from './dom-utils.js';
import { confirmDialog } from './confirm-dialog.js';
import { attentionChip, chip, sessionRowBody, sessionStatusBits } from './glance.js';
import { renderUsage } from './usage-meter.js';
import { createRowMenu } from './_vendored/row-menu/row-menu.js';
import { icon } from './_vendored/icons/icons.js';
import { hasTranscriptReader } from './chat/session-transcript.js';
// runChiefAction (and the ensureChief it wraps) lives in board-dispatch.js
// (split off board.js), exported
// for this cross-tab use, as is the chief settings dialog; board.js already imports
// stopSession/sessionTitle/openSessionRename from this module, so this
// mirrors the existing sessions.js<->terminal.js circular-import pattern
// rather than introducing a new risk.
import { runChiefAction } from './board/board-dispatch.js';
import { openSettingsAt } from './tabs.js';

// Kept as a named re-export (apps.js, jobs.js, jobs-row.js, and this
// module's own row render all import it) over the shared formatter in
// dom-utils.js — same "3h 24m" elapsed-time granularity as before.
export function fmtAgo(epochSeconds) {
  return fmtDuration(epochSeconds, { fromEpoch: true, granular: true });
}

// Last path segment of a session's project dir — the project folder name
// the row's context line shows in place of the full path.
function projectFolder(s) {
  const dir = String((s && s.project_dir) || '');
  const parts = dir.split(/[\\/]/).filter(Boolean);
  return parts.length ? parts[parts.length - 1] : '';
}

// The folder name lowercased, used to spot a live title that merely echoes it.
function projectBasename(s) {
  return projectFolder(s).toLowerCase();
}

// Display title for a session, with smart precedence. The single source of truth both the Coding tab and the Board
// tab call, so a live session shows an identical title on both — do not
// re-derive a title in board.js or anywhere else.
//
//   0. ``manual_title`` — a launcher-native rename always wins.
//      It is the one title channel that works identically across every
//      agent (including detached sessions) without depending on agent-native
//      OSC support, so it overrides every auto-derived source below.
//   1. ``shared_name`` (joined agent-aware server-side into
//      every session dict as shared_name/shared_name_source) wins outright
//      when it's a real Claude-assigned title (name_source !== 'derived') —
//      the one cross-tab authoritative source: Claude's own /resume picker
//      title, kept fresh on every UserPromptSubmit/Stop hook fire.
//   2. else the OSC-parsed ``live_title`` — kept as a same-poll-cycle-faster
//      supplement: it updates sub-second inside an open terminal (parsed
//      straight off the PTY), while the shared source only refreshes on the
//      next hook fire + sessions-state.json poll. Only some agents self-name
//      per conversation this way: Claude emits a real summary, Codex emits
//      "<folder> | <model>", Pi emits "π - <folder>", Antigravity/Copilot
//      emit nothing — a short title that's just the folder name is a
//      project echo, no more distinctive than the launch name, so it's
//      skipped here in favor of prompt_title below.
//   3. else the first-prompt-derived title (prompt_title) — covers agents
//      that don't self-name, and de-genericizes the folder-only echoes.
//   4. else the shared name even when it's the generic derived
//      "<project>-N" fallback — still better than a bare project echo since
//      it distinguishes sibling sessions in the same directory.
//   5. else the folder-echo live title, then the launch name.
// Coding agents prefix their live title with a brand glyph (Claude's green ✳);
// the per-session agent icon already identifies the agent, so strip any
// leading run of non-alphanumeric characters.
export function sessionTitle(s) {
  const manual = String((s && s.manual_title) || '').trim();
  if (manual) return manual;
  const shared = String((s && s.shared_name) || '').trim();
  const sharedDerived = !!(s && s.shared_name_source === 'derived');
  const live = String((s && s.live_title) || '')
    .replace(/^[^\p{L}\p{N}]+/u, '')
    .trim();
  const prompt = String((s && s.prompt_title) || '').trim();
  const base = projectBasename(s);
  // A short live title containing the folder name is a project echo (Codex /
  // Pi), no more distinctive than the launch name. A real summary is longer
  // and not folder-dominated, so the word-count guard lets it through.
  const projectEcho = !!live && !!base &&
    live.toLowerCase().includes(base) && live.split(/\s+/).length <= 4;
  if (shared && !sharedDerived) return shared;
  if (live && !projectEcho) return live;
  if (prompt) return prompt;
  if (shared) return shared;
  return live || (s && s.name) || (s && s.project) || 'session';
}

// ------------------------------------------------- row action menu
//
// One kebab per row opens a floating menu of the row's actions. The rail has
// no room for four separate icons on the phone without crowding the title,
// so they live behind one anchor. The menu machinery (open/close, outside
// tap, Escape, surviving the 4s poll re-render) is the vendored row-menu
// It floats left of the row's button column, top-aligned and
// overlapping it by 4px, so a long menu grows down over the rows below.
const sessionMenu = createRowMenu({
  className: 'session-menu',
  placement: {
    against: function (kebab) { return kebab.parentElement; },
    side: 'left', align: 'start', offset: -4,
  },
});

// The chief's kebab items beyond a session's own: Restart, the same
// graceful stop-then-resume as the Board's Restart, and Chief settings,
// which opens Settings › Chief. Restart only while it runs; settings
// either way.
function chiefMenuItems(kebab, alive) {
  return [
    {
      dataset: { item: 'chief-restart-btn' }, glyph: 'rotate-ccw',
      label: 'Restart', ariaLabel: 'Restart the chief',
      hidden: !alive,
      onTap: function () {
        runChiefAction({
          button: kebab, label: 'Restart', fresh: true, resume: true,
          onDone: fetchSessions, confirmMessage: CHIEF_RESTART_CONFIRM,
        });
      },
    },
    {
      dataset: { item: 'chief-settings-btn' }, glyph: 'sliders-horizontal',
      label: 'Chief settings',
      onTap: function () { openSettingsAt('chiefSheet'); },
    },
  ];
}

// The chief's row while no chief runs (replacing a separate
// "chief: not running" line): the crown avatar, and Start and Resume as plain
// icon-button verbs on the row itself. Start uses the same
// ensure endpoint as the Board's chat mode; Resume reattaches the most
// recent chief conversation, falling back server-side to a fresh spawn when
// nothing is resumable. restart=true: if a chief came up meanwhile, it is still
// stopped and resumed, as on the Board.
function stoppedChiefRow() {
  const li = document.createElement('li');
  li.className = 'app-item chief-stopped-row session-item-chief';

  const main = document.createElement('div');
  main.className = 'app-main';
  const body = document.createElement('div');
  body.className = 'chief-stopped-body';
  sessionRowBody({
    agentId: 'claude', agentLabel: 'Claude Code', badge: 'crown',
    title: 'Chief', meta: 'stopped', titleClass: 'name',
  }).forEach(function (node) { body.appendChild(node); });
  main.appendChild(body);
  li.appendChild(main);

  const actions = document.createElement('div');
  actions.className = 'row-actions session-actions';
  [
    { id: 'codingChiefStart', text: 'Start', glyph: 'play', resume: false,
      restart: false, title: 'Start the fleet chief' },
    { id: 'codingChiefResume', text: 'Resume', glyph: 'rotate-ccw', resume: true,
      restart: true, title: 'Reattach the most recent chief conversation' },
  ].forEach(function (verb) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.id = verb.id;
    btn.className = 'icon-button action-row-verb chief-verb';
    btn.innerHTML = icon(verb.glyph);
    btn.title = verb.title;
    btn.setAttribute('aria-label', verb.text + ' chief');
    btn.addEventListener('click', function () {
      runChiefAction({
        button: btn, label: verb.text, fresh: false, resume: verb.resume,
        restart: verb.restart, onDone: fetchSessions,
      });
    });
    actions.appendChild(btn);
  });
  const kebab = document.createElement('button');
  kebab.type = 'button';
  kebab.className = 'icon-button session-kebab';
  kebab.innerHTML = icon('ellipsis-vertical');
  kebab.title = 'Chief actions';
  kebab.setAttribute('aria-label', 'Chief actions');
  actions.appendChild(kebab);
  sessionMenu.attach('chief:stopped', kebab, chiefMenuItems(kebab, false));
  li.appendChild(actions);
  return li;
}

export function renderSessions() {
  const host = els.sessionsList;
  host.innerHTML = '';
  // Telegram channel sessions leave the list for one summary line.
  const shown = state.sessions.filter(function (s) { return !isHiddenChannel(s); });
  els.sessionsEmpty.hidden = shown.length !== 0;
  renderHomeHead();
  renderChannelSummaries();

  // The chief's row first, running or not: the standing
  // orchestrator is the one session that is always meant to be there.
  const chief = shown.find(isChiefSession);
  const ordered = chief
    ? [chief].concat(shown.filter(function (s) { return s !== chief; }))
    : shown;
  if (!chief) host.appendChild(stoppedChiefRow());

  ordered.forEach(function (s) { host.appendChild(sessionRow(s)); });
  // The open menu's row is gone (session ended) — drop the stale state.
  sessionMenu.endRender();
}

// One Running-sessions row: the open button, then its kebab rail.
function sessionRow(s) {
  const li = document.createElement('li');
  li.className = 'app-item session-item';
  // The crown avatar marks the chief; the class is the hook.
  const isChief = isChiefSession(s);
  if (isChief) li.classList.add('session-item-chief');
  // Stable hook so a test (or any consumer) can target a specific
  // session's row by id rather than position — e.g. the kill regression
  // must act on the session it launched, never ".first".
  li.dataset.sessionId = s.session_id;
  // The session in the wide layout's docked view.
  if (state.sessionView && state.sessionView.session.session_id === s.session_id) {
    li.setAttribute('aria-current', 'true');
  }

  const main = document.createElement('div');
  main.className = 'app-main';

  const remote = s.kind === 'remote';
  main.appendChild(sessionOpenButton(s, isChief, remote));
  li.appendChild(main);

  const actions = document.createElement('div');
  actions.className = 'row-actions session-actions';

  // One kebab, vertically centred against the full row height, opens the
  // row's floating action menu — a vertical icon + label list
  // It sits in its own rail *outside* the row button, so tapping
  // it opens the menu and never the session, while the row's own 60px tap
  // target still opens the session. The menu holds, in order:
  //   · Terminal — full-control rows only: the session overlay in
  //     Terminal mode (the PC mirror window instead, with that setting on
  //     for a desktop browser, as a row tap does).
  //   · Chat — every row whose agent has a transcript reader
  //     (Claude and Codex; hasTranscriptReader mirrors the
  //     endpoint's flavour map): the same overlay in Chat mode. The reader
  //     uses the agent's native history, never the PTY capture a detached
  //     row lacks.
  //   · Open in new window — full-control rows on a desktop browser only:
  //     the PC Edge window, whatever the "Open sessions in a new window"
  //     setting says.
  //   · Copy path — the full project path the row's context
  //     line now shortens to its folder name; the toast shows it on a phone.
  //   · Rename — a launcher-native override that always wins
  //     in sessionTitle()'s precedence, for both kinds. Submitting a blank
  //     title clears it, reverting to the automatic precedence.
  //   · Stop-and-kill, both kinds: the session-host quits
  //     gracefully then force-falls-back; the window always closes. Keeps
  //     the `action-stop-close` hook (data-item; danger text, a danger
  //     tint on press).
  // The glyph is the kebab the Coding tile's project menu already uses;
  // the contents are the gear's, unchanged (second attempt).
  const kebab = document.createElement('button');
  kebab.type = 'button';
  kebab.className = 'icon-button session-kebab';
  kebab.innerHTML = icon('ellipsis-vertical');
  kebab.title = 'Session actions';
  kebab.setAttribute('aria-label', 'Session actions');
  const items = sessionMenuItems(s, kebab, isChief, remote);
  actions.appendChild(kebab);
  sessionMenu.attach(s.session_id, kebab, items);

  li.appendChild(actions);

  return li;
}

// The row's one tap target: it opens the session overlay.
function sessionOpenButton(s, isChief, remote) {
  // Every row opens the session overlay in the mode it was last viewed in
  // a full-control row has a terminal, a detached row of an agent
  // with a transcript reader opens in Chat. A detached row of an agent
  // with *no* reader is tappable too — it opens Chat showing the reader's
  // own reason line. That shape is reachable today (`ssh` is registered in
  // SESSION_HOST_AGENTS and will never have a harness history), so no row
  // is inert and none is stranded. Kept deliberately from the first
  // attempt: restoring the kebab must not restore the dead row.
  const open = document.createElement('button');
  open.className = 'launch-btn session-open';
  open.type = 'button';

  // The shared session row: the agent's mark in an avatar whose
  // badge says it is alive (the chief's says crown), a one-line title, and
  // one meta line, "folder · status · age", then "up uptime". Chips only for exceptions: a detached
  // row says so; a full-control row is the normal case and says nothing.
  // The agent resolves against the registry (state.agents) so a new
  // agent's mark flows through untouched, falling back to Claude Code.
  const known = state.agents.find(function (a) { return a.id === s.agent; });
  const chips = [];
  // What the Board says it needs from you: "needs you" or
  // "stalled", from the Board's own routing on this poll. No chip when
  // the placement is unknown.
  // The chief is out of the Board's lanes, so its row never claims it needs you.
  const attention = isOffBoard(s) ? null : attentionChip(s);
  if (attention) chips.push(attention);
  if (isChannelSession(s)) chips.push(chip(channelSessionName(s), 'neutral', 'session-channel-tag'));
  if (remote) chips.push(chip('detached', 'neutral', 'session-detached'));
  const ago = fmtAgo(s.started_at);
  // The Board's own "status · age", from this poll's Board
  // placement: the same formatter and words as the Board's row. The word
  // is dropped where the attention chip already says it; the age
  // stays. No placement (null) shows neither.
  const statusBits = sessionStatusBits(s.board_status, s.board_age_seconds, {
    chief: isChief,
    hideStatus: !!attention,
  });
  // The folder name, not the full Windows path that wrapped to four lines
  // on a phone. The full path is the row's hover hint and the
  // row menu's Copy path.
  const body = sessionRowBody({
    agentId: known ? known.id : 'claude',
    agentLabel: known ? known.label : 'Claude Code',
    badge: isChief ? 'crown' : (s.alive === false ? 'down' : 'alive'),
    title: sessionTitle(s),
    meta: [projectFolder(s)].concat(statusBits).filter(Boolean).join(' · '),
    metaTail: ago ? 'up ' + ago : '',
    chips: chips,
    // `.name`: the hook every row test reads the title through.
    titleClass: 'name',
  });
  body.forEach(function (node) { open.appendChild(node); });
  open.dataset.kind = remote ? 'remote' : 'pty';
  if (s.project_dir) open.title = s.project_dir;
  open.addEventListener('click', function () { openSession(s); });
  return open;
}

// The row menu's items, in order, Stop last.
function sessionMenuItems(s, kebab, isChief, remote) {
  const items = [
    {
      dataset: { item: 'session-terminal-btn' }, glyph: 'terminal',
      label: 'Terminal', ariaLabel: 'Open terminal',
      hidden: remote,
      onTap: function () { openSession(s, 'terminal'); },
    },
    {
      dataset: { item: 'session-chat-btn' }, glyph: 'messages-square',
      label: 'Chat', ariaLabel: 'Open chat',
      hidden: !hasTranscriptReader(s),
      onTap: function () { openSession(s, 'chat'); },
    },
    {
      dataset: { item: 'session-open-window-btn' }, glyph: 'monitor',
      label: 'Open in new window', ariaLabel: 'Open in a new window',
      hidden: remote || !isDesktopClient(),
      onTap: function () { openWindowFromMenu(s); },
    },
    {
      dataset: { item: 'session-copy-path-btn' }, glyph: 'copy',
      label: 'Copy path', ariaLabel: 'Copy the project path',
      hidden: !s.project_dir,
      onTap: function () { copyProjectPath(s.project_dir); },
    },
    {
      glyph: 'pencil', label: 'Rename', ariaLabel: 'Rename session',
      onTap: function () { openSessionRename(s); },
    },
  ];
  // The chief's own controls sit before Stop, which stays last.
  if (isChief) Array.prototype.push.apply(items, chiefMenuItems(kebab, s.alive !== false));
  items.push({
    dataset: { item: 'action-stop-close' }, glyph: 'x', danger: true,
    label: 'Stop', ariaLabel: 'Stop and kill session',
    onTap: function () { stopSession(s); },
  });
  return items;
}

// The row menu's Open in new window. With the server's window mirroring
// off there is no window to open, so the item says so instead of silently
// doing nothing.
async function openWindowFromMenu(s) {
  if (!(await openPcWindow(s))) {
    toast('Window mirroring is off in the server config', 'error');
  }
}

// The row menu's Copy path: the toast names the path, so a phone (with no
// hover) still sees the full path the row's context line now leaves out.
async function copyProjectPath(path) {
  try {
    await navigator.clipboard.writeText(path);
    toast('Copied ' + path, 'good');
  } catch (exc) {
    apiFailToast('Could not copy the path', exc);
  }
}

// Open (or focus) the session's PC Edge --app window through the mirror
// route. Resolves true when a window is now showing it; false when the server
// has window mirroring disabled, so the caller can fall back to the page. A
// request failure toasts and resolves true: the caller must not then open a
// second view on top of the error.
async function openPcWindow(s) {
  try {
    const r = await jsonApi(
      '/api/claude-code/sessions/' + encodeURIComponent(s.session_id) +
        '/mirror',
      { method: 'POST' }
    );
    if (!(r && r.mirrored)) return false;
    toast(
      (r.action === 'focused' ? 'Focused ' : 'Opened ') +
        sessionTitle(s) + ' window',
      'good',
      { icon: 'monitor' }
    );
  } catch (exc) {
    apiFailToast('Open window failed', exc);
  }
  return true;
}

// Open a session when its row (or its kebab's Terminal / Chat item) is
// tapped — the Board drawer's Chat / Terminal button calls this too.
// `mode` forces 'terminal' or 'chat'; without it the session opens
// in the mode it was last viewed in (session-overlay.js), a detached
// session always in Chat, and a first open in Chat unless Settings says
// Terminal. Everything renders in the page. The one exception is the
// "Open sessions in a new window" setting (off by default) on a desktop
// browser: a full-control session's terminal then opens in a dedicated PC
// Edge --app window — the same window a new-session launch opens — so it can
// be closed without fear while the session keeps running headless. A second
// tap focuses that window rather than spawning a duplicate. The kebab's "Open in new
// window" reaches the same window with the setting off.
export async function openSession(s, mode) {
  const resolved = resolveSessionMode(s, mode);
  if (opensInPcWindow() && s.kind !== 'remote' && resolved === 'terminal') {
    // Mirroring disabled server-side falls through to the in-page terminal.
    if (await openPcWindow(s)) return;
  }
  openSessionOverlay(s, resolved);
}

export async function stopSession(s) {
  // No confirm — one tap stops. The stop is graceful
  // (the agent's own quit, then force-fallback) and a mis-tap is resumable,
  // so a confirmation dialog is just friction. The one exception is the
  // fleet chief (parity with the Board tab's own drawer guard, the
  // isChiefCard() check on the drawer's stop button in board.js::buildDrawer):
  // the chief is the one session a mis-tap shouldn't take down, from either
  // the Coding tab's row or the terminal overlay's kill button (which calls
  // this same function). The Board drawer's own call passes a stripped
  // {session_id, name} object with no kind/label, so isChiefSession() is a
  // safe no-op there and it never double-confirms.
  if (isChiefSession(s) && !(await confirmDialog(CHIEF_KILL_CONFIRM))) return;
  // While Telegram sessions are hidden nothing may stop one: the Board
  // drawer passes a stripped {session_id, name} with no label, so resolve the
  // full record first. The refusal names how to get the control back.
  const full = (state.sessions || []).find(function (x) { return x.session_id === s.session_id; });
  if (isHiddenChannel(full || s)) {
    toast('Telegram sessions are protected — turn off "Hide Telegram sessions" in Settings to stop one.', 'error');
    return;
  }
  if (isChannelSession(s) && !(await confirmDialog(channelKillConfirm(s)))) return;
  try {
    await jsonApi(
      '/api/claude-code/sessions/' + encodeURIComponent(s.session_id) +
        '/stop',
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode: 'quit' }),
      }
    );
    toast('Stopping ' + s.name + '…', 'good', { icon: 'octagon-x' });
    // The overlay showing this session closes whichever pane is up —
    // a detached session viewed in Chat has no state.terminal to key on.
    if (state.sessionView &&
        state.sessionView.session.session_id === s.session_id) {
      closeSessionOverlay();
    }
    setTimeout(fetchSessions, 1500);
  } catch (exc) {
    apiFailToast('Stop failed', exc);
  }
}

export async function fetchSessions() {
  try {
    const body = await jsonApi('/api/claude-code/sessions');
    state.sessions = body.sessions || [];
    // Unknown (null) unless the server said a number: never read as zero.
    state.chiefQuestions = typeof body.chief_questions === 'number' ? body.chief_questions : null;
    renderSessions();
  } catch (exc) {
    // Sessions polling is best-effort — don't spam toasts.
    logPollFailure('sessions fetch failed', exc);
  }
}

// The usage meter's one endpoint: both tabs' meters render from
// this poll, which runs whatever tab is up, so the Coding tab never depends
// on the Board having been opened and the two can never disagree. Both
// heavy agents are always listed, in the same order, whatever the model
// picker says, so there is no selection to race.
const QUOTA_ERROR_LINES = [
  { harness: 'claude', label: 'Claude Code', state: 'error' },
  { harness: 'codex', label: 'Codex', state: 'error' },
];

export async function fetchRateLimits() {
  try {
    const body = await jsonApi('/api/rate-limits');
    renderUsage(body.quota_lines);
  } catch (exc) {
    logPollFailure('rate-limits fetch failed', exc);
    renderUsage(QUOTA_ERROR_LINES);
  }
}

// --------------------------------------------------- rename dialog
//
// One dialog shared by the Coding tab's row rename button and the Board
// tab's drawer rename button (board.js imports openSessionRename), the same
// way #renameDialog is shared across the Apps tab's rename affordances.
// ``onDone`` lets a caller optimistically patch its own view of the session
// instead of always re-fetching (the Board drawer stays open across a
// rename, and patching shows the new title without waiting on a poll).
let renameSessionTarget = null;
let renameSessionOnDone = null;
let sessionLinkFeedbackTimer = null;

// The provider-native session URL (Claude's `claude.ai/code/session_…`
// remote-control link, captured from the PTY by the server as `web_url`) —
// '' when this session has none: a non-Claude agent, or a Claude session
// whose remote-control card has not been printed/scanned yet. It is the one
// link that opens from any browser on any network, so both copy affordances
// — this dialog and the session bar's ⋮ Copy link — resolve through here so
// they can never hand back two different strings for one session.
export function providerWebUrl(s) {
  return s && s.agent === 'claude' ? String(s.web_url || '') : '';
}

export function openSessionRename(s, onDone) {
  renameSessionTarget = s;
  renameSessionOnDone = onDone || null;
  els.sessionRenameInput.value = sessionTitle(s);
  const supportsLinkRow = s.kind !== 'remote' && (s.agent === 'claude' || s.agent === 'codex');
  const webUrl = providerWebUrl(s);
  els.sessionRenameHeading.textContent = supportsLinkRow ? 'Rename / link' : 'Rename session';
  els.sessionLinkRow.hidden = !supportsLinkRow;
  els.sessionLinkInput.value = webUrl || 'Not available yet';
  els.sessionLinkCopy.disabled = !webUrl;
  els.sessionLinkCopy.title = webUrl ? 'Copy link' : 'Web link not available yet';
  els.sessionLinkCopy.setAttribute(
    'aria-label', webUrl ? 'Copy session link' : 'Session web link not available yet'
  );
  window.clearTimeout(sessionLinkFeedbackTimer);
  els.sessionLinkCopy.classList.remove('is-copied');
  els.sessionRenameDialog.showModal();
}

// Send from a session's Chat mode. The chat pane's
// composer and the Board drawer's post to the same kind-agnostic /input
// route: a PTY session submits with the host's framing, settle
// and ingest verification; a detached session is typed into
// its PC console by PID and can only ever answer ``delivered: "unconfirmed"``.
// The row's old Send message dialog is gone — the composer covers
// both kinds — so the gate, the request and the outcome wording live here
// once. The row menu that used to open that dialog still exists (it is the
// kebab's menu), but it no longer carries a Send item.

// A detached session whose agent the registry says was never probed for
// console input (``console_input: false`` from /api/agents). Only an explicit
// false refuses: the boot fallback list carries no flag until that fetch
// lands, and "not known yet" must not grey Send out for the whole open — the
// session-host still refuses an unprobed agent with a 502 console_failed.
export function detachedSendRefused(s) {
  if (!s || s.kind !== 'remote') return false;
  const known = (state.agents || []).find(function (a) { return a.id === s.agent; });
  return !!known && known.console_input === false;
}

// Whether /compact applies: a Claude session (a missing agent reads as
// Claude, as session-transcript.js's hasTranscriptReader does) whose input
// route is open — a detached agent never probed for console input is not.
// Shared by the terminal ⋮ menu and the Telegram sessions popup.
export function canCompact(s) {
  return !!s && (s.agent || 'claude') === 'claude' && !detachedSendRefused(s);
}

// /input is passkey-gated (middleware `_TERMINAL_GUARD_RULES`), so the
// request carries the terminal token — without it a phone behind a configured
// passkey gate gets a 401, which api() turns into the login overlay. Loopback
// and an unconfigured gate resolve '' and the header is simply left off.
export async function sendSessionMessage(sid, text) {
  return terminalJsonApi(
    '/api/claude-code/sessions/' + encodeURIComponent(sid) + '/input',
    { method: 'POST', body: { data: text, submit: true } }
  );
}

// The toast for one successful /input answer: `{ text, kind }`. Each verdict
// gets its own words, because folding them into "sent" is exactly what
// the delivery bugs came from. The failures never get here — a payload the
// terminal never echoed (502 not_ingested), a console that did not take the
// keystrokes (502 console_failed), an exited session (409) all throw, and
// the caller toasts them as "Send failed" with the text kept.
//
// `opts.watch`: the caller will look for evidence the agent took an
// unconfirmed send (Chat reads the transcript it already polls), so the
// verdict says "Sent" and carries `watch: true`; the warning is that look's to
// raise, as NOT_PICKED_UP. A caller that cannot look keeps the old wording,
// because "Sent" with nothing behind it would pass an unchecked send silently.
export const NOT_PICKED_UP = {
  text: 'Sent, but the agent hasn’t picked it up. Your text is back in the box',
  kind: 'error',
};

export function sendOutcome(verdict, opts) {
  const v = verdict || {};
  // Detached: a console has no output stream, so this is the best a
  // successful send can ever be — not a failure.
  if (v.delivered === 'unconfirmed') {
    return { text: 'Sent, not confirmed: typed into the PC console', kind: '' };
  }
  switch (v.submit_state) {
    case 'confirmed':
      return { text: 'Sent', kind: 'good' };
    case 'unconfirmed':
      // A short payload or a bare submit: Enter went in, nothing checked it.
      if (opts && opts.watch) return { text: 'Sent', kind: '', watch: true };
      return { text: 'Sent, not confirmed: nothing verified the agent took it', kind: '' };
    case 'pending':
      // 202: in the agent's composer, Enter still with the watcher.
      return { text: 'Queued: the agent is busy, Enter goes in once it settles', kind: '' };
    case 'not_submitted':
      return { text: 'Not submitted: the text reached the agent but Enter was never sent', kind: 'error' };
    default:
      break;
  }
  // An older session-host carries no submit_state; only an
  // explicit `delivered: true` reads as a landing.
  if (v.delivered === true) return { text: 'Sent', kind: 'good' };
  return { text: 'Sent, not confirmed', kind: '' };
}

function wireSessionRenameDialog() {
  els.sessionRenameCancel.addEventListener('click', function () {
    els.sessionRenameDialog.close();
  });
  els.sessionLinkCopy.addEventListener('click', async function () {
    try {
      await navigator.clipboard.writeText(els.sessionLinkInput.value);
      els.sessionLinkCopy.classList.add('is-copied');
      window.clearTimeout(sessionLinkFeedbackTimer);
      sessionLinkFeedbackTimer = window.setTimeout(function () {
        els.sessionLinkCopy.classList.remove('is-copied');
      }, 650);
      toast('Session link copied', 'good', { icon: 'link' });
    } catch (exc) {
      apiFailToast('Copy link failed', exc);
    }
  });
  els.sessionRenameForm.addEventListener('submit', async function (ev) {
    ev.preventDefault();
    if (!renameSessionTarget) return;
    const title = els.sessionRenameInput.value.trim();
    try {
      const reply = await jsonApi(
        '/api/claude-code/sessions/' +
          encodeURIComponent(renameSessionTarget.session_id) + '/rename',
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ title }),
        }
      );
      els.sessionRenameDialog.close();
      // The session is renamed; its Life OS conversation not following says so.
      if (reply && reply.life_os_sync && reply.life_os_sync.status === 'failed') {
        toast(reply.life_os_sync.error, 'error');
      }
      if (renameSessionOnDone) {
        renameSessionOnDone(title);
      } else {
        await fetchSessions();
      }
    } catch (exc) {
      apiFailToast('Rename failed', exc);
    }
  });
}

export function wireSessions() {
  // No sessions yet: a session starts from a project.
  const sessionsEmptyAction = document.getElementById('sessionsEmptyAction');
  if (sessionsEmptyAction) {
    sessionsEmptyAction.addEventListener('click', function () {
      revealInCard(document.querySelector('details.projects-card'));
    });
  }
  wireSessionRenameDialog();
}
