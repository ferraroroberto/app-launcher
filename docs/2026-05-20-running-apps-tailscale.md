# Running apps section + tap-to-open over Tailscale (issue #35)

## What was done

The Apps tab gained a **Running apps** section — the sibling of the
Claude Code tab's Running sessions panel. It lists every app spawned
from the launcher, binds each to the port it eventually grabbed, and
offers two actions per row:

- **🌐 Open** — opens the app in the phone's browser at
  `https://<tailnet_host>:<port>/`. Disabled (with a hover hint) while
  the app is still binding a port, or when `tailnet_host` isn't set.
- **⏹️ Stop** — kills that instance's process tree after a confirm.

The pre-existing port-probe panel (titled "Running apps" before) was
retitled **🔌 Port listeners** to remove the name clash.

### How it works

- **Spawn tracking** (`src/app_runtime.py`, new) — an in-process dict of
  `SpawnedInstance` records. `launch_app` calls `record_spawn` after
  every non-claude-code bat launch. Liveness uses `psutil.pid_exists` +
  `is_running()` and a `create_time() ± 1s` check against `started_at`
  to guard against Windows PID reuse. State is process-memory only — a
  webapp restart forgets it (orphans still show under Port listeners).
- **Port derivation** — `spawn_bat` now spawns `cmd /k <bat>` directly
  (not via `start`, which detaches the console and orphans the
  descendant tree) and returns the PID. `diagnostics.listening_port_for_pid_tree`
  walks that PID's descendant tree and returns the first LISTEN port —
  so Streamlit's auto-port behaviour (8501 → 8502 → …) needs no config.
- **Tailnet host** — new `tailnet_host` key in `config/config.json`
  (`src/app_config.AppConfig`). Empty string disables the Open button.
- **Scheme detection** — the URL scheme is not hard-coded (the issue's
  "always https" assumption was wrong: plain Streamlit serves HTTP, the
  FastAPI siblings serve HTTPS). `diagnostics.detect_local_scheme`
  attempts a TLS handshake on the loopback port — handshake completes →
  `https`, fails → `http`.

### API

- `GET /api/apps/running` → `{"running": [...]}`, each row with
  `app_id, name, kind, pid, started_at, port, url, alive`. `port`/`url`
  are null until a descendant binds / when `tailnet_host` is unset.
- `POST /api/apps/{app_id}/instances/{pid}/stop` → kills the PID tree,
  forgets the spawn, returns `{"stopped": pid}`. 404 for an untracked
  PID — the endpoint never kills an arbitrary process.

Both inherit the existing bearer-token middleware.

## Files modified

- `config/config.sample.json` — `tailnet_host` key + comment
- `src/app_config.py` — load + expose `tailnet_host`
- `src/app_runtime.py` *(new)* — spawn tracker
- `src/diagnostics.py` — `listening_port_for_pid_tree`, `kill_process_tree`
- `src/launcher.py` — `spawn_bat` returns PID; spawns `cmd /k` directly
- `app/webapp/routers/apps.py` — `record_spawn` wiring, two new routes
- `app/webapp/static/index.html` — Running apps section; Port listeners retitle
- `app/webapp/static/{state,apps,main}.js`, `styles.css` — fetch/render/poll
- `README.md` — `tailnet_host` paragraph under Config
- `tests/test_webapp_api_apps_running.py` *(new)*, `tests/e2e/test_running_apps.py` *(new)*
- `tests/test_webapp_api_apps.py` — `record_spawn` wiring assertion

## Validation

- `pytest tests -m "not smoke"` — 79 passed
- `scripts/verify-before-ship.ps1` — green (byte-compile + pytest +
  Playwright Chromium + WebKit/iPhone, auto-booted)
- Webapp restarted on `:8445`; `/api/version` confirms the live build.

Manual Tailscale validation (phone over the tailnet — real Streamlit
launch, Open, Stop, double-launch, webapp-restart-forgets) is the
operator's step per the issue.
