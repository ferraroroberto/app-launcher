/* The launch-size contract every PTY-launch call site shares: how big the
   in-page terminal will be, or that a PC mirror window opens instead. Kept
   free of the terminal module graph so a launch path does not load it. */

import { opensInPcWindow } from './api.js';

// Estimate the phone's terminal size (rows × cols) BEFORE a session
// exists, so the launch request can spawn the PTY at the right width and
// a full-screen differential TUI (Codex's ratatui) paints its first frame
// at the correct width instead of the legacy 40×120 — which wrapped/cut on
// a portrait phone. Measures one monospace cell with the same
// font the live terminal uses, then divides the visual viewport. Cols (the
// cause of the "cut") is what matters; rows a touch high is harmless —
// applySize sends the exact size on WS open and ratatui reflows. Any
// failure falls back to the legacy 40×120 default.
const _TERM_FONT =
  '13px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace';

export function estimateTermSize() {
  try {
    const span = document.createElement('span');
    span.style.cssText =
      'position:absolute;visibility:hidden;white-space:pre;font:' + _TERM_FONT;
    span.textContent = 'W'.repeat(100);
    document.body.appendChild(span);
    const rect = span.getBoundingClientRect();
    const cellW = rect.width / 100;
    const cellH = rect.height;
    document.body.removeChild(span);
    const vp = window.visualViewport;
    const w = (vp && vp.width) || window.innerWidth || 0;
    const h = (vp && vp.height) || window.innerHeight || 0;
    if (!(cellW > 0) || !(cellH > 0) || !(w > 0) || !(h > 0)) {
      return { rows: 40, cols: 120 };
    }
    return {
      rows: Math.max(10, Math.min(200, Math.floor(h / cellH))),
      cols: Math.max(20, Math.min(300, Math.floor(w / cellW))),
    };
  } catch (_) {
    return { rows: 40, cols: 120 };
  }
}

// PC-window-vs-in-page launch-size contract, shared by every PTY-launch call
// site (Coding tab, Board issue-start/dispatch, Life OS recap/skill launch): a desktop browser
// with "Open sessions in a new window" on gets the dedicated PC mirror window
// sized to its own default; anything else renders the session in the page, and
// carries its real terminal size so the PTY's
// first frame is painted at the right width. Mutates `payload` in place —
// callers apply their own outer gate (e.g. "only for a streamed/pty launch")
// before calling this.
//
// `in_page: true` is the explicit "I will render this myself"
// signal the server's `should_mirror_to_pc` now requires from a genuine SPA
// loopback client, instead of inferring it from "loopback and no desktop
// flag" — an inference a non-browser loopback API caller (a script, the
// fleet chief) also matched, silently leaving it with no window at all. A
// phone sets it too, but it's moot there: the server already mirrors any
// non-loopback caller before this flag is ever consulted.
export function applyLaunchSizePayload(payload) {
  if (opensInPcWindow()) {
    payload.desktop = true;
  } else {
    payload.in_page = true;
    const sz = estimateTermSize();
    payload.rows = sz.rows;
    payload.cols = sz.cols;
  }
}
