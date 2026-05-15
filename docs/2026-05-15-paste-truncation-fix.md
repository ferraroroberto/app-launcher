# Paste button truncation fix

Date: 2026-05-15 · Issue: [#4](https://github.com/ferraroroberto/app-launcher/issues/4) · Branch: `feat/live-terminal-name-and-paste-button`

## What was done

The clipboard-paste button (📋) added in #3 silently dropped part of long pastes — `pywinpty.PtyProcess.write()` can return fewer bytes than requested (often 0 on a busy ConPTY input pipe), and `PtySession.write` was calling it once and discarding the return value.

`scripts/repro_paste_truncation.py` confirms the short-write behaviour: writes of 200/1000/4000/10000/50000 chars against a freshly-spawned `cmd.exe` ConPTY all returned `0` from pywinpty's `write()`.

### Fix

`src/session_host.py` — `PtySession.write` now loops until the whole payload is written, slicing off `n` bytes per successful call and sleeping 10 ms on zero-byte returns. A 5-second total retry budget guards against a stuck pipe hanging the websocket pump; a `logger.warning` reports the dropped-byte count if the budget is exhausted.

## Files modified

- `src/session_host.py`
- `scripts/repro_paste_truncation.py` (new — minimal repro)

## Validation

- `py_compile src/session_host.py` — OK
- `python -m scripts.repro_paste_truncation` — reproduces short-write against cmd.exe (exit 1).
- Live paste test against the phone terminal — deferred to a follow-up session.
