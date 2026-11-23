# Prevent lock

**Settings › Prevent lock** keeps the PC from locking for being idle. It is off by default and changes no Windows setting: no power plan, no screen-saver policy.

## What it does

While the switch is on, one loop in the tray-owned webapp (`src/desktop/prevent_lock.py`) wakes every 15 seconds and:

- **Nudges the pointer** 1 px, up and then down in turn, so the pointer never drifts. The move goes out through the repo's one `SendInput` seam (`src/desktop/input.py`) as a relative move; it never clicks and never presses a key.
- **Holds a keep-awake request**: `SetThreadExecutionState` with the system and display required. The flags belong to the calling thread, so a parked thread holds them. Switching off releases them at once, and the process ending drops them too.

With the switch off, nothing is held and nothing is sent.

## Real input wins

Windows keeps one idle clock for all input, the nudge's own included. The loop remembers the clock's tick right after each nudge; a later beat whose tick differs means something else moved the pointer or pressed a key, and that beat is skipped (a skipped beat waits a full interval and does not flip the direction). A beat is also skipped when the idle clock is under 15 s and no nudge of ours has been sent yet.

## State

The switch is `prevent_lock` in `config/webapp_config.json`, saved through `POST /api/config` like the other switches, so it survives a restart. `GET /api/config` returns the stored value and the page paints from it. Only the canonical webapp runs the loop: a disposable gate webapp never moves the real pointer.

## Known limits

A non-elevated process cannot inject input into an elevated window, and the lock screen refuses it. When Windows accepts no move, the loop logs one warning and tries again next beat. The keep-awake request still holds in that case.
