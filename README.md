# 🚀 Launcher

Phone-first launcher hub. One tap on your phone → the home PC either:

- runs a coding agent — **Claude Code**, **Codex CLI**, **Antigravity CLI**, **GitHub Copilot CLI**, **Pi**, or **Grok Build** — in a project folder (**Coding** tab),
- spawns any registered Streamlit / FastAPI launcher (**Apps** tab),
- fires a one-shot Python script or scheduled job (**Jobs** tab — same trigger surface the Stream Deck and Task Scheduler use), or
- invokes a [`life-os`](https://github.com/ferraroroberto/life-os) productivity skill and browses what it knows about you (**Life OS** tab).

Sister project to [`photo-ocr`](https://github.com/ferraroroberto/photo-ocr) and [`voice-transcriber`](https://github.com/ferraroroberto/voice-transcriber) — same FastAPI + SPA + PWA + Cloudflare-tunnel stack, but for kicking off other processes instead of doing work itself.

> Three ways to reach it from your phone:
> - **Local** (same Wi-Fi): `https://<pc-hostname>:8445`
> - **Tailscale** (anywhere): `https://<pc>.<tailnet>.ts.net:8445`
> - **Cloudflare named tunnel**: `https://launcher.<your-domain>` (no tailnet required)

---

## What it does, in one screen

Five tabs, plus **Settings** behind the **⚙ gear** in every page header:

- **Coding** — launch a coding agent (Claude Code, Codex CLI, Antigravity CLI, GitHub Copilot CLI, Pi or Grok Build) in a project folder, as a live terminal or detached. The tab also carries the Sessions list, the Usage card, per-project git state, favorites and the project menu; a running session opens in the **Terminal ⇄ Chat** session view.
- **Apps** — the registered Streamlit / FastAPI / tunnel / tray launchers, plus a Port-listeners panel with one-tap smart-kill.
- **Jobs** — one-shot Python scripts and scheduled jobs, fired from the phone, the Stream Deck or Task Scheduler through one shared trigger surface.
- **Life OS** — one row per [`life-os`](https://github.com/ferraroroberto/life-os) skill, its conversations, the weekly recap and Telegram channels.
- **Board** — a read-only kanban of what needs you now across the fleet.

Every behaviour, per tab, is in [docs/features.md](docs/features.md); the deep references are [Session view](docs/session-view.md), [Jobs tab](docs/jobs-tab.md) and [Board](docs/board.md).

---

## Install

```powershell
cd app-launcher
.\setup.bat
```

That creates `.venv`, installs deps, and generates the PWA icons. After this runs once, `tray.bat` is enough for day-to-day use.

**The icon step needs a `project-scaffolding` checkout beside this repo** (or `PROJECT_SCAFFOLDING_DIR` pointing at one): `scripts/gen_icons.py` is a thin caller onto that repo's shared `brand_gen.py`, and `setup.bat` propagates its failure, so without it the install stops at step 3. The generated icons are **committed**, so on a fresh clone that step is only re-running what is already in the tree — if you don't have the sibling checkout, run the first two steps by hand (`python -m venv .venv` then `.venv\Scripts\python.exe -m pip install -r requirements.txt`) and skip it.

If you came from the old `automation\launcher\` Flask version, your apps list and Claude flags survive — copy `automation\launcher\apps_config.json` → `app-launcher\config\apps.json` and `automation\launcher\config.json`'s contents into `app-launcher\config\webapp_config.json` under the matching `claude_*` keys.


Each coding agent has its own CLI to install (Codex, Antigravity, Copilot, Pi, Grok Build): [docs/installing-agent-clis.md](docs/installing-agent-clis.md).

---

## Run

```powershell
.\tray.bat           # tray icon + webapp (normal use, no console window)
.\webapp.bat         # uvicorn standalone, no tray (dev / headless)
```

Both bind `0.0.0.0:8445`. If `webapp/certificates/cert.pem` is present, the server is HTTPS — otherwise plain HTTP (fine for a fresh loopback-only clone). The cert pair is written by `scripts/gen_tailscale_cert.py` — a real Let's Encrypt cert for the tailnet name, zero per-device trust; see [HTTPS certificate](#https-certificate-tailscale).

The tray icon menu has:

- **🚀 Open launcher** — open the local URL in the default browser
- **📋 Copy local URL** — clipboard the loopback URL with `?token=…` baked in
- **📋 Copy Tailscale URL** — clipboard `https://<host>.<tailnet>.ts.net:8445?token=…`
- **📋 Copy Cloudflare URL** — clipboard the public tunnel URL with `?token=…`
- **🔄 Restart webapp** — pick up code changes without losing the tunnel
- **ℹ️ Status** — quick popup with running state + base URL


The build line, `/api/version`, the health watchdog and the request logs are in [docs/run-troubleshooting.md](docs/run-troubleshooting.md).

---

## Phone install (PWA)

The launcher is a PWA — installs to the iPhone home screen, full-screen, no Safari chrome. With the [Tailscale cert](#https-certificate-tailscale) there is no trust setup at all:

1. Open `https://<host>.<tailnet>.ts.net:8445?token=…` in Safari (tray menu → **📋 Copy Tailscale URL**). Lock icon should be solid, no "Not Secure".
2. **Share → Add to Home Screen**. The launcher rocket icon lands on your home screen.

On Android, Chrome shows an "Install app" prompt the second visit; the icon goes on the home screen the same way.

After that the launcher behaves like a native app — full-screen, no Safari chrome.

> **Debugging the phone:** when the [pre-ship gate](docs/verification.md#verifying-changes-before-ship) is green but the iPhone still misbehaves, [`docs/iphone-debugging.md`](docs/iphone-debugging.md) walks through attaching PC DevTools to the live phone via `ios-webkit-debug-proxy`.

---

## HTTPS certificate (Tailscale)

Fleet standard (set in the `ferraroroberto/project-scaffolding` repo). Provision a **real Let's Encrypt cert** via `tailscale cert` — no self-signed CA, no per-device trust dance (the legacy self-signed generator and its `/install-ca` iOS-profile detour no longer exist):

```powershell
.\.venv\Scripts\python.exe scripts\gen_tailscale_cert.py
# then: tray.bat --restart
```

One-time prereq: enable **DNS → HTTPS Certificates** in the [Tailscale admin console](https://login.tailscale.com/admin/dns). The script auto-detects the MagicDNS name and writes `webapp/certificates/cert.pem` + `key.pem`. Every device on the tailnet then trusts `https://<host>.<tailnet>.ts.net:8445` natively — no CA install, no profile, no Certificate Trust toggle.

**Renewal is automatic.** The LE leaf lives ~90 days, so every uvicorn-boot path (`tray.bat` via the webapp manager, `webapp.bat`, `run_named_tunnel.py`) runs `gen_tailscale_cert.py --check` first, which renews only a `.ts.net` cert expiring within 30 days and no-ops on any other cert. No calendar entry needed.

> **Loopback and LAN URLs:** the Tailscale cert is issued *only* for the ts.net name, so `https://127.0.0.1:8445` and LAN-IP URLs show a hostname-mismatch warning by design — open the launcher via the ts.net URL on the PC too. The **PC mirror windows adapt automatically**: with a Tailscale cert active they open the ts.net URL carrying their own credentials (`?token=` bearer bootstrap + a server-minted `?tt=` terminal token when the passkey gate is configured); with no Tailscale cert they keep the loopback URL and its auth bypass. The Cloudflare tunnel (`noTLSVerify`) and the e2e suite are unaffected either way. With no cert at all the server runs plain HTTP on loopback — fine for a fresh clone, but iOS Safari needs HTTPS for the PWA + mic features, so provision the Tailscale cert before phone use.

---

## Interactive terminal from the phone

A full-control Coding-tab launch opens a **live terminal** streamed to the phone: real output, scrollback, typing, `Ctrl+C` and image paste, for any coding agent. The terminal is gated harder than the rest of the app (Tailscale-only). The socket, key bar, composer and security model are in [docs/interactive-terminal.md](docs/interactive-terminal.md); the full-screen overlay around it is the [session view](docs/session-view.md).

---

## Screen view from the phone

A live view of the PC's screen that you can also click, drag, scroll and type into: tap the CPU reading in any page header, then the **Screen** row of the PC load sheet. Displays, Control and the rest: [docs/screen-view.md](docs/screen-view.md).

---

## Auth

Two layers, both optional. With nothing configured, the API is open (fine on a private tailnet).

### Bearer token (`auth_token`)

```powershell
.\.venv\Scripts\python.exe scripts\gen_token.py            # first time
.\.venv\Scripts\python.exe scripts\gen_token.py --force    # rotate
.\.venv\Scripts\python.exe scripts\gen_token.py --clear    # disable
.\.venv\Scripts\python.exe scripts\gen_token.py --show     # also echo the value
```

- The generated value is written to `config/webapp_config.json` and is **not** printed unless you pass `--show`. The tray bakes it into the URL it copies (`Tray → Copy Cloudflare URL`), so the normal flow never needs to read it off the console — and a run inside a launcher PTY session has its whole stdout captured to a transcript kept for `session_retention_days`.

- Loopback callers still bypass — *unless* the request carries Cloudflare's own edge headers. cloudflared runs on this PC and dials the webapp over loopback, so a tunnelled request only looks remote once uvicorn rewrites the client address from `X-Forwarded-For`; the edge headers are the signal that survives however that address resolves.
- Remote (tailnet, Cloudflare) callers must present `Authorization: Bearer <token>` *or* `?token=…`. This includes the terminal WebSocket, which re-applies the same gate by hand (Starlette middleware never sees a WS handshake) — a full-scope minted token works there exactly as it does on HTTP, and a job-scoped one is refused there exactly as it is on HTTP.
- The tray menu's **Copy …** items bake the token into the copied URL automatically. Paste once on the phone, the page stashes it in `localStorage`, strips it from the visible URL, you're in.
- **Settings → API tokens** mints additional *job-scoped* bearer tokens: each can only fire its chosen Jobs-tab job (`POST /api/jobs/<id>/run`) and is rejected everywhere else, so the URL baked into a Stream Deck button no longer carries full-SPA access. The raw token is shown once at mint; revoke + re-mint to rotate without touching `auth_token`. See `docs/jobs-tab.md` → "Scoped API tokens".
- **Settings → API tokens → Delegate token** (off by default) is a credential for an outside agent: it can read the Board and the chief's plan and file issues for the chief to triage, and is refused everywhere else. See [docs/config.md](docs/config.md#the-delegate-token).

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

The annotated repo tree (entry point, `app/`, `src/`, `scripts/`, `config/`, `tests/`, `docs/`): [docs/layout.md](docs/layout.md). The structural diagram is [docs/architecture.mmd](docs/architecture.mmd).

---

## Config

Two committed JSON templates; real files are gitignored. `config/config.json`, `config/webapp_config.json`, the model catalogs, `config/apps.json` and the registered trays are documented in [docs/config.md](docs/config.md).

---

## Auto-start at log on

Toggle **Start app-launcher at log on** in **Settings** (any page header's ⚙ gear) — it writes a
tiny wrapper bat (`AppLauncher.bat`, calling `tray.bat`) into your Windows
Startup folder (`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup`).
No admin rights needed: it's a plain file write under your own profile, the
same mechanism most other auto-starting desktop apps use. Untoggling removes
the file. `tray.bat` is idempotent, so a Startup-folder run racing an
already-running tray is a safe no-op.

(A Task Scheduler "At log on" trigger was tried first and reverted —
`schtasks /Create /SC ONLOGON` returns Access Denied from the launcher's own
unelevated process, the same reason `elevated` Jobs already require a manual
elevated-shell registration. The Startup folder needs no such privilege.)

### Manual fallback (Task Scheduler)

If you'd rather use Task Scheduler directly (e.g. to add a startup delay or
restart-on-failure policy the Startup-folder mechanism can't express):

1. Open **Task Scheduler** → **Create Task…** (not Basic).
2. **General**: name `Launcher`, **Run only when user is logged on** ✅ (required for visible CMD windows), Configure for Windows 10/11.
3. **Triggers** → New: At log on, delay 30 s.
4. **Actions** → New: Start a program → `E:\automation\app-launcher\tray.bat`, Start in `E:\automation\app-launcher`.
5. **Conditions**: uncheck "Start only if on AC power".
6. **Settings**: Allow on-demand ✅, restart on failure every 1 min × 3, "If already running: do not start a new instance".

To test without a reboot: select the task → **Run** in the right-hand pane.

Note: creating this task requires an elevated (Run as administrator) Task
Scheduler / PowerShell session — the same constraint that keeps the
Settings-tab toggle from using this mechanism itself.

---

## Security notes

- Tailscale already gates network access; the bearer token + password add a second factor in case a tailnet device is compromised.
- **The interactive terminal is gated harder than the rest of the app.** It is Tailscale-only (refused over the Cloudflare tunnel) and, when WebAuthn is configured, requires a platform passkey on an enrolled device. The enrolled-device whitelist (`config/webauthn_devices.json`) is yours to maintain; every terminal action is audited. See [Interactive terminal](#interactive-terminal-from-the-phone).
- The session-host binds `127.0.0.1` only — the PTYs are never directly reachable; the webapp is the sole way in.
- The launcher only ever runs bats from the registered list (id is checked against `config/apps.json`) or `claude` in a registered project_dir — it can't be coerced into running an arbitrary path.
- The smart-kill endpoint accepts any port in range but only acts on PIDs LISTENing on that port — a port no one is using is a no-op.
- Local TLS is the Tailscale LE cert, issued for the ts.net name only. Cloudflare terminates public TLS at the edge; the tunnel handshake to uvicorn uses `noTLSVerify: true` because the origin cert doesn't cover the public hostname.

## Usage log

The app records which tabs and main controls you open, and on which device class, so a design review reads real frequency instead of asking. It is the vendored `usage-log` component (`src/usage_log.py` plus `app/webapp/static/_vendored/usage-log/`, both declared in `.fleet.toml`). Events land in `usage.sqlite3` under the app's runtime-data directory (`<runtime root>/app-launcher/`, `APP_LAUNCHER_DATA_DIR` overrides it), outside git, on this machine only; nothing is sent to any other origin.

What is stored, and nothing else: event kind (`session_start`, `view`, `tap`, `session_end`), a view id (a tab name, or `settings`), a control id, a random per-visit session id, device class (phone / tablet / desktop), OS class, and a timestamp. Never message text, names, queries, form values, URLs with query strings, IP addresses or the raw user-agent; the server drops anything outside the bounded id pattern. Control ids are constants written as `data-track="…"` in `index.html`; no script builds one from element text. `GET /api/usage/summary?days=30` reads the counts back. The route sits behind the bearer token like every other `/api` route; `static/usage.js` hands the vendored client the bearer header through its `headers` option, a function so a rotated token is read on every send (without it a phone's events would 401 silently and only the PC would ever be logged); nothing wraps `window.fetch` or `navigator.sendBeacon`. Rows older than 365 days are pruned. The file needs the same backup treatment as other runtime SQLite state; losing it loses history, not function.

## Real-device timings

Every page load and every `/api/` request slower than 800 ms on the real device writes one timing row, so a slow moment can be traced to the connection, the server or the page; `/api` responses carry `Server-Timing: app;dur=<ms>` to split server from network. Rows hold route templates and milliseconds only, in `perf.sqlite3` beside the usage log, pruned at 90 days. Read them with `launcher.py perf-summary [--days N] [--json]` or `GET /api/perf/summary?days=N`: [docs/perf-timings.md](docs/perf-timings.md).

## Screen time to task-os

The Screen time numbers go to task-os's Time tracker as **one duration-only entry per completed local day**, all devices merged (a minute on the phone and the PC at once counts once), with the device split in the entry's note (`Launcher screen time: PC 2 h 50 m, Phone 1 h 10 m`). Each day is keyed `usage:app-launcher:<YYYY-MM-DD>` through task-os's idempotent `PUT /api/time/entries/by-source/{source_id}`, so a resend replaces the day instead of duplicating it; a day that now rounds to 0 minutes is a `DELETE` of its key. Today is never sent, since it is still running. The code is `src/usage/export.py`; nothing new is recorded.

- **Command:** `launcher.py usage-export [--since YYYY-MM-DD] [--dry-run]`. Bare, it resends the last 7 completed days, which absorbs late events and rule changes; `--since` covers every day from that date to yesterday (the backfill); `--dry-run` prints the per-day minutes and sends nothing, needing no config. Exit codes name the failure: `2` not set up, `3` category missing, `4` task-os unreachable, `5` task-os refused the data, `6` no usage log yet.
- **On demand:** the Screen time sheet's **Send to task-os** (`POST /api/screen-time/export`, the same code). `GET /api/screen-time/export` reads the last success and the last failure, kept in `screen-time-export.json` in the runtime-data folder (dates, counts and the error message only).
- **Nightly:** a Job running `launcher.py usage-export`; the sample is `screen-time-export` in `config/jobs.sample.json` (daily 03:30, alert on failure).
- **Config** (`config/webapp_config.json`): `task_os_url` (default `https://127.0.0.1:8448`), `task_os_person` (a task-os person id, from its `time.people`; empty keeps the export off), and the category as `task_os_area` / `task_os_category` slugs (default `work` / `launcher`), resolved through task-os's `GET /api/time/meta` on every send. The category must exist first: create **Work › Launcher** in task-os's Time tab (Settings › Tools › Time categories). A missing or archived one is its own error, never created from here.
- **First run:** `usage-export --since <first day> --dry-run`, check the table against the sheet, then the same command without `--dry-run`.

The pre-ship gate's disposable webapp gets an empty `task_os_url`, so no test can reach the live task-os.

## Life OS zombie cleanup

A nightly Job finds Life OS skill sessions opened by mistake and removes them, so the history stops filling up with empty conversations: [docs/life-os-zombie-cleanup.md](docs/life-os-zombie-cleanup.md).

---

## Verify

```powershell
& .\.venv\Scripts\python.exe -m py_compile launcher.py
& .\.venv\Scripts\python.exe -m uvicorn app.webapp.server:app --host 127.0.0.1 --port 8445 --loop app.webapp.event_loop:selector_loop_factory
# then in another terminal:
curl http://127.0.0.1:8445/healthz
```


Before declaring a webapp, launcher or session-host change done, run the pre-ship gate:

```powershell
pwsh -File scripts\verify-before-ship.ps1 > gate.log 2>&1; echo "exit=$?"
```

The gate also runs a static ratchet (new duplicate blocks and unused imports fail against a committed baseline; it needs Node for `npx` and the `ruff` from `requirements.txt`). A structure ratchet keeps files, functions and directories from growing and the root folders on the declared shape ([docs/layout.md](docs/layout.md#structure-ratchet)), and an instruction ratchet does the same for lint hits in `CLAUDE.md`, `AGENTS.md` and the skills, and a doc-refs ratchet keeps `README.md`, `CLAUDE.md` and `docs/` from naming a path, module, link or port that does not exist, and a dead-code ratchet keeps a function, class or method that nothing refers to from appearing. Read `gate.log`; it ends in `GATE-RESULT: PASS` or `GATE-RESULT: FAIL`, and a missing line is not a pass. Never pipe the gate: a pipeline reports its last command's exit code. The pytest API tests, the Playwright suite, the design review and what the gate does step by step: [docs/verification.md](docs/verification.md).

---

## Documentation

| Page | What it covers |
|---|---|
| [docs/features.md](docs/features.md) | Tab-by-tab feature reference |
| [docs/session-view.md](docs/session-view.md) | The Terminal ⇄ Chat session overlay |
| [docs/interactive-terminal.md](docs/interactive-terminal.md) | The live terminal, key bar, composer and its security model |
| [docs/screen-view.md](docs/screen-view.md) | Viewing and controlling the PC screen |
| [docs/jobs-tab.md](docs/jobs-tab.md) | Jobs: schedules, run history, health |
| [docs/board.md](docs/board.md) | The Board kanban |
| [docs/fleet-scope.md](docs/fleet-scope.md) | Settings › Fleet scope: which repos fleet-wide runs include |
| [docs/installing-agent-clis.md](docs/installing-agent-clis.md) | Installing each coding agent's CLI |
| [docs/run-troubleshooting.md](docs/run-troubleshooting.md) | Build identity, the watchdog, request logs |
| [docs/restart-and-liveness.md](docs/restart-and-liveness.md) | Restarting and proving the new build is live |
| [docs/config.md](docs/config.md) | Config files and registries |
| [docs/layout.md](docs/layout.md) | Annotated repo tree |
| [docs/architecture.mmd](docs/architecture.mmd) | Internal architecture diagram |
| [docs/verification.md](docs/verification.md) | Pytest, Playwright, design review and the pre-ship gate |
| [docs/e2e-conventions.md](docs/e2e-conventions.md) | Rules for writing browser tests, and the flaky legs |
| [docs/ci-github-actions.md](docs/ci-github-actions.md) | What CI runs and why |
| [docs/cache-hygiene-asset-hashing.md](docs/cache-hygiene-asset-hashing.md) | Content-hash asset stamping |
| [docs/iphone-debugging.md](docs/iphone-debugging.md) | Debugging real-iPhone Safari from the PC |
| [docs/launcher-owned-pty.md](docs/launcher-owned-pty.md) | The launcher-owned PTY terminal |
| [docs/detached-stop-decision.md](docs/detached-stop-decision.md) | Why detached sessions get a single Stop |
| [docs/pi-coding-agent.md](docs/pi-coding-agent.md) | Pi as a Coding-tab agent |
| [docs/life-os-zombie-cleanup.md](docs/life-os-zombie-cleanup.md) | Removing mistaken Life OS sessions |

