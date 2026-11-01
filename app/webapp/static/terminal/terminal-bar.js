/* Terminal bar ⋮ session menu + the floating "Latest" pill.
 *
 * The bar carries only ‹ Back · title · context ring (context-ring.js) ·
 * Terminal⇄Chat toggle (* session-overlay.js) · 🔊 · ⋮ — no permanent
 * ✕ Kill or ↓ Jump control, so the bar fits a 390px phone without
 * scrolling.
 *
 *   ⋮ menu — Rename · Copy link · Changed files · [Background tasks, Claude
 *   sessions only] · [Hide/Show steps · Reload, Chat mode
 *   only] · [Compact, Claude sessions only] · Stop and kill. Copy link prefers the session's provider-native
 *   URL and falls back to this launcher's tailnet-only ?session= link, the
 *   same resolution the Rename / link dialog uses (see
 *   sessionCopyLink below). Built once on the vendored row-menu component
 *   (the same one as the sessions-list gear); the chat-only rows are
 *   declared with function-valued `hidden`, so the menu re-evaluates them
 *   on every open and they are simply absent from the DOM in Terminal
 *   mode. Every item resolves the open session when tapped, so one menu
 *   serves whichever session the overlay shows.
 *
 *   Latest pill — shown only while the viewport is more than one screen
 *   above the tail; a tap jumps back down and the pill hides again. It is
 *   a child of #terminalHost, so the Chat pane hides it with the terminal.
 */

import { els, state } from '../state.js';
import { apiFailToast, toast } from '../api.js';
import {
  canCompact, openSessionRename, providerWebUrl, sendOutcome,
  sendSessionMessage, sessionTitle, stopSession,
} from '../sessions.js';
import { openSessionChanges } from '../changes/changes-overlay.js';
import { hasBackgroundTasks, openBackgroundTasks } from '../background-tasks/background-tasks-sheet.js';
import { hasLinks, openLinks } from '../links/links-sheet.js';
import { createRowMenu } from '../_vendored/row-menu/row-menu.js';
import { createLatestPill } from '../latest-pill.js';
import { refreshTerminalTitle, setTerminalTitleText } from './terminal-mirror.js';
import {
  groupsAreHidden, hasTranscriptReader, loadNew, reloadNewest, toggleGroups,
} from '../chat/session-transcript.js';
import { inChatMode } from '../session-overlay.js';
import { isHiddenChannel } from '../channel-sessions.js';

// Drops below the overlay bar, right edges 8px in. The offset is measured
// from the bar's border box, so 1 here puts the menu 2px under its padding
// box, where it always sat (the bar's bottom border is 1px).
const terminalMenu = createRowMenu({
  className: 'terminal-menu',
  placement: {
    against: function (kebab) { return kebab.closest('.terminal-bar'); },
    offset: 1, inset: 8,
  },
});

// The open session as the list knows it (it carries kind / agent /
// manual_title for the chief guard and the rename dialog). Falls back to
// the object the overlay was opened with — a bare {session_id, name} for a
// ?session= deep link or a Board drill-down opened before the list loaded.
function currentSession() {
  const v = state.sessionView;
  if (!v) return null;
  const sid = v.session.session_id;
  const s = (state.sessions || []).find(function (x) {
    return x.session_id === sid;
  });
  return s || v.session;
}

// The shareable launcher link that drops straight into this session
// (main.js boot reads ?session=<sid>; unlike ?terminal= it never claims
// PC-mirror ownership).
function sessionShareUrl(sid) {
  return window.location.origin + window.location.pathname +
    '?session=' + encodeURIComponent(sid);
}

// What ⋮ Copy link writes, and how to describe it. The launcher's own
// ?session= URL is tailnet-gated by design — the terminal endpoints refuse
// anything that did not arrive from 100.64.0.0/10 — so it is dead outside the
// tailnet and is the *fallback*, not the answer. When the session has a
// provider-native link (Claude's, resolved by sessions.js::providerWebUrl
// from the same `web_url` the Rename / link dialog shows) copy that instead:
// it is authenticated by the Anthropic account and opens anywhere. The toast
// names which one went to the clipboard rather than folding the two — a
// silent fallback can quietly bring back the tailnet link
// without anyone noticing.
//
// `s` may be the bare {session_id, name} a ?session= deep link opens with,
// which carries no agent or web_url at all — providerWebUrl('' agent) is ''
// and that path lands on the fallback, never on `undefined`.
function sessionCopyLink(s) {
  const web = providerWebUrl(s);
  if (web) return { url: web, message: 'Claude web link copied' };
  return {
    url: sessionShareUrl(s.session_id),
    message: 'Launcher link copied (tailnet only)',
  };
}

export function closeTerminalMenu() {
  terminalMenu.close();
}

function notInChat() {
  return !inChatMode();
}

// Whether ⋮ Changed files applies: any agent with a
// transcript reader (a missing agent reads as Claude), whose transcript the
// fold reads.
function hasChangedFiles(s) {
  return !!s && hasTranscriptReader(s);
}

// Background tasks: what this Claude session launched in the background,
// running and finished, in both modes.
const BACKGROUND_TASKS_ITEM = {
  dataset: { item: 'overlay-tasks-btn' }, glyph: 'clipboard-list', label: 'Background tasks',
  ariaLabel: 'Show background tasks',
  hidden: function () { return !hasBackgroundTasks(currentSession()); },
  onTap: function () {
    const s = currentSession();
    if (s) openBackgroundTasks(s);
  },
};

// Links: every artifact and URL the transcript mentioned, in both modes.
const LINKS_ITEM = {
  dataset: { item: 'overlay-links-btn' }, glyph: 'link', label: 'Links',
  ariaLabel: 'Show links',
  hidden: function () { return !hasLinks(currentSession()); },
  onTap: function () {
    const s = currentSession();
    if (s) openLinks(s);
  },
};

// The shared ↓ Latest pill (latest-pill.js) over the xterm buffer,
// created by wireTerminalMenu().
let latestPill = null;

export function wireTerminalMenu() {
  terminalMenu.attach('terminal', els.terminalMenu, [
    {
      glyph: 'pencil', label: 'Rename', ariaLabel: 'Rename session',
      onTap: renameCurrentSession,
    },
    {
      glyph: 'link', label: 'Copy link', ariaLabel: 'Copy session link',
      onTap: copyCurrentSessionLink,
    },
    // Changed files: every file this session edited, folded from
    // its transcript into the Show changes panel. In both modes.
    {
      glyph: 'file-diff', label: 'Changed files',
      hidden: function () { return !hasChangedFiles(currentSession()); },
      onTap: function () {
        const s = currentSession();
        if (s) openSessionChanges(s, sessionTitle(s));
      },
    },
    BACKGROUND_TASKS_ITEM,
    LINKS_ITEM,
    // Chat-only: the transcript bar's 👁 and 🔄 moved here when the
    // transcript became a pane of this overlay. Absent from the DOM in
    // Terminal mode (function-valued `hidden`, the row menu). The step lines
    // show by default, so the item reads "Hide steps" first.
    {
      glyph: function () { return groupsAreHidden() ? 'eye' : 'eye-off'; },
      label: function () { return groupsAreHidden() ? 'Show steps' : 'Hide steps'; },
      ariaLabel: function () {
        return groupsAreHidden()
          ? 'Show steps: tool calls and system entries'
          : 'Hide steps: tool calls and system entries';
      },
      hidden: notInChat,
      onTap: toggleGroups,
    },
    // Load new: only what came after the newest turn shown, the
    // live tick's own read, forced now. Reload stays as the full escape hatch.
    {
      glyph: 'arrow-down', label: 'Load new', ariaLabel: 'Load new messages',
      hidden: notInChat,
      onTap: loadNew,
    },
    {
      glyph: 'rotate-ccw', label: 'Reload', ariaLabel: 'Reload transcript',
      hidden: notInChat,
      onTap: reloadNewest,
    },
    // Compact: sends Claude Code's /compact in one tap, in either
    // mode, through the same verified /input route Chat's Send uses, so the
    // toast says what actually happened. Claude only: /compact is its
    // command, and other agents would take it as a prompt.
    {
      glyph: 'chevrons-down-up', label: 'Compact', ariaLabel: 'Compact conversation',
      hidden: function () { return !canCompact(currentSession()); },
      onTap: compactCurrentSession,
    },
    {
      dataset: { item: 'action-stop-close' }, glyph: 'x', danger: true,
      label: 'Stop and kill', ariaLabel: 'Stop and kill session',
      // A hidden Telegram session can be looked at, never stopped.
      hidden: function () { return isHiddenChannel(currentSession()); },
      // stopSession() keeps the chief confirm and closes the overlay
      // once the session it is showing stops.
      onTap: function () {
        const s = currentSession();
        if (s) stopSession(s);
      },
    },
  ]);

  latestPill = createLatestPill(els.terminalLatest, {
    isAway: function () {
      const t = state.terminal;
      if (!t || !t.term) return false;
      const b = t.term.buffer.active;
      return isScrolledAwayFromTail(b.viewportY, b.baseY, t.term.rows);
    },
    jump: function () {
      const t = state.terminal;
      if (t && t.term) t.term.scrollToBottom();
    },
  });
}

// The menu's Rename: the bar shows the new name at once.
function renameCurrentSession() {
  const s = currentSession();
  if (!s) return;
  const t = state.terminal;
  openSessionRename(s, function (title) {
    // manual_title wins in sessionTitle(), so the bar shows the new
    // name now instead of waiting for the next title poll (which
    // then keeps it in step); an empty title clears the override, as
    // on the server. No list fetch: the list stays unrendered under
    // the overlay, and closing it refreshes the list on the way out.
    // A detached session viewed in Chat has no terminal to refresh
    // through — set the bar title directly.
    s.manual_title = title;
    if (t && t === state.terminal && t.sid === s.session_id) {
      refreshTerminalTitle(t, s);
    } else {
      setTerminalTitleText(s);
    }
  });
}

// The menu's Copy link.
function copyCurrentSessionLink() {
  const s = currentSession();
  if (!s) return;
  const link = sessionCopyLink(s);
  // iOS only allows a clipboard write inside the tap gesture:
  // writeText is called synchronously from the click handler.
  navigator.clipboard.writeText(link.url).then(
    function () { toast(link.message, 'good', { icon: 'link' }); },
    function (exc) { apiFailToast('Copy link failed', exc); }
  );
}

// The menu's Compact, with the verified send's outcome in a toast.
function compactCurrentSession() {
  const s = currentSession();
  if (!canCompact(s)) return;
  sendSessionMessage(s.session_id, '/compact').then(
    function (verdict) {
      const outcome = sendOutcome(verdict);
      toast('Compact: ' + outcome.text, outcome.kind, { icon: 'chevrons-down-up' });
    },
    function (exc) { apiFailToast('Compact failed', exc); }
  );
}

// More than one screen of scrollback below the viewport → show the pill.
// A full-screen (alternate-buffer) agent has no scrollback, so it never
// shows there. Kept rather than unified with the scroll panes' 120px: a
// terminal scrolls in rows, and a screen is its natural "away" unit.
function isScrolledAwayFromTail(viewportY, baseY, rows) {
  return baseY - viewportY > rows;
}

export function updateLatestPill() {
  if (latestPill) latestPill.update();
}

// Keep the pill in step with one terminal's scroll position. Scrolling
// (touch, wheel, scrollToBottom), new output landing while scrolled up,
// and a reflow can each move the viewport off or back onto the tail.
// Coalesced to one check per frame by the pill; a stashed warm terminal
// is inert. Returns a disposer.
export function watchLatestPill(t) {
  const schedule = function () {
    if (latestPill && t === state.terminal) latestPill.schedule();
  };
  const subs = [
    t.term.onScroll(schedule),
    t.term.onWriteParsed(schedule),
    t.term.onResize(schedule),
  ];
  return function () {
    subs.forEach(function (d) { try { d.dispose(); } catch (_) {} });
  };
}
