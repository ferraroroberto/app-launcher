# iOS PWA terminal scroll-lock

Date: 2026-05-15 · Issue: [#7](https://github.com/ferraroroberto/app-launcher/issues/7)

## What was done

When the launcher was installed as a PWA on iOS and a terminal session was open, vertical drags inside the xterm output area would intermittently rubber-band the entire document, sliding the terminal bar (`‹ Sessions`, title, paste/image/^C/⏹) up under the iOS status bar.

`html, body { overscroll-behavior-y: contain }` and `body.terminal-open { overflow: hidden }` were already in place but iOS standalone doesn't fully honor either when the gesture starts inside a scrollable descendant — the body still translates, and a `position: fixed` overlay translates visually with it.

### Fix

Standard iOS scroll-lock pattern: while the terminal is open, pin `<body>` at its current scroll offset with `position: fixed`, then restore the offset on close so the user lands back where they were.

- `app/webapp/static/styles.css` — `body.terminal-open` now adds `position: fixed; top: var(--terminal-scroll-y, 0px); left: 0; right: 0; width: 100%;` on top of the existing `overflow: hidden`.
- `app/webapp/static/app.js` — new `lockBodyScroll()` / `unlockBodyScroll()` helpers capture `window.scrollY` into a CSS custom property on open and call `window.scrollTo(0, savedScrollY)` on close. The three sites that previously toggled the `terminal-open` class directly now go through these helpers.
- `app/webapp/static/index.html` — bumped `styles.css?v=11 → v=12` and `app.js?v=12 → v=13` so the installed PWA picks up the new files on refresh.

## Files modified

- `app/webapp/static/styles.css`
- `app/webapp/static/app.js`
- `app/webapp/static/index.html`

## Validation

- `node -e "new Function(require('fs').readFileSync('app/webapp/static/app.js','utf8'))"` — JS parses clean.
- No server restart performed: the launcher owns the active PTYs; restarting it would drop running sessions. Static files are served from disk each request, so a hard refresh of the PWA on the phone is sufficient to pick up the change.
- Live device verification deferred to the user — open a session, swipe up/down hard inside the output, confirm the bar stays pinned below the status bar.
