/* Session overlay mode routing (#982, Step 3 of #979).
 *
 * One overlay (#terminalOverlay), two render modes of the same session:
 *
 *   terminal — the live PTY (terminal.js): xterm + WebSocket + composer.
 *   chat     — the agent's own history as chat-style cards
 *              (session-transcript.js), for any session kind whose agent
 *              has a transcript reader.
 *
 * Switching modes shows/hides a pane; nothing reconnects or reloads. A
 * connected terminal stays connected while Chat is showing (its resize
 * path is inert, see terminal.js applySize), and the chat pane keeps its
 * pages and fold state while Terminal is showing. The terminal is
 * connected lazily — opening straight into Chat never runs the passkey /
 * WebSocket path until Terminal is first tapped.
 *
 * Availability: Terminal needs a PTY (`kind !== 'remote'`); Chat needs a
 * reader for the agent. A segment the session can't offer is aria-disabled
 * and a tap on it toasts the reason. A detached session opens in Chat; a
 * PTY session of an agent with no reader opens in Terminal; otherwise the
 * mode the session was last viewed in wins (remembered per session id in
 * localStorage), defaulting to Terminal. A session that can offer *neither*
 * (detached, no reader) still opens, in Chat, on the reader's reason line
 * — the overlay is the only place its Rename and Stop live (#1025).
 */

import { els, state } from './state.js';
import { toast } from './api.js';
import {
  attachTerminalPane,
  hideTerminal,
  showSessionShell,
  stashActiveTerminal,
} from './terminal.js';
import {
  chatPaneSession,
  closeChatComposerPopovers,
  closeChatPane,
  hasTranscriptReader,
  openChatPane,
  pinChatToKeyboard,
  syncLiveRefresh,
} from './session-transcript.js';
import { applyTermTheme } from './terminal-theme.js';
import { closeTerminalMenu, updateLatestPill } from './terminal-bar.js';
import { closeSpeakPopover, revealReadAloudButton } from './terminal-readaloud.js';
import { terminalComposer } from './terminal-compose.js';
import { isWideLayout } from './layout.js';
import { setTab } from './tabs.js';

// Last-viewed mode per session id — one JSON map under one key rather than
// a key per session, capped so ended sessions don't accumulate forever.
const MODES_KEY = 'launcher.sessionModes';
const MODES_MAX = 50;

function readModes() {
  try {
    const raw = localStorage.getItem(MODES_KEY);
    const obj = raw ? JSON.parse(raw) : {};
    return obj && typeof obj === 'object' && !Array.isArray(obj) ? obj : {};
  } catch (_) {
    return {};
  }
}

export function lastMode(sid) {
  const m = readModes()[sid];
  return m === 'chat' || m === 'terminal' ? m : null;
}

function rememberMode(sid, mode) {
  try {
    const modes = readModes();
    // Re-insert so insertion order doubles as LRU.
    delete modes[sid];
    modes[sid] = mode;
    const keys = Object.keys(modes);
    while (keys.length > MODES_MAX) delete modes[keys.shift()];
    localStorage.setItem(MODES_KEY, JSON.stringify(modes));
  } catch (_) { /* private mode / quota: the mode just isn't remembered */ }
}

export function terminalAvailable(s) {
  return !!s && s.kind !== 'remote';
}

export function chatAvailable(s) {
  return hasTranscriptReader(s);
}

function agentLabel(s) {
  const known = (state.agents || []).find(function (a) { return a.id === s.agent; });
  return known ? known.label : (s.agent || 'this agent');
}

// The one-line reason a segment is off, shown as the segment's title and
// toasted on tap (a phone has no hover).
export function modeReason(s, mode) {
  if (mode === 'terminal') return 'Detached session — no terminal';
  return 'No transcript reader for ' + agentLabel(s);
}

function inChatMode() {
  return !!els.terminalOverlay && els.terminalOverlay.dataset.mode === 'chat';
}

function segment(mode) {
  return mode === 'chat' ? els.sessionModeChat : els.sessionModeTerminal;
}

function syncSegment(mode, session) {
  const btn = segment(mode);
  if (!btn) return;
  const ok = mode === 'chat' ? chatAvailable(session) : terminalAvailable(session);
  const label = mode === 'chat' ? 'Chat' : 'Terminal';
  if (ok) {
    btn.removeAttribute('aria-disabled');
    btn.title = label;
    btn.setAttribute('aria-label', label);
  } else {
    btn.setAttribute('aria-disabled', 'true');
    btn.title = modeReason(session, mode);
    btn.setAttribute('aria-label', label + ' — ' + btn.title);
  }
}

function syncPressed(mode) {
  ['terminal', 'chat'].forEach(function (m) {
    const btn = segment(m);
    if (btn) btn.setAttribute('aria-pressed', m === mode ? 'true' : 'false');
  });
}

// Release a keyboard pin (#135) left by whichever pane was showing, so the
// next pane starts from the CSS-driven full height; the pane re-pins itself
// if the keyboard is genuinely still up.
function releasePin() {
  const o = els.terminalOverlay;
  if (!o) return;
  o.style.height = '';
  o.style.bottom = '';
  o.style.top = '';
}

export function setSessionMode(mode) {
  const v = state.sessionView;
  if (!v) return;
  const s = v.session;
  if (mode === 'chat' && !chatAvailable(s)) mode = 'terminal';
  if (mode === 'terminal' && !terminalAvailable(s)) mode = 'chat';
  const changed = v.mode !== mode;
  v.mode = mode;
  els.terminalOverlay.dataset.mode = mode;
  syncPressed(mode);
  rememberMode(s.session_id, mode);
  if (changed) {
    releasePin();
    closeTerminalMenu();
    closeSpeakPopover();
    terminalComposer.closePopovers();
    closeChatComposerPopovers();
  }
  // The chat pane's live refresh follows the mode (#1050): showing Terminal
  // stops it, showing Chat starts it. Called for both so a window sitting on
  // Terminal never fetches chat for the session it is showing — the "10
  // windows, only one being read" constraint the feature exists for.
  syncLiveRefresh();
  if (mode === 'terminal') {
    const t = state.terminal;
    if (t && t.sid === s.session_id) {
      // Already connected (or warm): just re-measure the pane it hid behind.
      if (t.applySize) t.applySize();
      updateLatestPill();
      try { t.term.focus(); } catch (_) { /* no xterm yet */ }
    } else {
      attachTerminalPane(s);
    }
  } else {
    if (chatPaneSession() !== s.session_id) openChatPane(s);
    // A detached session never runs attachTerminalPane's reveal (#988) — the
    // 🔊 button lives in the shared bar, so Chat mode reveals it here too.
    revealReadAloudButton();
    updateLatestPill();
    pinChatToKeyboard();
  }
  // The user background override paints the overlay only behind the
  // terminal; the chat pane is a reading surface on the app canvas.
  applyTermTheme();
}

// The wide layout's list-and-detail (#1135): the row whose session is in the
// docked view keeps the accent tint. The mark is harmless below 1100px,
// where the view covers the list; CSS only paints it at the wide query.
export function markSelectedSession(sid) {
  document.querySelectorAll('#sessionsList li.session-item').forEach(function (li) {
    if (sid && li.dataset.sessionId === sid) li.setAttribute('aria-current', 'true');
    else li.removeAttribute('aria-current');
  });
}

export function openSessionOverlay(session, wantMode) {
  if (!session || !session.session_id) return;
  // On a wide window the view is the Code tab's detail pane, so a session
  // opened from elsewhere (the Board drawer) lands on the Code tab.
  if (isWideLayout() && state.tab !== 'claude') setTab('claude');
  const termOK = terminalAvailable(session);
  const chatOK = chatAvailable(session);
  // A session that can offer neither pane still opens (#1025): both segments
  // render aria-disabled and the chat pane shows the reader's own reason
  // line, which beats an unexplained dead row. That shape — detached, agent
  // with no reader — is the only way Rename and Stop stay reachable for it
  // now that the row gear is gone, and they live in the bar's ⋮ menu.
  let mode = wantMode || lastMode(session.session_id) ||
    (termOK ? 'terminal' : 'chat');
  if (mode === 'chat' && !chatOK) mode = 'terminal';
  if (mode === 'terminal' && !termOK) mode = 'chat';
  // Panes left showing another session are stale for this one: the chat
  // pane is closed, and an active terminal is stashed warm (#430) so its
  // title poll can't keep writing the old title over this session's bar
  // while Chat is showing (Terminal mode re-attaches through the cache).
  if (chatPaneSession() && chatPaneSession() !== session.session_id) closeChatPane();
  if (state.terminal && state.terminal.sid !== session.session_id) stashActiveTerminal();
  showSessionShell(session);
  state.sessionView = { session: session, mode: null };
  syncSegment('terminal', session);
  syncSegment('chat', session);
  setSessionMode(mode);
  markSelectedSession(session.session_id);
}

export function closeSessionOverlay() {
  state.sessionView = null;
  closeChatPane();
  if (els.terminalOverlay) delete els.terminalOverlay.dataset.mode;
  hideTerminal();
  markSelectedSession(null);
}

export function wireSessionModeToggle() {
  // A wide window shows the rail beside the docked view, so another tab is
  // one click away. The view belongs to the Code tab's list: leaving it
  // closes the view (a terminal stays warm, #430), rather than leaving a
  // hidden chat polling for a reader who has gone (#1050).
  document.addEventListener('launcher:tab', function (ev) {
    const tab = ev.detail && ev.detail.tab;
    if (tab !== 'claude' && state.sessionView && isWideLayout()) closeSessionOverlay();
  });
  if (!els.sessionMode) return;
  els.sessionMode.addEventListener('click', function (ev) {
    const btn = ev.target.closest('.session-mode-btn');
    if (!btn || !state.sessionView) return;
    const mode = btn.dataset.mode;
    if (btn.getAttribute('aria-disabled') === 'true') {
      toast(modeReason(state.sessionView.session, mode), '', {
        icon: mode === 'terminal' ? 'cloud' : 'messages-square',
      });
      return;
    }
    setSessionMode(mode);
  });
}

export { inChatMode };
