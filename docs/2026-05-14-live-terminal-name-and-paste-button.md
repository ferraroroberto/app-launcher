# Live terminal name + clipboard-paste button

Date: 2026-05-14 · Issue: [#3](https://github.com/ferraroroberto/app-launcher/issues/3) · Branch: `feat/live-terminal-name-and-paste-button`

Two UX gaps in the launcher-owned PTY terminal (follow-up to #1).

## What was done

### A. Live terminal name

`claude` advertises its window title via OSC escape sequences (`ESC ] 0|2 ; <title> BEL/ST`) and renames the terminal mid-session. Nothing in the stack captured that, so the running-sessions list and the terminal title bar showed only the static launch-time `name` (the project name).

- `src/session_host.py` — `PtySession` now parses OSC 0/2 title sequences out of the PTY stream on the reader thread (`_scan_title`, with a 512-char carry buffer for sequences split across read chunks), stores the result in `PtySession.title`, and exposes it in `to_api()`.
- `app/webapp/static/app.js` — the sessions list renders `s.title || s.name`; `openTerminal` subscribes to xterm's `term.onTitleChange` to update the terminal bar title and `document.title` (so the PC mirror `--app` window's OS title bar tracks it too). `hideTerminal` restores `document.title`.

### B. Clipboard-paste button

Mobile click-and-paste into the xterm host is unreliable, and only image paste had a dedicated button. Added a one-tap text-paste button.

- `app/webapp/static/index.html` — new icon-only `#terminalPaste` button (📋) in `.terminal-bar-actions`, immediately left of the image button.
- `app/webapp/static/app.js` — click handler reads `navigator.clipboard.readText()` and sends it into the session over the existing `{type:'input'}` WebSocket frame.
- `app.js` cache-bust bumped to `v=12` in `index.html`.

## Files modified

- `src/session_host.py`
- `app/webapp/static/index.html`
- `app/webapp/static/app.js`

## Validation

- `py_compile src/session_host.py` — OK
- `node --check app/webapp/static/app.js` — OK
- Not run against the live launcher — deferred to a later session per the issue.
