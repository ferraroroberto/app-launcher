# Launcher-owned PTY + interactive phone terminal

**Date:** 2026-05-14
**Issue:** #1 — *Rearchitect Claude Code sessions around a launcher-owned PTY*
**See also:** [`2026-05-14-lessons-launcher-owned-pty.md`](2026-05-14-lessons-launcher-owned-pty.md)
— the didactic retrospective (reasoning, gotchas, best practices).

## What was done

Replaced the "detached CMD window the launcher keeps no handle on" model
with launcher-owned ConPTY sessions, and built a full interactive terminal
on the phone on top of it — live output, scrollback, typing, `Ctrl+C`,
`/quit`, image paste — gated behind Tailscale + a WebAuthn passkey.

### Architecture

- **session-host** (`app/session_host/`, `src/session_host.py`) — a
  separate long-lived process, loopback-only on port `8446`, that owns
  every `claude` ConPTY (via `pywinpty`). Each session has an output ring
  buffer for scrollback-on-reconnect and writes a full transcript. Started
  and owned by the tray like `cloudflared`, so it survives a webapp
  restart.
- **webapp** (`app/webapp/server.py`) — proxies a browser WebSocket
  through to the session-host and is the single auth choke point:
  Tailscale gating, bearer token, and the passkey terminal token are all
  re-checked here (WebSockets bypass the HTTP middleware, so the WS route
  re-applies them).
- **frontend** — `xterm.js` (vendored, no build step) rendering a
  full-screen terminal overlay; tap a session to open, *‹ Sessions* to go
  back.

### Security

- Terminal endpoints (WebSocket, image upload, WebAuthn) are **Tailscale
  only**: rejected on the `Cf-Ray` header (public tunnel) and required to
  come from `100.64.0.0/10` / loopback / `tailnet_allowlist`.
- **WebAuthn passkey gate** (`src/webauthn_gate.py`): platform passkey
  (Face ID), enrolled-device whitelist in `config/webauthn_devices.json`,
  one-time enrollment window opened from the tray. A passkey assertion
  mints a 12 h terminal token required by the WS + image endpoints.
- **Audit** (`src/audit.py`): `webapp/terminal_audit.log` for cross-session
  events; `webapp/sessions/<id>.log` + `<id>.transcript` per session.

### Other

- Interactive terminal window on the PC: with `claude_show_local_window`
  (default on), launching a session from the phone also opens an `--app`
  browser window on the PC pointed at `?terminal=<sid>` over loopback.
  Because the session-host fans output to every client and accepts input
  from all of them, the phone and the PC drive the same session.
- Image paste: phone uploads → session-host writes the file under
  `<project>/.launcher-tmp/` → types the path into the PTY via bracketed
  paste.

## Files

**Added**

- `app/session_host/__init__.py`, `app/session_host/server.py`
- `app/cli/commands/session_host_cmd.py`
- `src/session_host.py`, `src/session_client.py`, `src/webauthn_gate.py`,
  `src/audit.py`
- `app/webapp/static/vendor/` — vendored `xterm.js` + fit / web-links addons
- `docs/2026-05-14-launcher-owned-pty.md` (this file)

**Modified**

- `app/webapp/server.py` — Tailscale gating, WS proxy, session + WebAuthn
  routes, launch-as-PTY-session
- `app/cli/commands/__init__.py` — register `session-host`
- `app/tray/tray.py` — own the session-host process, *🔐 Enroll device* menu
- `src/launcher.py` — `spawn_claude_session`
- `src/webapp_config.py` — `session_host_port`, `tailnet_allowlist`,
  `claude_show_local_window`, `webauthn_*`
- `app/webapp/static/{index.html,app.js,styles.css}` — terminal overlay,
  passkey UX, settings section
- `requirements.txt` — `pywinpty`, `webauthn`, `python-multipart`
- `config/webapp_config.sample.json`, `.gitignore`, `README.md`

**Removed**

- `src/sessions.py`, `src/console_ctrl.py` — the discover-by-scan +
  Ctrl+C-attach mechanism, superseded by launcher-owned PTYs.

## Validation

- `py_compile` across all changed modules — clean.
- Booted session-host + webapp; verified: healthz, launch a real `claude`
  PTY session, WebSocket proxy streams the TUI output, resize + input
  frames apply, image upload lands under `.launcher-tmp/` with the path
  returned, stop (quit / kill) works.
- Static assets (`vendor/xterm.js`, `app.js?v=6`, terminal overlay markup)
  serve from the webapp.

Passkey enrollment / assertion against a real iOS authenticator is
verified manually on-device (requires the `.ts.net` HTTPS origin with the
trusted CA).

## Follow-up — two launch modes + mirror sizing

After on-device testing, three refinements:

- **Two launch modes, chosen by a toggle.** The Claude Code tab gained a
  **☁️ Detached** toggle in the options card (off by default). Off →
  full-control ConPTY session (as above). On → a **detached** session: the
  session-host spawns `claude` in its own `CREATE_NEW_CONSOLE` window
  (`RemoteSession`) and only *tracks* the process handle. Detached sessions
  show in the running-sessions list (tagged `☁️ detached`), can be killed
  from the phone, but have no WebSocket — the Claude cloud app drives them,
  and they outlive a session-host restart. `POST /sessions` takes a
  `kind` (`pty` | `remote`); `src/launcher.spawn_claude` was removed (the
  webapp now routes both modes through `spawn_claude_session`).
- **Phone drives, PC mirrors.** A full-control session is one ConPTY with
  one size. The WS proxy tags each client `role=pc` (loopback) or
  `role=phone`; the session-host honours `resize` only from the phone, and
  the PC mirror window renders whatever size the phone set (re-synced from
  `to_api`'s `rows`/`cols`). The two clients no longer fight over
  dimensions.
- The per-row `✏️ Rename` button was removed from Claude Code rows (kept
  on the Apps tab); `📜 Generate BAT files` shortened to `📜 Create BATs`.

**Files:** `src/session_host.py` (`RemoteSession`, `create_remote`, size
tracking), `app/session_host/server.py` (`kind` dispatch, `role` gating),
`src/session_client.py`, `app/webapp/server.py`, `src/launcher.py`,
`app/webapp/static/{index.html,app.js,styles.css}`.

## Follow-up 2 — UI consolidation

A pass to cut icon clutter and fix the terminal overlay on long output:

- **Edit mode.** A Settings toggle (off by default, persisted in
  `localStorage`) gates per-row `✏️ rename` + `🗑️ remove` on both lists.
  In normal use the rows are icon-free — no per-row icon inflation.
- **Header stripped to the title.** The `⚙️` and `🔎` header buttons are
  gone; `🔎 Scan` and `📜 Create BATs` moved into the Settings panel as
  occasional-use actions. The Settings `<details>` opens via its own
  summary.
- **Terminal bar pinned.** Opening the terminal adds `terminal-open` to
  `<body>` (scroll-locked); the overlay is `overflow: hidden` and the bar
  + status rows are `flex-shrink: 0`. Long output can no longer scroll
  the bar off-screen or reveal the app list underneath.

**Files:** `app/webapp/static/{index.html,app.js,styles.css}`, `README.md`.
