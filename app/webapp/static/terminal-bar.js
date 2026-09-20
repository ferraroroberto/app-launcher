/* Terminal bar ⋮ session menu + the floating "Latest" pill (#981, Step 2
 * of #979).
 *
 * The bar used to carry ✕ Kill and ↓ Jump as permanent controls. They
 * left the bar so it fits a 390px phone without scrolling: ‹ Back · title
 * · Terminal⇄Chat toggle (#982, session-overlay.js) · 🔊 · ⋮.
 *
 *   ⋮ menu — Rename · Copy link · [Show/Hide tool calls · Reload, Chat mode
 *   only] · Stop and kill. Copy link prefers the session's provider-native
 *   URL and falls back to this launcher's tailnet-only ?session= link, the
 *   same resolution the Rename / link dialog uses (#1096 — see
 *   sessionCopyLink below). Built once on the shared row-menu.js component
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

import { els, state } from './state.js';
import { apiFailToast, toast } from './api.js';
import { openSessionRename, providerWebUrl, stopSession } from './sessions.js';
import { createRowMenu } from './row-menu.js';
import { refreshTerminalTitle, setTerminalTitleText } from './terminal-mirror.js';
import { groupsAreHidden, reloadNewest, toggleGroups } from './session-transcript.js';
import { inChatMode } from './session-overlay.js';

const terminalMenu = createRowMenu('terminal-menu');

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

// What ⋮ Copy link writes, and how to describe it (#1096). The launcher's own
// ?session= URL is tailnet-gated by design — the terminal endpoints refuse
// anything that did not arrive from 100.64.0.0/10 — so it is dead outside the
// tailnet and is the *fallback*, not the answer. When the session has a
// provider-native link (Claude's, resolved by sessions.js::providerWebUrl
// from the same `web_url` the Rename / link dialog shows) copy that instead:
// it is authenticated by the Anthropic account and opens anywhere. The toast
// names which one went to the clipboard rather than folding the two — a
// silent fallback is how #981 re-introduced the tailnet link for two weeks
// without anyone noticing (#879 had already removed it).
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

export function wireTerminalMenu() {
  const menu = terminalMenu.attach('terminal', els.terminalMenu, [
    {
      glyph: 'pencil', label: 'Rename session', text: 'Rename',
      onTap: function () {
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
      },
    },
    {
      glyph: 'link', label: 'Copy session link', text: 'Copy link',
      onTap: function () {
        const s = currentSession();
        if (!s) return;
        const link = sessionCopyLink(s);
        // iOS only allows a clipboard write inside the tap gesture:
        // writeText is called synchronously from the click handler.
        navigator.clipboard.writeText(link.url).then(
          function () { toast(link.message, 'good', { icon: 'link' }); },
          function (exc) { apiFailToast('Copy link failed', exc); }
        );
      },
    },
    // Chat-only (#982): the transcript bar's 👁 and 🔄 moved here when the
    // transcript became a pane of this overlay. Absent from the DOM in
    // Terminal mode (function-valued `hidden`, row-menu.js).
    {
      glyph: function () { return groupsAreHidden() ? 'eye' : 'eye-off'; },
      label: function () {
        return groupsAreHidden()
          ? 'Show tool calls and system entries'
          : 'Hide tool calls and system entries';
      },
      text: function () { return groupsAreHidden() ? 'Show tool calls' : 'Hide tool calls'; },
      hidden: notInChat,
      onTap: toggleGroups,
    },
    {
      glyph: 'rotate-ccw', label: 'Reload transcript', text: 'Reload',
      hidden: notInChat,
      onTap: reloadNewest,
    },
    {
      className: 'action-stop-close', glyph: 'x',
      label: 'Stop and kill session', text: 'Stop and kill',
      // stopSession() keeps the chief confirm (#547) and closes the overlay
      // once the session it is showing stops.
      onTap: function () {
        const s = currentSession();
        if (s) stopSession(s);
      },
    },
  ]);
  els.terminalMenu.closest('.terminal-bar').appendChild(menu);

  els.terminalLatest.addEventListener('click', function () {
    const t = state.terminal;
    if (!t || !t.term) return;
    try { t.term.scrollToBottom(); } catch (_) {}
    updateLatestPill();
  });
}

// More than one screen of scrollback below the viewport → show the pill.
// A full-screen (alternate-buffer) agent has no scrollback, so it never
// shows there.
function isScrolledAwayFromTail(viewportY, baseY, rows) {
  return baseY - viewportY > rows;
}

export function updateLatestPill() {
  const t = state.terminal;
  let show = false;
  if (t && t.term) {
    try {
      const b = t.term.buffer.active;
      show = isScrolledAwayFromTail(b.viewportY, b.baseY, t.term.rows);
    } catch (_) { /* hidden is the safe default */ }
  }
  if (els.terminalLatest.hidden === show) els.terminalLatest.hidden = !show;
}

// Keep the pill in step with one terminal's scroll position. Scrolling
// (touch, wheel, scrollToBottom), new output landing while scrolled up,
// and a reflow can each move the viewport off or back onto the tail.
// Coalesced to one check per frame; a stashed warm terminal (#430) is
// inert. Returns a disposer.
export function watchLatestPill(t) {
  let frame = 0;
  const schedule = function () {
    if (frame || t !== state.terminal) return;
    frame = window.requestAnimationFrame(function () {
      frame = 0;
      if (t === state.terminal) updateLatestPill();
    });
  };
  const subs = [
    t.term.onScroll(schedule),
    t.term.onWriteParsed(schedule),
    t.term.onResize(schedule),
  ];
  return function () {
    if (frame) window.cancelAnimationFrame(frame);
    subs.forEach(function (d) { try { d.dispose(); } catch (_) {} });
  };
}
