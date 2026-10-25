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
│       ├── manager.py         # adopt-or-spawn uvicorn lifecycle
│       ├── shell_html.py      # splices static/views/*.html into index.html where its include markers sit
│       ├── routers/           # split API routers (config, sessions, life_os, board, …)
│       └── static/            # SPA shell + PWA manifest + icons + vendored xterm.js
│           ├── views/         # each view's markup (panes, dialogs, overlays), spliced into index.html when served
│           └── styles/        # the app's own CSS: base.css (tokens, tabs, shell) + one file per view, linked in cascade order
│
├── src/                        # logic layer (no UI imports)
│   ├── app_config.py           # log level, webapp embed section
│   ├── webapp_config.py        # host/port/scan-paths/agent settings/secrets/terminal knobs
│   ├── launch_flags.py         # per-agent CLI flag builders (claude/codex/agy/copilot/pi/grok + resume)
│   ├── agents.py               # coding-agent registry (claude/codex/agy/copilot/pi/grok) + PATH detection
│   ├── registry.py             # apps registry (load/save/scan) + live claude-code rows
│   ├── scanner.py              # bat classifier + project-dir + life-os skill discovery
│   ├── launcher.py             # spawn_bat / spawn_claude_session helpers
│   ├── session_host.py         # PtySession + RemoteSession + SessionManager (ConPTY via pywinpty)
│   ├── session_host_scan.py    # pure PTY-protocol byte scanning (OSC title, colour-OSC, bracketed-paste, prompt-title)
│   ├── session_host_composer.py # typed-but-unsent composer state inferred from PTY input (composer_dirty)
│   ├── session_host_input.py   # server-initiated input-delivery protocol (settle-then-submit, ingest/echo verify, deferred-submit watcher)
│   ├── session_host_paths.py   # session-host CLAUDE.md-declared path parsing (stale_relevant scoping)
│   ├── vt_snapshot.py          # headless pyte VT mirror per fullscreen session (reconnect snapshot)
│   ├── _loopback_http.py       # shared loopback HTTP client base (session/voice/photo/tts)
│   ├── session_client.py       # webapp → session-host loopback HTTP client
│   ├── webauthn_gate.py        # passkey enrollment / assertion + terminal tokens
│   ├── audit.py                # terminal audit + per-session logs
│   ├── session_retention.py    # daily webapp/sessions retention sweep (session_retention_days)
│   ├── diagnostics.py          # log ring buffer + port-owner introspection
│   ├── static_versioning.py    # content-hash query strings for asset cache-busting
│   ├── subprocess_flags.py     # NO_WINDOW spawn-flag convention (single source, fleet-config#412)
│   ├── env_path.py             # effective-PATH resolution for coding-agent detection
│   ├── build_info.py           # git-sha / build-time metadata behind `GET /api/version`
│   ├── boot_autostart.py       # launch-on-login registration
│   ├── app_runtime.py          # process-level bootstrap shared by the CLI commands
│   ├── _json_io.py             # shared JSON read/write helpers
│   ├── instance_role.py        # primary-checkout vs worktree role detection
│   ├── context_filter_state.py # Context-filter card's persisted UI state
│   ├── fleet_scope.py          # Settings › Fleet scope: fleet-config's active/dormant switch, via its own writer
│   ├── api_tokens.py           # job-scoped bearer tokens (Settings → API tokens)
│   ├── auth/                   # credentials beyond the main token: the delegate token (read the Board, file issues)
│   ├── notifications.py        # notify_on_failure dispatch
│   ├── notify/                 # provider package behind notifications.py (Telegram, …)
│   ├── github_client.py        # `gh`-backed client for Board backlog/PR/issue data
│   ├── llm_client.py           # local-llm-hub client (root-cause lines, fleet chief, summarize)
│   ├── photo_ocr_client.py     # photo-ocr sibling-service client
│   ├── tts_client.py           # local-llm-hub TTS client (🔊 Read aloud)
│   ├── voice_client.py         # voice-transcriber sibling-service client
│   ├── chief_pointer.py        # fleet-chief PTY session lookup for the chief's ensure path
│   ├── chief_answers.py        # the chief's questions already answered, shared by every device
│   ├── chief/                  # the chief's plan card writes: plan_prune.py clears merged rows through fleet-config's writer
│   ├── board.py                # Board backend: kanban column computation
│   ├── board_state.py          # session-state join (session-host + fleet-config overlay)
│   ├── board_sessions.py       # live-session enumeration for the Board
│   ├── board_exchange.py       # drill-down drawer's last-exchange read
│   ├── transcript_locate.py    # filesystem correlation of a live session to its harness transcript file
│   ├── board_transcript.py     # transcript overlay + conversation-source hierarchy
│   ├── session_transcript.py   # paginated, typed transcript pages for one live Coding session
│   ├── session_screen.py       # a live session's terminal screen: answer a question, plan picker, resume, context % (#1424)
│   ├── transcript_flavors/     # per-harness transcript-line grammars (claude/codex/grok/pi/antigravity/copilot)
│   ├── usage/                  # usage-meter settings: providers.py owns the usage_providers list and its migration
│   ├── session_changes.py      # a session's Changed files, folded from its transcript (never `git diff`)
│   ├── session_outbox.py       # files an agent sends to Chat: path/type/size guards + runtime store (#1530)
│   ├── usage_log.py            # vendored: local view/tap log router + SQLite store (#1524)
│   ├── resume_picker.py        # Claude Code's `/resume` picker, answered from a Chat card
│   ├── plan_picker.py          # Claude Code's plan picker, read off the live terminal screen
│   ├── git_utils.py            # shared `git` subprocess runner — every git spawn goes through it (#794, #1111)
│   ├── life_os_index.py        # Life OS conversation-artefact reconciliation (index.json/index.md/search db)
│   ├── life_os_delete.py       # the one Life OS conversation delete routine: the Delete route and the zombie job share it (#1738)
│   ├── life_os_zombies.py      # zombie classifier + sweep; life_os_zombie_backup.py is its backup/restore/retention (#1738)
│   ├── channel_profiles.py     # Telegram channel launch profiles: loader + per-session settings file (#1366)
│   └── jobs*.py, jobs_kinds/   # Jobs backend, the largest group in src/ — config chain, scheduling/trigger/queue/reap,
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
├── tests/                      # pytest suite, one test_<module>.py per unit; a flat package by history (see below)
│   ├── unit/                   # unit tests by domain: board/ (Board backend), transcript/ (Coding-tab transcript)
│   ├── api/                    # API tests by router, with a shared-support module per tab (Life OS)
│   ├── e2e/                    # Playwright browser suite (chromium + iPhone projections) and its helpers; board/ and life_os/ hold one tab's modules each
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
- **Cache-busting is automatic**: `src/static_versioning.py` stamps every `href="/static/styles/*.css"` with the fleet hash, so a new file needs only its `<link>`.
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

- **Where new code goes:** a view's browser code in `app/webapp/static/`, its API in `app/webapp/routers/`, logic with no UI import in `src/`, tests under `tests/unit/<domain>/` (logic), `tests/api/` (routers) or `tests/e2e/<tab>/` (browser), not loose in the flat `tests/` or `tests/e2e/`. A new module in a full directory goes in a subpackage (`src/notify/`, `src/jobs_kinds/` and `src/transcript_flavors/` are the pattern), never loose in the directory.
- **Declared shape:** `webapp-tray`, with `webapp/` as the one extra root. It is the tray's runtime-state folder: only the two `*.sample.*` files in it are tracked, and the rest is gitignored. The preset itself lists `assets/` and `config/`.
- **Flat directories, baselined:** `src/`, `tests/`, `tests/e2e/` and `app/webapp/static/` each hold more than 40 modules today. They take no new module until they are split.
- **Layer import, known debt:** `src/chief_job_failed.py` imports `board_chief` from `app.webapp.routers` inside a function, which breaks the rule that `src` has no UI imports. The shared piece belongs in `src/`; until it moves, the baseline records the one import and a second one fails the gate.
- **Oversized files and functions:** every one over the limits is in the baseline at its current size. Splitting them is separate work, and a split lowers its entry.
