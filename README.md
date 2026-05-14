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

- **Claude Code** — every `.code-workspace` and orphan `*-remote.bat` in your projects directory becomes a button. Tap → fresh CMD window runs `claude` with your saved flags in that project folder. Flags (model / effort / verbose / debug) are a one-tap panel above the project list.
- **Apps** — every `*.bat` under your scan root that the classifier recognises as Streamlit, a FastAPI webapp, or a Cloudflare-tunnel script. Tap → fresh CMD window runs the bat. Tunnel rows surface a live `📡 <url>` under the launch button, refreshed every 4 s.

Both tabs share **one** registry file: `config/apps.json`. The header **🔎 Scan** button walks both scan paths, shows what's new in a checklist, and saves selections.

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

---

## Phone install (PWA)

1. Open `https://<pc-hostname>:8445/install-ca` once on iPhone (Safari) → install the trust profile in Settings → General → VPN & Device Management → enable in Settings → General → About → Certificate Trust Settings.
2. Open the home URL → Safari **Share → Add to Home Screen**. The launcher rocket icon lands on your home screen.
3. On Android, Chrome shows an "Install app" prompt the second visit. The icon goes on the home screen the same way.

After that the launcher behaves like a native app — full-screen, no Safari chrome.

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
│   ├── cli/                   # argparse dispatcher: tray | webapp | scan
│   ├── tray/                  # pystray icon — owns webapp + cloudflared
│   └── webapp/
│       ├── server.py          # FastAPI routes
│       ├── manager.py         # adopt-or-spawn uvicorn lifecycle
│       └── static/            # SPA shell + PWA manifest + icons + CA install
│
├── src/                       # logic layer (no UI imports)
│   ├── app_config.py          # log level, webapp embed section
│   ├── webapp_config.py       # host/port/scan-paths/claude flags/secrets
│   ├── registry.py            # unified app registry (load/save/scan/mutate)
│   ├── scanner.py             # bat classifier + claude-code project discovery
│   ├── bat_generator.py       # workspace ↔ remote.bat sync
│   ├── launcher.py            # spawn_bat / spawn_claude helpers
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
  "webapp": {
    "enabled": true,
    "host": "0.0.0.0",
    "port": 8445
  }
}
```

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

---

## Files

- `launcher.py` — argparse → `tray` | `webapp` | `scan`
- `app/webapp/server.py` — FastAPI server + all `/api/*` routes
- `app/webapp/manager.py` — adopt-or-spawn uvicorn for the tray
- `app/tray/tray.py` — pystray icon + cloudflared lifecycle
- `src/registry.py` — unified apps registry
- `src/scanner.py` — bat classifier + Claude-Code project discovery
- `src/bat_generator.py` — workspace ↔ `*-remote.bat` sync
- `src/webapp_config.py` — persisted UI prefs + auth secrets
- `scripts/gen_*.py` — token / password / icons / SSL cert / tunnel
- `config/*.sample.json` — committed templates; real files are gitignored
- `webapp/` — runtime state (certs, tunnel URL, audit log)
