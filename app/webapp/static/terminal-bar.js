/* Terminal bar ⋮ session menu + the floating "Latest" pill (#981, Step 2
 * of #979).
 *
 * The bar used to carry ✕ Kill and ↓ Jump as permanent controls. They
 * left the bar so it fits a 390px phone without scrolling: ‹ Back · title
 * · [mode toggle, #982] · 🔊 · ⋮.
 *
 *   ⋮ menu — Rename · Copy link · Stop and kill. Built once on the shared
 *   row-menu.js component (the same one as the sessions-list gear), and
 *   every item resolves the open session when tapped, so one menu serves
 *   whichever session the overlay shows.
 *
 *   Latest pill — shown only while the viewport is more than one screen
 *   above the tail; a tap jumps back down and the pill hides again.
 */

import { els, state } from './state.js';
import { apiFailToast, toast } from './api.js';
import { openSessionRename, stopSession } from './sessions.js';
import { createRowMenu } from './row-menu.js';
import { refreshTerminalTitle } from './terminal-mirror.js';

const terminalMenu = createRowMenu('terminal-menu');

// The open session as the list knows it (it carries kind / agent /
// manual_title for the chief guard and the rename dialog). Falls back to a
// bare {session_id, name} when the overlay was opened before the list
// loaded — a ?session= deep link or a Board drill-down.
function currentSession() {
  const t = state.terminal;
  if (!t) return null;
  const s = (state.sessions || []).find(function (x) {
    return x.session_id === t.sid;
  });
  return s || { session_id: t.sid, name: els.terminalTitle.textContent || t.sid };
}

// The shareable launcher link that drops straight into this session
// (main.js boot reads ?session=<sid>; unlike ?terminal= it never claims
// PC-mirror ownership).
function sessionShareUrl(sid) {
  return window.location.origin + window.location.pathname +
    '?session=' + encodeURIComponent(sid);
}

export function closeTerminalMenu() {
  terminalMenu.close();
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
          // the overlay, and hideTerminal() refreshes it on the way out.
          s.manual_title = title;
          if (t && t === state.terminal) refreshTerminalTitle(t, s);
        });
      },
    },
    {
      glyph: 'link', label: 'Copy session link', text: 'Copy link',
      onTap: function () {
        const s = currentSession();
        if (!s) return;
        // iOS only allows a clipboard write inside the tap gesture:
        // writeText is called synchronously from the click handler.
        navigator.clipboard.writeText(sessionShareUrl(s.session_id)).then(
          function () { toast('Session link copied', 'good', { icon: 'link' }); },
          function (exc) { apiFailToast('Copy link failed', exc); }
        );
      },
    },
    {
      className: 'action-stop-close', glyph: 'x',
      label: 'Stop and kill session', text: 'Stop and kill',
      // stopSession() keeps the chief confirm (#547) and hides the overlay
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
