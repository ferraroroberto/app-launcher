/* PC-mirror-window concerns for the live terminal (issue #315 split off
 * terminal.js): the OS-title marker Stop & Close matches on, deciding
 * whether *this* open is a mirror, and keeping the header/OS title in sync
 * with the session's live title.
 *
 * The PC mirror window is the launcher-spawned Edge --app window: it is
 * opened via the ?terminal=<sid> deep-link (state.isMirrorWindow, set at
 * boot) AND connects over loopback. Both conditions are required — a
 * human's own desktop browser over loopback also reports reason 'loopback'
 * but is NOT a mirror, and treating it as one made Stop & Close
 * window.close() the user's actual Chrome window (issue #241).
 */

import { els, state } from './state.js';
import { sessionTitle } from './sessions.js';

// The PC mirror window's OS title: the human session title first (so it shows
// in the Windows/PTI title bar) followed by the hidden marker the launcher's
// EnumWindows scan matches — as a substring — to close/reconcile the window
// (issue #20). The marker must always be present; the scan tolerates text
// around it (it already handles Edge prepending the app name).
export function mirrorDocTitle(sid, title) {
  const marker = 'app-launcher-mirror-' + sid;
  return title ? title + ' — ' + marker : marker;
}

// True when the *current* terminal open is the launcher-spawned PC mirror
// window, as opposed to a human's own desktop browser also reaching the
// session over loopback (issue #241 — both report reason 'loopback', only
// the mirror also carries state.isMirrorWindow from the ?terminal= deep-link).
export function isMirrorWindowSession() {
  return !!state.isMirrorWindow &&
    !!(state.status && state.status.terminal &&
       state.status.terminal.reason === 'loopback');
}

// Push the current title onto the open overlay header and, for a mirror
// window, the OS title bar (keeping the close marker). Called on open and on
// each title poll so a later rename (Claude's evolving summary, a first-prompt
// title landing) updates the live window (issue #266).
export function refreshTerminalTitle(t, session) {
  const title = sessionTitle(session);
  if (els.terminalTitle) els.terminalTitle.textContent = title;
  if (t.mirror) document.title = mirrorDocTitle(t.sid, title);
}

// Mirror window uses a uniquely identifiable OS title so the launcher
// can find this Edge --app window via EnumWindows and dismiss it
// with WM_CLOSE on Stop & Close (issue #20). Must run on every open
// because Edge sets the title from the page after load. The
// console.info is intentional — open DevTools on the mirror window
// to confirm the title was actually applied if Stop & Close fails.
export function announceMirrorWindow(sid, title) {
  const mirrorTitle = mirrorDocTitle(sid, title);
  document.title = mirrorTitle;
  console.info('[app-launcher] mirror title set:', mirrorTitle);
}
