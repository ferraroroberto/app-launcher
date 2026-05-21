# On-screen keys popover for the mobile terminal

**Issue:** #36 · **Date:** 2026-05-21

## What was done

The live-terminal overlay's top bar lost its `^C` (Send Ctrl+C) and `⏹`
(Quit session) buttons and gained a single `⌨️` button. `⌨️` toggles a
3×3 D-pad popover anchored below the bar:

```
[Esc]  [ ↑ ]  [Tab]
[ ← ]  [ ↵ ]  [ → ]
       [ ↓ ]
```

iPhone keyboards (SwiftKey and most third-party keyboards) have no
arrow keys, Esc, or Tab — which made Claude's TUI prompts (Yes/No/Always
pickers, plan-mode approval, `AskUserQuestion` lists, slash-command
autocomplete) impossible to drive from the phone terminal. The popover
restores those keys.

The `^C`/`⏹` slots were free to reclaim: sessions are now stopped from
the outer "Running sessions" list, not from inside the terminal overlay.

## How it works

Each key carries a `data-key` attribute mapped to a VT/xterm escape
sequence (`↑`→`\x1b[A`, `↓`→`\x1b[B`, `→`→`\x1b[C`, `←`→`\x1b[D`,
`↵`→`\r`, `Esc`→`\x1b`, `Tab`→`\t`). A delegated click handler on the
popover sends the bytes over the existing terminal WebSocket as
`{type: "input", data: ...}` — the same channel the 📋 paste button
uses — so the server, audit trail, and session-host need no changes.

Behaviour:

- The popover **stays open** across arrow/Tab taps so the user can chain
  `↓ ↓ ↵` without reopening — the key mitigation that makes a popover
  acceptable instead of an always-visible key row.
- It closes on a tap outside, on the `⌨️` button again, or after `↵`/`Esc`
  (those usually end a prompt).
- Opening the popover snaps the terminal to the bottom (like the `↓`
  jump-to-latest button) — opening it signals the user is about to drive
  a prompt, and prompts live at the tail.
- Focus returns to the terminal after each key.

The popover lives inside `.terminal-bar` (made `position: relative`) and
is absolutely positioned below it.

## Files modified

- `app/webapp/static/index.html` — removed `#terminalCtrlC` /
  `#terminalQuit`; added `#terminalKeys` button and the
  `#terminalKeysPopover` grid.
- `app/webapp/static/state.js` — swapped the two removed element
  references for `terminalKeys` / `terminalKeysPopover`.
- `app/webapp/static/terminal.js` — deleted the `^C` and Quit click
  handlers (and the now-unused `jsonApi` import); added `wireKeysPopover()`,
  `openKeysPopover()` / `closeKeysPopover()` (with an outside-tap
  `pointerdown` listener), and a `closeKeysPopover()` call in
  `hideTerminal()`.
- `app/webapp/static/styles.css` — `.terminal-bar { position: relative }`;
  added `.keys-popover` / `.key-btn` / `.key-spacer`; removed the unused
  `.term-btn.danger` rule.
- `tests/e2e/test_keys_popover.py` — **new** regression test (#36):
  toggles the popover, taps `↓` and asserts `\x1b[B` reaches the session
  log, confirms the popover stays open then closes on `↵`, and asserts
  the `^C`/Quit buttons are gone.
- `README.md` — regression-net table updated.

No `?v=` cache-bust query strings were added — `index.html` has no such
pattern and content-hash asset stamping is owned by #30.

## Validation

- `scripts/verify-before-ship.ps1` — byte-compile + non-e2e pytest (80
  passed) + Playwright e2e on Chromium and WebKit/iPhone (41 passed, 7
  skipped). Exit 0.
- Verified on a real iPhone: popover toggles, `↑↓` navigate an
  `AskUserQuestion` list, `↵` selects and closes the popover, `Tab`
  keeps it open, outside-tap closes it, and opening it scrolls the
  terminal to the bottom.
