# Mobile compose bar for predictive-keyboard text entry

**Issue:** #37 · **Date:** 2026-05-21

## What was done

The live-terminal overlay gained a second input mode. A new `✏️` toolbar
button toggles a slim compose bar — a normal `<textarea>` plus a `➤`
Send button — docked at the bottom of the overlay, above the phone
keyboard.

xterm.js delivers characters one at a time by wiping its hidden
`.xterm-helper-textarea` after every keystroke. iOS/Android predictive
keyboards need to see an intact in-progress word inside a real input to
suggest completions and apply autocorrect, so predictive text never
worked when typing directly into the terminal. The compose bar is the
same workaround ttyd and code-server use: type into a plain textarea
with full predictive support, then Send the buffered text to the PTY in
one shot.

Direct typing into xterm is unchanged — best for vim, REPLs, and `y/n`
prompts. The compose bar is purely additive.

## How it works

- `✏️` toggles `setComposeOpen()`, which shows/hides `#terminalComposeBar`
  and moves focus between the textarea (predictive on) and xterm.
- The textarea carries **no** `autocorrect` / `autocapitalize` /
  `spellcheck` / `autocomplete` attributes — the browser defaults are
  exactly what predictive keyboards need.
- `➤` Send forwards `<textarea value> + "\r"` to the PTY over the
  existing WebSocket frame (`{type: "input", data: ...}`), clears the
  bar, and keeps it focused. The frame shape is unchanged, so the
  server, audit trail, and session-host need no changes.
- The textarea auto-grows up to ~4 rows; the iOS return key inserts a
  newline — Send is the only path to the PTY.
- `📋` Paste branches when the bar is open: it inserts the clipboard at
  the textarea caret (`setRangeText`) instead of WS-sending, so the
  user can review before Send.
- The bar is **hidden in the PC mirror window** (`isMirror` — loopback
  already has a real keyboard with predictive support).
- Compose state is reset in `closeTerminal()` so a re-open never shows a
  stale draft.

The CSS docks `.compose-bar` as a `flex-shrink: 0` row at the bottom of
the flex-column overlay; `.terminal-host` (`flex: 1`) shrinks to fit and
xterm re-fits on the `visualViewport` resize. The textarea uses the
system font (not the xterm monospace) at 16px — 16px keeps iOS from
auto-zooming the page on focus.

## iOS spin-off relevance

Issue #40 (`home-stack-ios` unified hub) names this compose bar as the
literal `insertText` destination for the planned system-wide keyboard
extension. The bar is a plain focusable `<textarea>`, so it carries into
the `WKWebView` shell unchanged and the keyboard extension can target it
like any other text field.

## Files modified

- `app/webapp/static/index.html` — `#terminalCompose` button;
  `#terminalComposeBar` (textarea + `➤` send button).
- `app/webapp/static/state.js` — added `terminalCompose`,
  `terminalComposeBar`, `terminalComposeInput`, `terminalComposeSend`.
- `app/webapp/static/terminal.js` — `t.composeOpen` flag; `setComposeOpen()`,
  `growComposeInput()`, `resetComposeBar()`, `wireCompose()`; the `✏️`
  mirror gate in `openTerminal()`; the Paste handler's compose branch;
  `resetComposeBar()` in `closeTerminal()`.
- `app/webapp/static/styles.css` — `.compose-bar`, `.compose-input`,
  `.compose-send`.
- `tests/e2e/test_compose_bar.py` — **new** regression test (#37): pins
  the mirror gate (`✏️` hidden under loopback) and the `➤` Send path
  (`<text>\r` reaches the session log).
- `README.md` — regression-net table updated.

No server-side change (`server.py`, routers, session-host, audit log,
WS schema all untouched). No `?v=` cache-bust — content-hash asset
stamping is owned by #30.

## Validation

- `scripts/verify-before-ship.ps1` — byte-compile + non-e2e pytest (80
  passed) + Playwright e2e on Chromium and WebKit/iPhone (45 passed, 7
  skipped). Exit 0.
- Verified on a real iPhone: `✏️` opens the bar with predictive
  suggestions, `➤` Send runs the typed text in the terminal, direct
  xterm typing still works, and the bar is absent in the PC mirror.

## Deferred

Image-into-bar integration — when the bar is open, `🖼` should insert the
uploaded image's path at the textarea caret instead of pasting it into
the PTY. That needs a session-host or upload-endpoint change and is
tracked as a separate follow-up issue.
