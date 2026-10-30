# Layout

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
│       ├── manager.py         # adopt-or-spawn webapp lifecycle
│       ├── serve.py           # the one launch entry: hypercorn, HTTP/2 over TLS, selector loop
│       ├── shell_html.py      # splices static/views/*.html into index.html where its include markers sit
│       ├── routers/           # split API routers (config, sessions, life_os, board, …)
│       └── static/            # SPA shell + PWA manifest + icons + vendored xterm.js
│           ├── views/         # each view's markup (panes, dialogs, overlays), spliced into index.html when served
│           ├── apps/          # Apps tab modules beside apps.js: the Coding list, the app dialogs, the list listeners
│           ├── board/         # Board tab modules beside board.js: dispatch, the answer sheet, the drawer, the Chief's plan card, its Clear merged control, the Done lane's earlier days and its Issue / PR / Proof actions
│           ├── background-tasks/ # the session ⋮ menus' Background tasks sheet: running and finished tasks, an output tail
│           ├── changes/       # Show changes and Changed files: the overlay, a diff, a file's view and its token colours
│           ├── chat/          # the session overlay's Chat pane: session-transcript.js, the entry renderer and its parts, the decision and resume cards
│           ├── jobs/          # Jobs tab modules beside jobs.js: a job row, a Recent runs row, the agenda, the job dialog
│           ├── life-os/       # Life OS tab modules beside life-os.js: conversations, the doc browser, the conversation viewer
│           ├── screen/        # the Screen view: the socket and lifecycle (screen-view.js), control, keys, quality, and their pure helpers
│           ├── settings/      # Settings sheets and cards: launch defaults, API tokens, Telegram, the context filter, fleet scope
│           ├── terminal/      # the Terminal pane's modules beside terminal.js: bar, composer bytes, socket, keys, mirror, read-aloud, theme, touch
│           └── styles/        # the app's own CSS: base.css (tokens, tabs, shell) + one file per view, linked in cascade order
│
├── src/                        # logic layer (no UI imports); flat modules first, then one subpackage per domain
│   ├── app_config.py           # log level, webapp embed section
│   ├── webapp_config.py        # host/port/scan-paths/agent settings/secrets/terminal knobs
│   ├── webapp_settings/        # values.py: the defaults and value sets every webapp_config field validates against
│   ├── agents.py               # coding-agent registry (claude/codex/agy/copilot/pi/grok) + PATH detection
│   ├── registry.py             # apps registry (load/save/scan) + live claude-code rows
│   ├── scanner.py              # bat classifier + project-dir + life-os skill discovery
│   ├── session_host.py         # PtySession + SessionManager (ConPTY via pywinpty)
│   ├── session_host_scan.py    # pure PTY-protocol byte scanning (OSC title, colour-OSC, bracketed-paste, prompt-title)
│   ├── session_host_composer.py # typed-but-unsent composer state inferred from PTY input (composer_dirty)
│   ├── session_host_input.py   # server-initiated input-delivery protocol (settle-then-submit, ingest/echo verify, outcome vocabulary)
│   ├── session_hosting/        # session-host parts: session kinds + stop modes, child env, detached console sessions, deferred-submit watcher
│   ├── session_host_paths.py   # session-host CLAUDE.md-declared path parsing (stale_relevant scoping)
│   ├── vt_snapshot.py          # headless pyte VT mirror per fullscreen session (reconnect snapshot)
│   ├── console_input.py        # per-send console-input helper, run as `python -m`, off the session-host closure
│   ├── audit.py                # terminal audit + per-session logs
│   ├── session_retention.py    # daily webapp/sessions retention sweep (session_retention_days)
│   ├── diagnostics.py          # log ring buffer + port-owner introspection
│   ├── static_versioning.py    # content-hash query strings for asset cache-busting
│   ├── stylesheet_bundle.py    # the linked stylesheets concatenated into the one /static/bundle.css
│   ├── subprocess_flags.py     # NO_WINDOW spawn-flag convention (single source)
│   ├── env_path.py             # effective-PATH resolution for coding-agent detection
│   ├── build_info.py           # git-sha / build-time metadata behind `GET /api/version`
│   ├── boot_autostart.py       # launch-on-login registration
│   ├── app_runtime.py          # process-level bootstrap shared by the CLI commands
│   ├── _json_io.py             # shared JSON read/write helpers
│   ├── instance_role.py        # primary-checkout vs worktree role detection
│   ├── context_filter_state.py # Context-filter card's persisted UI state
│   ├── fleet_scope.py          # Settings › Fleet scope: fleet-config's active/dormant switch, via its own writer
│   ├── git_utils.py            # shared `git` subprocess runner — every git spawn goes through it
│   ├── notifications.py        # notify_on_failure dispatch
│   ├── notify/                 # vendored provider package behind notifications.py (Telegram, …)
│   ├── usage_log.py            # vendored: local view/tap log router + SQLite store
│   ├── auth/                   # credentials beyond the main token: api_tokens.py (job-scoped tokens), the delegate token,
│   │                           #   webauthn_gate.py (passkey enrollment / assertion + terminal tokens)
│   ├── clients/                # loopback HTTP clients: the shared _loopback_http.py base, the session-host (session.py),
│   │                           #   local-llm-hub (llm.py, tts.py), photo-ocr, voice-transcriber, and `gh` (github.py)
│   ├── launching/              # coding-agent launch: per-agent CLI flags (flags.py), spawn helpers (launcher.py),
│   │                           #   the model catalog, Telegram channel profiles, VS Code workspaces
│   ├── board/                  # Board backend: kanban assembly (__init__.py), session-state join (state.py),
│   │                           #   live-session enumeration (sessions.py), the drawer's last exchange (exchange.py),
│   │                           #   the transcript overlay (transcript.py, dispatch_ids.py, transcript_exchange.py)
│   │                           #   the Done lane's proof reports (proof_reports.py: a read-only view of each repo's `.proof/` store)
│   ├── chief/                  # the fleet chief: identity.py (which live session is the chief), the plan card, its pointer,
│   │                           #   answered questions, dispatch briefs, the job-failed event
│   ├── session_view/           # a live Coding session's views: typed transcript pages, Changed files, the Chat outbox,
│   │                           #   background tasks, the plan and `/resume` pickers, transcript-file location, the working-tree diff
│   ├── transcript_flavors/     # per-harness transcript-line grammars (claude/codex/grok/pi/antigravity/copilot)
│   ├── desktop/                # the Screen view's PC side: capture, input, cursor, clipboard, browser
│   ├── life_os/                # Life OS: capture, history, title, the delete routine, the index, zombie sweep + backup, the relay registry
│   ├── usage/                  # usage meter: providers.py, auto_switch.py (the Grok fallback for Sonnet lanes),
│   │                           #   quota.py, export.py (Send to task-os)
│   └── jobs/, jobs_kinds/      # Jobs backend: the facade (__init__.py), config chain, scheduling/trigger/queue/reap,
│                               #   history/stats/coverage/outcome, webhooks, secrets, per-kind executors
│                               #   (batch/http_check/inline_shell/powershell/python/shell_wsl)
│
├── scripts/
│   ├── gen_icons.py            # thin caller onto project-scaffolding's shared brand_gen.py (rocket master);
│   │                           #   finds that checkout as this repo's sibling — PROJECT_SCAFFOLDING_DIR overrides
│   ├── gen_tailscale_cert.py   # tailscale cert (real LE) + --check auto-renew
│   ├── gen_token.py            # bearer token rotate / clear
│   ├── set_password.py         # login password set / clear
│   ├── session_retention.py    # dry-run: what the webapp/sessions retention sweep would remove
│   ├── probe_repaints.py       # counts an agent's full-viewport repaints in a session transcript
│   │                           #   (#930's measurement — see docs/launcher-owned-pty.md)
│   ├── run_named_tunnel.py     # uvicorn + cloudflared (headless)
│   ├── telegram_relay.py       # the relay CLI a relay-enabled Telegram bot runs (#1639)
│   ├── classify_e2e.py         # diff-proportionate e2e-tier routing against `main`
│   ├── e2e-gate-route.ps1      # turns the classifier's verdict into the gate's pytest args + mutex flag
│   ├── run-e2e.ps1             # dev-loop e2e runner (dual-projection, or --browser chromium)
│   ├── restart-session-host.ps1 # confirmation-gated `:8446` restart — see CLAUDE.md `## session-host`
│   └── verify-before-ship.ps1  # the pre-ship gate — disposable webapp + session-host, no tray needed
│
├── tests/                      # pytest suite; shared helpers and the root conftest sit here, every test module in a folder below
│   ├── unit/                   # unit tests by domain: board/ chief/ clients/ core/ desktop/ jobs/ launching/ life_os/ pty/ repo/
│   │                           #   scanner/ session_host/ sessions/ shell/ static_ui/ telegram/ transcript/ tray/ usage/ webapp_config/
│   ├── api/                    # API tests by router, with a shared-support module per tab (Life OS); webapp/ and nonblocking/ hold the webapp suite
│   ├── e2e/                    # Playwright browser suite (chromium + iPhone projections): one folder per tab (apps/ board/ chat/ code/
│   │                           #   compose/ jobs/ life_os/ screens/ session/ settings/ shell/ terminal/), fixtures/ for the conftest's parts
│   ├── js/                     # plain-Node tests for browser-side logic that a desktop engine cannot judge
│   └── fixtures/               # recorded transcripts and sample files the tests read
│
├── docs/                       # durable reference, one topic per file; architecture.mmd is the structural diagram
│
├── config/                     # *.sample.json committed, real files gitignored
│   ├── config.sample.json
│   ├── webapp_config.sample.json
│   ├── apps.sample.json
│   ├── jobs.sample.json
│   └── channel_profiles.sample.json  # Telegram channel launch profiles (#1366)
│
├── assets/                    # generated by scripts/gen_icons.py, committed
│   ├── tray/app-launcher.ico       # Windows tray icon (16/32/48/64/256)
│   └── stream-deck/app-launcher-144.png  # Elgato Stream Deck button
│
└── webapp/                    # runtime state — all gitignored except samples
    ├── certificates/          # cert.pem / key.pem from gen_tailscale_cert
    ├── cloudflared.sample.yml
    ├── cloudflared.yml        # your filled-in copy (gitignored)
    ├── terminal-themes.sample.json  # VS Code-style PTY terminal theme overrides (#381)
    ├── terminal-themes.json   # your tuned copy (gitignored) — per-mode xterm colors + contrast
    ├── last_tunnel_url.txt    # tray + run_named_tunnel write here
    └── auth.log               # failed-login audit
```

## Stylesheets

`app/webapp/static/styles/` holds the app's own rules, cut into files of at most 1,000 lines. `index.html` links them one by one, and that link order is the cascade: a rule in a later file wins a tie against an earlier one exactly as it did when they were a single sheet.

- **Tokens, tabs, cards and the page header** live in `base.css`; the bottom-tab nav and the standalone PWA shell rules are there, so they stay first.
- **A view's rules go in that view's file**: `settings.css`, `jobs.css`, `lifeos.css`, `board.css`, `code.css`, `apps.css`, `terminal.css`, and `chat.css` / `chat-cards.css` for the transcript pane. Rules several views share go in `shared.css` (toasts, dialogs, the Glance kit, row menus) or `lists.css` (collapsible cards, option rows, card lists).
- **A rule that overrides another stays after it.** The `wide.css` desktop media blocks restyle the views above, so they sit below those views' files; a new file goes where its rules must win, not alphabetically.
- **A file that would pass 1,000 lines splits at a section comment**, never inside a block, and its link goes right after the file it was cut from.
- **The browser fetches one stylesheet.** The source files stay separate, but at server start `src/stylesheet_bundle.py` concatenates every sheet `index.html` links, vendored ones included, in link order into `/static/bundle.css`, and the served page links only that: over HTTP/1.1 a phone queues behind six connections per origin, so 31 requests cost far more than one. A new file needs only its `<link>` in `index.html`; the bundle sheets must use no `@import`, `@charset` or relative `url()`, which `tests/unit/static_ui/test_stylesheet_bundle.py` pins. `xterm.css` loads lazily and goes first in the head, ahead of the bundle, so the app's terminal rules still override it.
- **Cache-busting is automatic**: `src/static_versioning.py` stamps the bundle URL with the fleet hash, which also covers `index.html`'s link order, so a reorder busts the immutable cache.
- **Tests read the sheets through `tests/_css.py`**, which concatenates them in `index.html`'s link order.

## Page shell

`app/webapp/static/index.html` is the shell: the head, the icon sprite, the bottom-tab nav, the toast and login mount points and the scripts. Everything a view draws lives in `app/webapp/static/views/<name>.html`, named for its view (`board-pane.html`, `jobs-dialogs.html`, `overlays.html`), and `index.html` pulls each in with a marker line on its own:

```html
<!-- @include views/board-pane.html -->
```

- **The server splices, the browser sees one page.** `app/webapp/shell_html.py` replaces each marker with its fragment's text, so `GET /` returns the same bytes a single-file `index.html` would, with no client-side render and no first-paint gap. A module's import-time `getElementById` therefore still finds its element.
- **Marker order is document order.** A dialog moves only by moving its marker or its text, never by renaming a fragment; a fragment holds contiguous markup, indented as it sits in the page.
- **A new view's markup goes in its own fragment**, with a marker where it belongs in `index.html`. The shell stays under 1,000 lines and holds no pane or dialog; `tests/unit/shell/test_shell_html.py` pins both.
- **Fragments do not nest and must sit under `views/`.** The page cache keys on `index.html` and every fragment, so an edit on disk shows on the next load without a restart.
- **Tests that read the page as source go through `assemble_shell`**, not `read_text`, so they see the fragments.

## Structure ratchet

`scripts/structure_ratchet.py` (vendored byte-for-byte from project-scaffolding, recorded in `.fleet.toml` `[vendored]`) runs in the pre-ship gate. It fails a new file over 1,000 lines, a new function over 100 lines, a directory past 40 modules, a `src` file that imports `app`, and a root folder the declared shape does not list. A finding already in `.structure-ratchet-baseline.json` may not grow past its recorded size. Shrinking one means lowering the baseline with `--update-baseline`; raising or adding an entry needs `--allow-additions`, which shows in review.

- **Where new code goes:** a view's browser code in `app/webapp/static/`, its API in `app/webapp/routers/`, logic with no UI import in `src/`, tests under `tests/unit/<domain>/` (logic), `tests/api/<router>/` (routers) or `tests/e2e/<tab>/` (browser), never loose in `tests/` or `tests/e2e/`, which hold only shared helpers and the conftests. A new module in a full directory goes in a subpackage (`src/notify/`, `src/jobs_kinds/` and `src/transcript_flavors/` are the pattern), never loose in the directory.
- **Declared shape:** `webapp-tray`, with `webapp/` as the one extra root. It is the tray's runtime-state folder: only the two `*.sample.*` files in it are tracked, and the rest is gitignored. The preset itself lists `assets/` and `config/`.
- **Flat directories:** `tests/` and `tests/e2e/` hold only helpers and conftests, so a test module never goes loose in either. `src/` is under the limit because its domains sit in subpackages; a new module goes in its domain's package, not loose in `src/`. `app/webapp/static/` is under the limit because its views' modules sit in the subfolders above; a new module goes in its view's folder, not loose beside `main.js`.
- **The e2e conftest is a registrar.** `tests/e2e/conftest.py` holds the pytest hooks, the `_autoboot_server` session fixture and the fixtures it re-exports; the code behind them is in `tests/e2e/fixtures/`: `env.py` (paths, env-var names, run state), `waits.py` (waiting helpers and timeout budgets), `ui.py` (page-driving helpers), `processes.py` (spawning and leak scans), `autoboot.py` (the disposable servers' boot and isolation checks), `launch_target.py`, `sessions.py` and `pages.py`.
- **Oversized files and functions:** every one over the limits is in the baseline at its current size. Splitting them is separate work, and a split lowers its entry.
