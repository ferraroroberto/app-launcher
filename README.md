# 🚀 Launcher

Phone-first launcher hub. One tap on your phone → a CMD window opens on the home PC and either:

- runs `claude --remote-control …` in a project folder (**Claude Code** tab), or
- spawns any registered Streamlit / FastAPI launcher (**Apps** tab).

Sister project to [`photo-ocr`](https://github.com/) and [`voice-transcriber`](https://github.com/) — same FastAPI + SPA + PWA + Cloudflare-tunnel stack, but for kicking off other processes instead of doing work itself.

> Three ways to reach it from your phone:
> - **Local** (same Wi-Fi): `https://<pc-hostname>:8445`
> - **Tailscale** (anywhere): `https://<pc>.<tailnet>.ts.net:8445`
> - **Cloudflare named tunnel**: `https://launcher.<your-domain>` (no tailnet required)

---

## What it does, in one screen

The web UI has two tabs:

- **Claude Code** — every `.code-workspace` and orphan `*-remote.bat` in your projects directory becomes a button, with **two launch modes** chosen by the **☁️ Detached** toggle in the options card:
  - **Toggle off → full control.** `claude` starts inside a **launcher-owned pseudo-console (ConPTY)** and the phone drops straight into a **live, fully interactive terminal** — real output, scrollback, typing, `Ctrl+C`, `/quit`, image paste.
  - **Toggle on → detached.** `claude` opens in its own console window on the PC. The launcher only *tracks* it — it shows in the running-sessions list (tagged `☁️ detached`) and you can kill it from the phone, but there's no streamed terminal; the Claude **cloud app** drives it. It survives a launcher restart.

  Running sessions are listed above the project buttons, each tagged `⚡ full control` or `☁️ detached`; tap a full-control one to re-attach, tap *‹ Sessions* to come back. Flags (model / effort / verbose / debug) are a one-tap panel above the list. See [Interactive terminal](#interactive-terminal-from-the-phone) for the security model.
- **Apps** — every `*.bat` under your scan root that the classifier recognises as Streamlit, a FastAPI webapp, or a Cloudflare-tunnel script. Tap → fresh CMD window runs the bat. Tunnel rows surface a live `📡 <url>` under the launch button, refreshed every 4 s.

Both tabs share **one** registry file: `config/apps.json`. **Settings** (the panel at the bottom) holds the occasional-use actions: **🔎 Scan** walks both scan paths and shows what's new in a checklist; **📜 Create BATs** generates `*-remote.bat` files. **Edit mode** there reveals per-row ✏️ rename and 🗑️ remove on every list — off by default, so the lists stay icon-free in normal use.

Smart-kill: the settings panel polls common app ports (8443, 8444, 8445, 8501, 5050) and lists what's actually listening. One tap stops the right PID — no hardcoded "kill :8501" buttons that fire blind.

---

## Install

```powershell
cd app-launcher
.\setup.bat
```

That creates `.venv`, installs deps, and generates the PWA icons. After this runs once, `tray.bat` is enough for day-to-day use.

If you came from the old `automation\launcher\` Flask version, your apps list and Claude flags survive — copy `automation\launcher\apps_config.json` → `app-launcher\config\apps.json` and `automation\launcher\config.json`'s contents into `app-launcher\config\webapp_config.json` under the matching `claude_*` keys.

---

## Run

```powershell
.\tray.bat           # tray icon + webapp (normal use, no console window)
.\webapp.bat         # uvicorn standalone, no tray (dev / headless)
```

Both bind `0.0.0.0:8445`. If `webapp/certificates/cert.pem` is present (`scripts/gen_ssl_cert.py` generates it), the server is HTTPS — otherwise plain HTTP.

The tray icon menu has:

- **🚀 Open launcher** — open the local URL in the default browser
- **📋 Copy local URL** — clipboard the loopback URL with `?token=…` baked in
- **📋 Copy Tailscale URL** — clipboard `https://<host>.<tailnet>.ts.net:8445?token=…`
- **📋 Copy Cloudflare URL** — clipboard the public tunnel URL with `?token=…`
- **🔄 Restart webapp** — pick up code changes without losing the tunnel
- **ℹ️ Status** — quick popup with running state + base URL

### Confirming which build the phone is running

Every `/static/*.{js,css}` URL carries a content-hash query string (`?v=<8 hex>`) computed at boot, so editing any asset busts iOS Safari's cache automatically — no more "did the deploy take?" guessing. Hashed assets are served with `Cache-Control: public, max-age=31536000, immutable`; `index.html` itself stays `no-cache, must-revalidate`.

To verify visually, the Settings panel (bottom of the launcher) shows a build line:

```
Build: 35caad4 · 2026-05-19 21:34
```

- **`git_sha`** — `git rev-parse --short HEAD` at the moment the webapp process started. Changes only across commits.
- **`built_at`** — process start time. Changes on **every** restart, even with no code change — useful as a "did the tray actually restart?" anchor.

Backed by `GET /api/version`, which also returns the current `asset_hash` for quick diff against the PC. The line updates only when the webapp module re-imports (i.e., tray restart or 🔄 Restart webapp) — a phone refresh alone won't move it.

---

## Phone install (PWA)

The launcher is a PWA — installs to the iPhone home screen, full-screen, no Safari chrome. First-time setup is two short detours because the webapp uses a self-signed cert.

**One-time iPhone trust setup**

1. Open `https://<pc-hostname>:8445/install-ca` in Safari → tap **Allow** to download the profile.
2. **Settings → General → VPN & Device Management** → tap "Launcher Local CA" → **Install** → enter passcode → confirm.
3. **Settings → General → About → Certificate Trust Settings** (at the very bottom of the About list) → toggle **Launcher Local CA** ON → confirm the warning. *This step is easy to miss — the install in step 2 places the CA in the keychain but does not trust it for TLS.*
4. **Force-quit Safari** (swipe up, dismiss the Safari card). Safari caches negative-trust decisions per-process; the toggle alone is not enough to flip an already-open page from "Not Secure" to trusted.

**Then install the PWA**

5. Reopen the launcher URL in Safari. Lock icon should be solid, no "Not Secure".
6. **Share → Add to Home Screen**. The launcher rocket icon lands on your home screen.

On Android, Chrome shows an "Install app" prompt the second visit; the icon goes on the home screen the same way. Android trusts the system store; for manual install the CA is also served as DER at `/static/ca.crt`.

After that the launcher behaves like a native app — full-screen, no Safari chrome.

> **Debugging the phone:** when the [pre-ship gate](#verifying-changes-before-ship) is green but the iPhone still misbehaves, [`docs/2026-05-20-iphone-debugging.md`](docs/2026-05-20-iphone-debugging.md) walks through attaching PC DevTools to the live phone via `ios-webkit-debug-proxy`.

---

## TLS cert: regenerate every ~13 months

The leaf cert in `webapp/certificates/cert.pem` is capped at **396 days** because Apple/WebKit reject any server cert with a validity period > 398 days (since iOS 14), regardless of how thoroughly the issuing CA is trusted. After ~13 months, Safari will start showing "Not Secure" on the iPhone again — that is the leaf cert expiring, not a regression.

**Routine renewal (no iPhone re-trust needed):**

```powershell
.\.venv\Scripts\python.exe scripts\gen_ssl_cert.py --skip-install
# then: tray menu → 🔄 Restart webapp   (or restart tray.bat / webapp.bat)
```

The script reuses the existing `ca.pem` + `ca.key` if they exist, so the iPhone trust profile installed once stays valid. Only the leaf cert rotates. On the iPhone, force-quit Safari to clear its TLS cache and the next refresh is clean.

**Force a fresh CA (rarely — e.g. CA key compromise):**

```powershell
.\.venv\Scripts\python.exe scripts\gen_ssl_cert.py --force-new-ca
```

After this you **must** re-install the trust profile on every device: delete the old "Launcher Local CA" profile in *VPN & Device Management*, then repeat the one-time setup above.

**Troubleshooting "Not Secure" on iPhone despite the profile being installed:**

| Symptom check | Fix |
| --- | --- |
| Full Trust toggle is OFF in *Certificate Trust Settings* | toggle it ON (step 3 above) |
| Safari was already open before trust changed | force-quit Safari (step 4) |
| Leaf cert > 398 days — `openssl x509 -in webapp/certificates/cert.pem -noout -dates` | regenerate with the script above |
| Tailnet hostname or LAN IP changed — `openssl x509 -in webapp/certificates/cert.pem -noout -ext subjectAltName` doesn't list it | regenerate with the script above (it rescans hostnames + IPs) |
| Still rejected after all of the above | reboot the iPhone — iOS occasionally caches negative-trust decisions device-wide |

---

## Interactive terminal from the phone

Launching a Claude Code project in **full control** mode (the default — the ☁️ Detached toggle off) opens a **live terminal** — the same thing you'd see in the CMD window on the PC, streamed to the phone: real output, scrollback, typing, `Ctrl+C`, `/quit`, and image paste. Tap a `⚡ full control` session in the list to re-attach.

When the same session is also open on the PC (the mirror window), the **phone drives the terminal size** and the PC window mirrors it — one ConPTY has one size, so the phone is the single authority and the two never fight over dimensions.

**Terminal toolbar**

A small toolbar sits under the terminal for the things a phone keyboard can't do well:

- **⌨️ Keys** — a popover D-pad of arrow / `Esc` / `Tab` / `Enter` keys for iPhone keyboards (SwiftKey etc.) that lack them, so Claude's TUI prompts stay navigable (#36).
- **✏️ Compose** — toggles a slim predictive `<textarea>` above the keyboard. xterm.js wipes its own helper textarea on every keystroke, so iOS/Android autocomplete can't suggest there; the compose bar is a plain textarea, so it can. `➤` Send forwards the buffered text + `Enter` to the PTY in one frame. Hidden in the PC mirror window (#37).
- **📋 Paste** — reads the clipboard. Bar **closed**: sends straight to the PTY. Bar **open**: drops the text into the textarea at the caret for review before Send.
- **🖼 Image** — uploads a phone image. A terminal can't hold an image, so the file is saved on the PC and Claude is handed its **file path** (Claude reads the image from that path). Bar **closed**: the path is pasted straight into Claude's prompt. Bar **open** (`?inline=1`, #41): the session-host skips the paste and returns the path, which is inserted into the textarea — so several images + text can be composed and sent together.

**How it's wired**

- A separate long-lived **session-host** process (loopback-only, port `8446`) owns every `claude` ConPTY. The tray starts and owns it like it owns `cloudflared`. Because it's its own process, a *Restart webapp* doesn't kill running sessions (a PC reboot still does).
- The webapp proxies a WebSocket from the phone through to the session-host. The webapp is the single auth choke point.
- `xterm.js` renders the terminal in the SPA — no build step, vendored under `app/webapp/static/vendor/`.

**Security model — the terminal is not the same as the launcher**

Launching, listing, and stopping sessions stay public (bearer-token gated, reachable over the Cloudflare tunnel). The **live terminal itself does not**:

- **Tailscale-only.** The terminal WebSocket, image upload, and WebAuthn endpoints refuse any request that arrived over the public Cloudflare tunnel (they're rejected on the `Cf-Ray` header) and require a client IP in the Tailscale CGNAT range `100.64.0.0/10` (plus loopback, plus an optional `tailnet_allowlist`).
- **Passkey-gated.** When `webauthn_rp_id` + `webauthn_origin` are set, opening or driving a terminal requires a **WebAuthn platform passkey** — Face ID on the enrolled iPhone. A passkey assertion mints a short-lived (12 h) terminal token; the WebSocket and image endpoints require it.
- **Device whitelist you control.** Enrolled passkeys live in `config/webauthn_devices.json` (gitignored). Enrollment only works during a one-time window you open deliberately from the tray (**🔐 Enroll device** — 5 minutes). Revoke a device from **Settings → Terminal access**.
- **Audited.** Every terminal action is logged: `webapp/terminal_audit.log` (enroll / unlock / session lifecycle, device, client IP) and per-session `webapp/sessions/<id>.log` (input chunks, image uploads) + `<id>.transcript` (full output).

> `--dangerously-skip-permissions` is still always on. The marginal risk over your existing Tailscale remote access is small (anyone on the tailnet could already RDP in) — the passkey gate + audit log make this surface *more* controlled than plain remote access, not less.

**Enrolling your iPhone**

1. On the PC, set `webauthn_rp_id` (bare tailnet hostname, e.g. `pc.tailnet.ts.net`) and `webauthn_origin` (full origin, e.g. `https://pc.tailnet.ts.net:8445`) in `config/webapp_config.json`, and restart the webapp.
2. On the iPhone, open the launcher over the Tailscale URL.
3. On the PC, tray menu → **🔐 Enroll device (5 min)**.
4. On the iPhone, **Settings → Terminal access → 📲 Enroll this device** → Face ID.

After that, opening any session prompts Face ID once per 12 h.

**Terminal on the PC too.** With `claude_show_local_window: true` (the default), launching a session from the phone also opens an **interactive** terminal window for it on the PC. That window connects over loopback — so it bypasses the Tailscale + passkey gate — and because the session-host fans output to every connected client and accepts input from all of them, **you can type from the phone and the PC interchangeably**. Set it to `false` to launch silently.

---

## Auth

Two layers, both optional. With nothing configured, the API is open (fine on a private tailnet).

### Bearer token (`auth_token`)

```powershell
.\.venv\Scripts\python.exe scripts\gen_token.py            # first time
.\.venv\Scripts\python.exe scripts\gen_token.py --force    # rotate
.\.venv\Scripts\python.exe scripts\gen_token.py --clear    # disable
```

- Loopback callers still bypass.
- Remote (tailnet, Cloudflare) callers must present `Authorization: Bearer <token>` *or* `?token=…`.
- The tray menu's **Copy …** items bake the token into the copied URL automatically. Paste once on the phone, the page stashes it in `localStorage`, strips it from the visible URL, you're in.

### Login password (`auth_password`)

```powershell
.\.venv\Scripts\python.exe scripts\set_password.py <password>
.\.venv\Scripts\python.exe scripts\set_password.py --clear
```

Companion to the token. When set, a fresh device with no token in `localStorage` (e.g. an iOS PWA whose storage is partitioned from Safari) shows a login overlay. Type the password → server hands back the bearer token → page stashes it → equivalent to opening the tokenised URL once.

Failed attempts log to `webapp/auth.log` with client IP.

---

## Persistent URL via named Cloudflare tunnel

Use a named tunnel so the URL never changes:

```powershell
cloudflared tunnel login
cloudflared tunnel create launcher
cloudflared tunnel route dns launcher launcher.<your-domain>

copy webapp\cloudflared.sample.yml webapp\cloudflared.yml
REM ...then edit webapp\cloudflared.yml: tunnel UUID + hostname

.\webapp_tunnel_named.bat
```

Or do nothing — `tray.bat` reads the same `webapp/cloudflared.yml` and spawns cloudflared alongside the webapp automatically. The tunnel URL is written to `webapp/last_tunnel_url.txt` (with `?token=…` appended when `auth_token` is set).

> **Combine with Cloudflare Access.** Add an Access policy on the hostname so only your email/IdP gets past Cloudflare's edge, then the bearer token is a *second* factor on the API itself.

---

## Layout

```
app-launcher/
├── launcher.py                # thin entry point — sys.path shim → app/cli/main
├── webapp.bat / tray.bat      # the two day-to-day .bat entrypoints
├── webapp_tunnel_named.bat    # uvicorn + cloudflared (named tunnel)
├── setup.bat                  # one-shot fresh-clone installer
│
├── app/
│   ├── cli/                   # argparse dispatcher: tray | webapp | scan | session-host
│   ├── tray/                  # pystray icon — owns webapp + cloudflared + session-host
│   ├── session_host/          # loopback PTY host — owns every claude ConPTY
│   └── webapp/
│       ├── server.py          # FastAPI routes + Tailscale gating + WS proxy
│       ├── manager.py         # adopt-or-spawn uvicorn lifecycle
│       └── static/            # SPA shell + PWA manifest + icons + vendored xterm.js
│
├── src/                       # logic layer (no UI imports)
│   ├── app_config.py          # log level, webapp embed section
│   ├── webapp_config.py       # host/port/scan-paths/claude flags/secrets/terminal knobs
│   ├── registry.py            # unified app registry (load/save/scan/mutate)
│   ├── scanner.py             # bat classifier + claude-code project discovery
│   ├── bat_generator.py       # workspace ↔ remote.bat sync
│   ├── launcher.py            # spawn_bat / spawn_claude_session helpers
│   ├── session_host.py        # PtySession + RemoteSession + SessionManager (ConPTY via pywinpty)
│   ├── session_client.py      # webapp → session-host loopback HTTP client
│   ├── webauthn_gate.py       # passkey enrollment / assertion + terminal tokens
│   ├── audit.py               # terminal audit + per-session logs
│   └── diagnostics.py         # log ring buffer + port-owner introspection
│
├── scripts/
│   ├── gen_icons.py           # rocket silhouette PWA icons
│   ├── gen_ssl_cert.py        # self-signed CA + leaf + iOS .mobileconfig
│   ├── gen_token.py           # bearer token rotate / clear
│   ├── set_password.py        # login password set / clear
│   └── run_named_tunnel.py    # uvicorn + cloudflared (headless)
│
├── config/                    # *.sample.json committed, real files gitignored
│   ├── config.sample.json
│   ├── webapp_config.sample.json
│   └── apps.sample.json
│
└── webapp/                    # runtime state — all gitignored except samples
    ├── certificates/          # ca.pem / cert.pem / key.pem from gen_ssl_cert
    ├── cloudflared.sample.yml
    ├── cloudflared.yml        # your filled-in copy (gitignored)
    ├── last_tunnel_url.txt    # tray + run_named_tunnel write here
    └── auth.log               # failed-login audit
```

---

## Config

Two committed JSON templates; real files are gitignored.

### `config/config.json`

Cross-surface settings (read by tray, CLI, server):

```json
{
  "log_level": "INFO",
  "tailnet_host": "pc.example-tailnet.ts.net",
  "webapp": {
    "enabled": true,
    "host": "0.0.0.0",
    "port": 8445
  }
}
```

`tailnet_host` is the Tailscale (MagicDNS) hostname of this PC. The Apps
tab's **Running apps** section uses it to build each launched app's
remote URL (`<scheme>://<tailnet_host>:<port>/`) so you can tap **🌐 Open**
from the phone and land on the app. The scheme is auto-detected per app
(a TLS probe of the bound port — `https` for the FastAPI siblings,
`http` for a plain Streamlit server). Leave it empty (`""`) to disable
the feature — the Open button is then shown disabled with a hover hint.

### `config/webapp_config.json`

UI prefs + secrets, authored from the web UI:

| Key | Default | What it controls |
|---|---|---|
| `host` | `"0.0.0.0"` | uvicorn bind host |
| `port` | `8445` | uvicorn bind port |
| `projects_dir` | parent of this repo | Where the Claude Code tab scans for `.code-workspace` + `*-remote.bat` |
| `apps_scan_root` | parent of this repo | Where the Apps tab scans recursively for `*.bat` |
| `claude_model` | `"opus"` | Default `--model` for `claude` |
| `claude_effort` | `"high"` | Default `--effort` (use `"off"` to omit the flag) |
| `claude_verbose` | `true` | Pass `--verbose` |
| `claude_debug` | `false` | Pass `--debug` |
| `auth_token` | `""` | Bearer token. Empty = gate off. |
| `auth_password` | `""` | Optional companion for `/api/login`. |
| `session_host_port` | `8446` | Loopback port the PTY session-host binds. Never network-reachable; must differ from `port`. |
| `tailnet_allowlist` | `[]` | Extra IPs / CIDRs allowed to reach the terminal endpoints, on top of loopback + `100.64.0.0/10`. |
| `claude_show_local_window` | `true` | Open an interactive terminal window on the PC when a session is launched from the phone. |
| `webauthn_rp_id` | `""` | Passkey relying-party ID — the bare tailnet hostname. Empty disables the passkey gate. |
| `webauthn_rp_name` | `"Launcher"` | Display name shown in the passkey prompt. |
| `webauthn_origin` | `""` | Full https origin the phone connects to (scheme + host + port). |

`--remote-control` and `--dangerously-skip-permissions` are **always** added — that's the whole point of the remote tab.

### `config/apps.json`

Unified registry. Each row:

```json
{ "id": "...", "name": "...", "kind": "claude-code | streamlit | webapp | tunnel",
  "bat_path": "...",      // for non-claude-code kinds
  "project_dir": "...",   // for claude-code kind
  "added_at": "2026-..." }
```

Scan flow: tap **🔎** in the header → `/api/apps/scan` returns a diff → checklist dialog → submit selections → `/api/apps/save` persists.

---

## Auto-start at log on with Task Scheduler

1. Open **Task Scheduler** → **Create Task…** (not Basic).
2. **General**: name `Launcher`, **Run only when user is logged on** ✅ (required for visible CMD windows), Configure for Windows 10/11.
3. **Triggers** → New: At log on, delay 30 s.
4. **Actions** → New: Start a program → `E:\automation\app-launcher\tray.bat`, Start in `E:\automation\app-launcher`.
5. **Conditions**: uncheck "Start only if on AC power".
6. **Settings**: Allow on-demand ✅, restart on failure every 1 min × 3, "If already running: do not start a new instance".

To test without a reboot: select the task → **Run** in the right-hand pane.

---

## Security notes

- Tailscale already gates network access; the bearer token + password add a second factor in case a tailnet device is compromised.
- **The interactive terminal is gated harder than the rest of the app.** It is Tailscale-only (refused over the Cloudflare tunnel) and, when WebAuthn is configured, requires a platform passkey on an enrolled device. The enrolled-device whitelist (`config/webauthn_devices.json`) is yours to maintain; every terminal action is audited. See [Interactive terminal](#interactive-terminal-from-the-phone).
- The session-host binds `127.0.0.1` only — the PTYs are never directly reachable; the webapp is the sole way in.
- The launcher only ever runs bats from the registered list (id is checked against `config/apps.json`) or `claude` in a registered project_dir — it can't be coerced into running an arbitrary path.
- The smart-kill endpoint accepts any port in range but only acts on PIDs LISTENing on that port — a port no one is using is a no-op.
- Self-signed TLS is for loopback + tailnet. Cloudflare terminates public TLS at the edge; the tunnel handshake to uvicorn uses `noTLSVerify: true` because the origin cert is intentionally not publicly trusted.

---

## Verify

```powershell
& .\.venv\Scripts\python.exe -m py_compile launcher.py
& .\.venv\Scripts\python.exe -m uvicorn app.webapp.server:app --host 127.0.0.1 --port 8445
# then in another terminal:
curl http://127.0.0.1:8445/healthz
```

### Pytest API tests

In-process FastAPI `TestClient` suite under `tests/` (the sister-project pattern). 37 tests across 6 files covering `/healthz`, `/api/config` (GET + POST allow-list), `/api/login` + bearer-token gate, `/api/apps` CRUD, `/api/claude-code/generate`, and `/api/claude-code/sessions` (list + stop). Session-host loopback client is mocked — no live tray, no port :8446 needed.

```powershell
& .\.venv\Scripts\python.exe -m pytest tests -m "not smoke" -v
```

Runs in about a second. The `-m "not smoke"` flag excludes the live-tray Playwright suite below.

### Playwright smoke + regression tests

A `pytest-playwright` suite under `tests/e2e/` covers two things:

- **Boot smoke** (`test_smoke.py`) — JS error on boot, empty config form, broken tab switch, missing stop buttons per session kind, missing login overlay.
- **iPhone regression net** — one focused test per closed iOS-only bite, so the next regression of any of them surfaces locally before a deploy instead of after an hour of phone-PC round-trips:

  | File | Pins fix from | What would regress without it |
  | --- | --- | --- |
  | `test_cache_busting.py` | `35caad4` + `bf76d0d` (#30) | `?v=<hash>` stamps in served `index.html` diverge from on-disk asset bytes (forgot to restart tray after editing JS) |
  | `test_iphone_revalidate.py` | `696b723` | iOS Safari serves stale `index.html` and references a `?v=<old>` script that no longer exists → empty Model/Effort controls |
  | `test_terminal_reconnect.py` | `142e2b4` (#28) | Live terminal WS drops on iOS suspend, overlay sticks on "Disconnected." until manual re-open |
  | `test_paste_button.py` | (#29) | 📋 paste button in iOS PWA reaches `navigator.clipboard.readText()` but bytes never arrive at the session-host |
  | `test_ports_probe.py` | `d564114` | Pywinpty's loopback ephemerals leak into the Running-apps panel under bogus high ports |
  | `test_edge_mirror_close.py` | `b946bc8` (#20) | `terminal.js` stops marking the mirror page with `document.title = 'app-launcher-mirror-<sid>'`, EnumWindows can't find the HWND, Stop & Close leaves the Edge `--app` window hanging |
  | `test_viewport.py` | (#31) | WebKit projection silently loses the iPhone 15 Pro Max descriptor — the whole projection becomes desktop-shaped and the table above stops catching iOS bugs |
  | `test_terminal_native_scroll.py` | (#23) | `.xterm-screen` stops being `pointer-events:none`, so touches no longer fall through to `.xterm-viewport` and the phone loses iOS native momentum scrolling |
  | `test_keys_popover.py` | (#36) | `⌨️` popover stops sending arrow/Esc/Tab/Enter escape sequences over the WS, so iPhone keyboards without those keys can't drive Claude's TUI prompts |
  | `test_compose_bar.py` | (#37, #41) | `✏️` compose bar's `➤` Send stops forwarding `<text>\r` to the PTY, the bar leaks into the PC mirror window, or `🖼` stops dropping the uploaded image path into the bar when it's open |

Every test runs in **two projections** — Chromium-desktop and WebKit on an iPhone 15 Pro Max viewport — so engine-specific iOS bugs get caught on Windows before they reach a real phone. A few tests skip on the duplicate projection where the check is browser-agnostic (server-side header inspection, etc.). Pin a single engine with `--browser chromium` (or `webkit`) for a faster dev loop.

One-time setup:

```powershell
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
& .\.venv\Scripts\python.exe -m playwright install chromium webkit
```

**Run after every webapp/SPA edit** with the tray up (`tray.bat`):

```powershell
.\scripts\run-e2e.ps1                       # both projections, ~60 s
.\scripts\run-e2e.ps1 --browser chromium    # Chromium-only, ~15 s — dev loop
# or directly:
& .\.venv\Scripts\python.exe -m pytest -m smoke -v tests/e2e
```

The suite runs against the live tray on `https://127.0.0.1:8445` — it does not boot anything itself. If the tray isn't up, every test is skipped with a clear message instead of hanging. Loopback access auto-bypasses the bearer-token middleware and the passkey gate, so no credentials are needed.

The terminal-related regression tests (reconnect, paste, mirror-close) launch a real `claude` PTY via the `launched_pty_session` fixture and force-kill it in teardown — they don't require any test-only product hooks (no `LAUNCHER_TEST_HOOKS=1` env var). The WebSocket-drop probe and the clipboard mock are injected via `page.add_init_script` from inside each test, so the production surface is untouched.

### Verifying changes before ship

`run-e2e.ps1` above is the dev loop — fast, but it *skips* the whole e2e suite if the tray isn't up, which is the wrong default for a final check (a forgotten tray looks like a green run). The pre-ship gate closes that hole:

```powershell
pwsh -File scripts\verify-before-ship.ps1
```

It runs the full pipeline as one pass/fail — byte-compile (`app`, `src`, `tests`), the non-e2e pytest suite, then the Playwright e2e suite on both projections — and **boots its own disposable webapp + session-host** on a free port, so it never silently skips:

- A tray on `:8445` may be running or not. Autoboot picks a free port for its webapp and adopts the tray's session-host on `:8446` if one is up, otherwise spawns its own. The existing tray is left untouched.
- The disposable instance serves HTTPS reusing `webapp/certificates/` (plain HTTP if no cert pair exists). Subprocess output is captured to `webapp/e2e-autoboot-*.log`.
- It exits non-zero on the first failure and prints total wall time (~20–40 s typical).

Run it before declaring any change to `app/webapp/`, `src/launcher.py`, or `src/session_host*.py` done. The same autoboot path is available to a plain pytest run with `--e2e-autoboot` (or `LAUNCHER_E2E_AUTOBOOT=1`).

The same gate also runs on CI (`.github/workflows/e2e.yml`, `windows-latest`) on every push to a non-`main` branch and on pull requests into `main` — so the gate runs without relying on remembering to. The local `verify-before-ship.ps1` stays the contract; CI is supplementary. Note: `claude` is not on PATH on the runner, so the terminal-regression tests (reconnect, paste, mirror-close) launched via the `launched_pty_session` fixture skip cleanly there — expected, not a failure.

---

## Files

- `launcher.py` — argparse → `tray` | `webapp` | `scan` | `session-host`
- `app/webapp/server.py` — FastAPI server + all `/api/*` routes, Tailscale gating, WS proxy
- `app/webapp/manager.py` — adopt-or-spawn uvicorn for the tray
- `app/session_host/server.py` — loopback PTY host (HTTP + WebSocket)
- `app/tray/tray.py` — pystray icon + cloudflared + session-host lifecycle
- `src/session_host.py` — `PtySession` + `SessionManager` (ConPTY via pywinpty)
- `src/session_client.py` — webapp → session-host loopback client
- `src/webauthn_gate.py` — passkey enrollment / assertion + terminal tokens
- `src/audit.py` — terminal audit + per-session logs/transcripts
- `src/registry.py` — unified apps registry
- `src/scanner.py` — bat classifier + Claude-Code project discovery
- `src/bat_generator.py` — workspace ↔ `*-remote.bat` sync
- `src/webapp_config.py` — persisted UI prefs + auth secrets + terminal knobs
- `scripts/gen_*.py` — token / password / icons / SSL cert / tunnel
- `config/*.sample.json` — committed templates; real files are gitignored
- `webapp/` — runtime state (certs, tunnel URL, audit logs, per-session logs)
