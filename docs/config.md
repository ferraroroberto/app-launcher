# Config

Two committed JSON templates; real files are gitignored.

## `config/config.json`

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

The `webapp` section also accepts three optional tuning knobs, omitted
above because the defaults are almost always right:
`startup_timeout_seconds` (`15.0`) — how long the tray waits for uvicorn
to answer on `:8445` before declaring the boot failed; raise it on a
loaded box that boots slowly. `request_timeout_seconds` (`1.0`) — per
health-probe HTTP timeout. `poll_interval_seconds` (`0.4`) — gap between
those probes while waiting for startup.

`tailnet_host` is the Tailscale (MagicDNS) hostname of this PC. The Apps
tab's **Running** card uses it to build each launched app's
remote URL (`<scheme>://<tailnet_host>:<port>/`) so you can tap its row
from the phone and land on the app. The scheme is auto-detected per app
(a TLS probe of the bound port — `https` for the FastAPI siblings,
`http` for a plain Streamlit server). Leave it empty (`""`) to disable
the feature — the Open button is then shown disabled with a hover hint.

## `config/webapp_config.json`

UI prefs + secrets, authored from the web UI:

| Key | Default | What it controls |
|---|---|---|
| `host` | `"0.0.0.0"` | uvicorn bind host |
| `port` | `8445` | uvicorn bind port |
| `projects_dir` | parent of this repo | Master folder whose direct child directories the Coding tab lists as projects |
| `projects_ignore` | `[]` | gitignore-style folder-name patterns (case-insensitive, `*`/`?` globs) hidden from the Coding tab |
| `coding_favorites` | `[]` | Project ids (scanner slugs) starred as favorites in the Coding tab. Managed by the per-tile ★ — favorites pin to the top of the list and the header **★ Favorites** toggle filters to just these. Not normally hand-edited. |
| `coding_hidden_agents` | `[]` | Coding-row launch buttons hidden from the project rows — agent ids plus the pseudo-ids `vscode` (the whole ⋯ project menu: VS Code · Show changes · Open folder) and `github`. Managed by the **Visible agents** switches in Settings › Launch defaults. A *hidden* list, so a newly registered agent appears by default. |
| `hidden_models` | Codex Terra, Sol, Astra | Provider-qualified models (`codex:gpt-5.6-sol`) the model dropdowns leave out: the Projects, Skills, Board dispatch and per-agent Launch defaults pickers. Managed by the **Models in the dropdowns** switches in Settings › Launch defaults. A *hidden* list, so a model newly added to the catalog appears by default; a saved default that is hidden falls back to the first visible model of its agent. |
| `apps_scan_root` | parent of this repo | Where the Apps tab scans recursively for `*.bat` |
| `life_os_dir` | sibling `../life-os` | Root of the `life-os` checkout the Life OS tab surfaces (skills at `<life_os_dir>/.claude/skills`, identity at `<life_os_dir>/identity`). When the skills dir doesn't exist the tab shows disabled, the same way the Coding tab handles a missing `projects_dir`. |
| `life_os_refresh_skill_instructions` | `true` | A resumed Life OS conversation first re-reads its skill's instructions when they changed since it last loaded them ([Features](features.md)). Settings › Life OS, saved at once and read on the next resume. |
| `fleet_config_dir` | sibling `../fleet-config` | Root of the `fleet-config` checkout (shown as **fleet-config folder** in Settings) that the Board, sessions, Life OS conversations and the fleet chief read, and whose canonical `skills/_lib/quota_snapshot.py` / `quota_sources.py` own quota validation and Codex refresh. Missing contract code is reported as a quota source error; the launcher does not duplicate its collectors. Formerly `claude_config_dir`: an older `webapp_config.json` is still read under the old name and rewritten with the new one on the next save. |
| `terminal_history_lines` | `10000` | Bounded scrollback (200-50000 lines) a full-screen agent (Codex, etc.) session keeps for a (re)connect, Settings-tab configurable. Too low and the true start of a real conversation becomes unreachable; too high risks a slower reconnect paint on a weak mobile connection. |
| `large_upload_max_mb` | `2048` | The compose bar's **Attach large file (not read)** limit in MB (1-10240), Settings-tab configurable. The ordinary attach keeps its fixed 12 MB. Enforced by the webapp (a declared `Content-Length` over it is refused before anything moves, and the relayed byte count is checked as it streams) and passed to the session-host per upload, so a change applies without a session-host restart. |
| `sessions_state_file` | `~/.claude/hooks/state/sessions-state.json` | The sessions-state file fleet-config's `session_state` hook writes, read by the Board tab. Absent/corrupt/stale degrades to `unknown` session status, never an error. |
| `usage_providers` | `["claude", "codex"]` | Which providers the usage meter draws, any of `"claude"`, `"codex"` and `"grok"`, stored in card order; `[]` hides the Code tab's Usage card. Settings › Usage meter, one switch each, saved at once and applied on every device. An older file's single `usage_shows` choice (`claude`, `codex`, `both`, `none`) is read into this list, and the next save drops it. Grok shares no limits with scripts, so its row never draws a bar: it shows today's spend, summed from `grok usage <session-id>` over the sessions touched since local midnight and refreshed at most every two minutes, or `limits not exposed by Grok` when that ledger cannot be read. |
| `grok_auto_switch` | `false` | With it on, a `POST /api/board/issues/start` asking for the Sonnet tier (`claude:sonnet`, or a bare `sonnet`) launches `grok:grok-4.5` instead when Claude's weekly used percent is within 5 points of the week's pace or above it. Pace is the share of the weekly window elapsed, the same figure as the usage meter's pace tick. Opus, Fable and Codex starts never switch. A Claude reading that is not fresh, a weekly window with no reset ahead, Grok not installed, or a repo folder that `~/.grok/trusted_folders.toml` does not trust exactly (`grok --trust` once in the repo's primary checkout) keeps the lane on Claude. Settings › Chief › Grok for Sonnet lanes saves it through `POST /api/config`. |
| `rate_limits_file` | `~/.claude/hooks/state/rate-limits.json` | Legacy Claude 5h/7d cache path and the directory anchor for fleet-config's versioned `quota-v1/` provider shards. The legacy file is used only while the canonical Claude shard is absent; an unknown timestamp never counts as fresh. |
| `chief_plan_file` | `~/.claude/hooks/state/chief-plan.json` | The fleet chief's plan (format v1, written by fleet-config's `chief_ops.py plan`), shown by the Board's **Chief's plan** card. Absent or an empty queue leaves the card blank; unreadable JSON or an unsupported `version` adds a quiet *Plan unreadable* note, never an error. |
| `chief_managed_file` | `~/.claude/hooks/state/chief-managed.json` | The registry of chief-dispatched sessions (written by fleet-config's `chief_managed.py`). The session-host's reaper reads it when a session dies: a chief-managed one gets a `session-end` event in the chief's mailbox even if its own hook never ran. An unreadable registry logs an *undetermined* line and publishes nothing. Read by the session-host, so a change here needs its restart. |
| `chief_inbox_dir` | `~/.claude/hooks/state/chief-inbox` | The chief's mailbox directory (contract: fleet-config `docs/chief-mailbox.md`); the reaper publishes one atomic `<ms>-<id>.json` per event. The chief doorbell counts it. |
| `chief_doorbell` | `false` | The chief doorbell: every 3 s the canonical webapp checks the fleet chief, and when it is idle at its prompt (Board status `idle-finished` or `awaiting-input`; never a permission prompt or picker), its composer is clean (the session-host's `composer_dirty` is `false`; unknown never rings) and `chief_inbox_dir` holds pending events (counted by file name, per fleet-config `docs/chief-mailbox.md`), it types the contract's fixed line `⟳ chief-inbox: pending — run chief_ops.py inbox` with submit. It waits 3 s first, so lanes that finish together ring once, then backs off 60 s, ringing again if the chief is still idle with mail. The line carries no data. Settings > Chief > Doorbell saves it through `POST /api/config` (read every tick, so no restart); off is also the phone's kill switch. With it off the chief arms its own `wait-event` waiter instead. |
| `chief_job_failures` | `true` | Publishes one `kind: "job-failed"` v2 event into `chief_inbox_dir` for every failed Job run, so the doorbell above can wake the chief. It is the third channel of `notify_failure`, so the executor's normal finalise, a watchdog kill and the stranded-run reaper all publish, and a success or a designed deferral publishes nothing. It publishes only on the canonical instance and only while the session-host lists a live chief (an unreadable session list publishes nothing). The event carries `job_id`, `job_name`, `run_id`, `exit_code` (`null` when reaped), `outcome` (`failed` or `not-confirmed`), `trigger` (`schedule`, `manual`, `webhook` or `chain`), `script_path`, `repo` (the direct child of `projects_dir` holding `script_path`, else `null`) and an `excerpt` of the last 1,500 characters of `output.log` (`excerpt_source: "job-output"`); `launcher_sid`, `number` and `dispatch_id` are `null`. The file is written atomically, per fleet-config `docs/chief-mailbox.md`. A write failure is logged and swallowed. Settings > Chief > Job failures saves it through `POST /api/config`; each failed run re-reads the config, so no restart is needed. |
| `context_filter_mode_file` | `~/.fleet-context-filter/mode.json` | The machine-wide off/shadow/rewrite switch for the fleet's PreToolUse context-filter hook — the Settings tab's **Context filter** card reads and writes it. A plain file write, effective immediately for every coding-agent session on the machine; absent file reads as the true "off" default, a corrupt file degrades the panel, never an error. |
| `context_filter_log_file` | `~/.fleet-context-filter/shadow.jsonl` | The context filter's savings telemetry — one JSON row per hook invocation (tokens saved, agent, command). The Settings › Context filter sheet's stats block aggregates it (cached, recomputed only when the file changes); absent/corrupt degrades to the stats block hiding, never an error. The writer rotates the file at 20 MB keeping one prior generation, so the stats totals reflect the current generation and reset at rotation. |
| `github_owner` | `"ferraroroberto"` | GitHub owner whose repos the Board tab's `gh` searches span (Backlog / PRs / Done-today). |
| `chief_model` | `"fable"` | Model the fleet chief spawns on (`sonnet`/`opus`/`fable`). Edited from Settings › Chief (the Board's chief gear opens it). |
| `chief_worker_cap` | `3` | Max concurrent worker sessions the `/chief` skill may keep running (1-10). Read by the skill over loopback via `GET /api/board/chief/settings` — its dispatch rail, phone-tunable from Settings › Chief. |
| `chief_auto_compact_threshold` | `30` | The context-use percent at which the fleet chief compacts itself: `0` is off, otherwise 10-90. Set from Settings › **Chief** (its switch turns it off) through `POST /api/config`; the chief reads it from `GET /api/board/chief-settings` → `{"auto_compact_threshold": <int>}` (board token, the default when nothing is stored). The app only stores the number and never compacts anything itself. |
| `claude_model` | `"opus"` | Default `--model` for `claude` (Claude Code button only) |
| `channel_model` | `"sonnet"` | Model Telegram channel sessions start on: the boot-time autostart, the Life tab's **Start on Telegram** and **Restart** (neither sends a model), and any channel launch whose request names no model. An explicit model in an API request still wins. A running channel session keeps its model until it is restarted. Set it through `POST /api/config` (no Settings control). |
| `claude_model_efforts` | `{}` | Per-model `--effort` **overrides only**, e.g. `{"opus": "high"}`: precedence is the model's override, then its catalog default (`opus` `medium`, `sonnet`/`fable` `high`), then `high`. A value of `"off"` omits the flag for that model. The legacy global `claude_effort` key is read once on load (`high` dropped, any other value carried to every Claude model) and gone on the next save. |
| `claude_verbose` | `true` | Pass `--verbose` |
| `claude_debug` | `false` | Pass `--debug` |
| `claude_permission_mode` | `"auto"` | Permission flag: `"auto"` → `--permission-mode auto`, `"skip"` → `--dangerously-skip-permissions` |
| `pi_model_efforts` | `{}` | Per-model `--thinking` **overrides only**, e.g. `{"gpt-5.6-sol": "low"}`: precedence is the model's override, then its catalog default (`high` for every Pi model), then `high`. Any level of Pi's `off`…`max` ladder. Edited from Settings › Launch defaults › Pi (the Effort row follows the Model combo; "Model default" clears the override). The legacy global `pi_effort` key is read once on load (`high` dropped, any other value carried to every Pi model) and gone on the next save. |
| `grok_effort` | `"high"` | Reasoning tier for `grok` (`low`/`medium`/`high`) → `--reasoning-effort`. Edited from Settings › Launch defaults › Grok Build. |
| `grok_permission_mode` | `"auto"` | Permission flag for `grok`: `"auto"` → `--permission-mode auto`, `"skip"` → `--permission-mode bypassPermissions`. |
| `auth_token` | `""` | Bearer token. Empty = gate off (unless `api_tokens` has entries). |
| `auth_password` | `""` | Optional companion for `/api/login`. |
| `secrets` | `{}` | One gitignored place for job secret values: a job's `webhook.secret` and any `Job.env` value can be `$secret:<key>` resolved against this dict at fire time. Legacy key `webhook_secrets` still loads. |
| `api_tokens` | `[]` | Scoped bearer tokens minted from **Settings → API tokens**: salted-hash records whose job-scoped kind can only call `POST /api/jobs/<id>/run` for its allowed jobs — safe to bake into a Stream Deck URL. Don't hand-edit. |
| `delegate_token` | `{}` | The delegate token, off by default: a salted-hash record turned on, regenerated or turned off from **Settings → API tokens → Delegate token**. See [The delegate token](#the-delegate-token). Don't hand-edit. |
| `session_host_port` | `8446` | Loopback port the PTY session-host binds. Never network-reachable; must differ from `port`. |
| `tailnet_allowlist` | `[]` | Extra IPs / CIDRs allowed to reach the terminal endpoints, on top of loopback + `100.64.0.0/10`. |
| `outbox_scratch_roots` | `["%TEMP%/claude"]` | Folders, besides a session's own, that an agent may send a file to Chat from: the outbox and Claude's `SendUserFile`. Edited in Settings → Folders → Chat file folders, which stores the text as typed and refuses a network or device path, a whole drive, a missing folder, a file and a duplicate; at least one folder stays. `%VAR%` and `~` expand. The default is Claude Code's scratch base, so a file from any session's scratchpad is admitted; a non-empty list here replaces it (an empty one reads as unset, like every list setting). Besides images, PDF, text and markdown, a `.zip`, `.gz`, `.tgz` or `.7z` from any of these folders is shown as a download-only card (see the outbox paragraph above). |
| `claude_show_local_window` | `true` | Open an interactive terminal window on the PC when a session is launched from the phone. |
| `desktop_browser` | `"chrome"` | Where a link tapped in Chat or a transcript opens when the page runs on the PC itself (a loopback page or a session's mirror window): `"chrome"` opens it in Chrome, found through the registry's App Paths and falling back to the Windows default when Chrome isn't installed; `"system"` uses the Windows default. A phone or laptop over the tailnet always opens links itself. |
| `webauthn_rp_id` | `""` | Passkey relying-party ID — the bare tailnet hostname. Empty disables the passkey gate. |
| `webauthn_rp_name` | `"Launcher"` | Display name shown in the passkey prompt. |
| `webauthn_origin` | `""` | Full https origin the phone connects to (scheme + host + port). |
| `voice_transcriber_url` | `https://127.0.0.1:8443` | Base URL of the sibling voice-transcriber webapp the composer's 🎤 dictation proxies to over loopback. Empty string disables dictation (the mic button stays in the grid, disabled). |
| `photo_ocr_url` | `https://127.0.0.1:8444` | Base URL of the sibling photo-ocr webapp the composer's 📷 screenshot OCR proxies to over loopback. Empty string disables OCR (the option leaves the 🖼 Image button's menu). |
| `llm_hub_url` | `http://127.0.0.1:8000` | Base URL of the sibling local-llm-hub the 🔊 read-aloud's Orpheus voice proxies to over loopback. Plain HTTP — the hub serves no TLS. Empty string disables the hub path (🔊 falls back to the on-device Web Speech voice). |
| `pushover_api_token` / `pushover_user_key` | `""` | Pushover credentials for Jobs-tab failure notifications. Both must be set; missing creds = no-op. |
| `notify_on_failure` | `false` | Master switch — even with creds set, no push fires until this flips on. |
| `notify_failure_streak` | `0` | When > 0, also fire a separate "N consecutive failures" push when the failure streak ticks to exactly this count. |
| `notify_failure_summary` | `false` | When `true`, pipe the output tail through the local LLM hub at `llm_hub_url` (`claude-haiku-4-5`) for a one-line root-cause line prepended to the push body. |
| `telegram_chat_log` | `""` | Optional Telegram chat for routine log lines: the nightly `life-os-zombie-cleanup` summary goes here, not to the alerts chat. Empty falls back to `telegram_chat_id`, so a single-chat setup is unchanged. Job-failure alerts always use `telegram_chat_id`. |
| `telegram_bot_token` / `telegram_chat_id` | `""` | Telegram credentials for the per-job `alert_on_failure` channel. Both must be set; missing creds = no-op. Independent of the Pushover settings above — this fires only for jobs with `alert_on_failure: true`, not globally. |
| `jobs_coverage_interval_minutes` | `60` | Minutes between background missed-fire coverage scans — a scheduled job whose `\AppLauncher\` Task Scheduler entry is missing/disabled, or whose slot elapsed with no run record. A job with *no* run history at all is reported `unknown`, never "not firing" — an empty run-history store can't distinguish a missed fire from a missing record, and the badge/alert would be a false positive; the structural half still flags a missing or disabled entry regardless. `0` disables the background tick; the Jobs-tab **⚠ not firing** badge still computes on poll. On by default because it pushes nothing on its own — alerts route through the two opt-in gates above. Only the **canonical** instance runs the tick — see below. |
| `session_retention_days` | `365` | **Retention window for `webapp/sessions/`** (decided 2026-09-11) — both `<id>.log` and `<id>.transcript`. Once a day the webapp deletes every such file whose mtime is older than this, off the event loop (`src/session_retention.py`). It never deletes a file belonging to a **live** session (checked against the session-host's list), never follows a symlink/junction, and treats anything it can't establish as `unknown` and keeps it: a file it can't stat or whose mtime is in the future is logged and skipped; an unreachable session-host, an unlistable directory, or a clock under which *every* file looks expired stands the whole sweep down. `0` keeps everything forever. Hand-edit only — deliberately not patchable from the Settings tab. Canonical instance only, like the coverage tick. Preview without deleting: `.\.venv\Scripts\python.exe scripts\session_retention.py` (`--days N` to try another window). |

**Only the canonical checkout may alert.** The coverage tick pushes to a real phone, so it is gated on `src/instance_role.py::canonical_instance`, not just on the e2e autoboot marker. A **linked git worktree** — a `.git` *file* rather than a `.git` *directory* — is never canonical: it carries the real `config/jobs.json` and the real Telegram/Pushover credentials, but `webapp/jobs/` resolves under *its own* root, so it reads an empty run history and concludes every scheduled fire was missed. That is exactly what happened on 2026-08-10, when a webapp booted out of `app-launcher-wt-727` pushed three false "scheduled run never fired" alerts. A non-canonical instance logs its reason and its root at `WARNING` and starts no tick. Inference cannot distinguish a *second full clone* from the real one, so `LAUNCHER_CANONICAL_INSTANCE=0` force-silences an instance and `=1` force-arms one (e.g. a deployment with no `.git` at all, which otherwise stands down rather than guessing). The `LAUNCHER_SESSION_HOST_PORT` autoboot exemption is checked first and cannot be overridden.

`--remote-control` is always added to Claude Code. Codex fresh and resume launches pass `--model <id>` plus a compatible `-c model_reasoning_effort=<level>`; permission flags are omitted from `codex resume`, whose subcommand rejects them. Full-control Codex sessions additionally carry `-c disable_paste_burst=true`.

## The delegate token

A narrow credential for an outside agent, such as a cloud-hosted bot, that should see the fleet and hand it work without being able to run anything. The main bearer token can spawn coding sessions that run with full permissions on this PC, so it must never sit in a third party's store. The delegate token is the one to hand over instead.

It allows exactly three calls, and every other request carrying it gets a 403 that names the limit, terminal endpoints included even from the tailnet:

- `GET /api/board`: the Board snapshot.
- `GET /api/board/chief-plan`: the chief's plan.
- `POST /api/board/issues/file` with `{"repo", "title", "body"}`: files a GitHub issue on a repo in the projects folder, assigned to the owner, with a footer saying the delegate token filed it. Nothing is spawned. The fleet chief triages every filed issue before any lane builds it.

It is off until **Turn on** in Settings → API tokens shows its value once. **Regenerate** replaces it and the old value stops at once; **Turn off** removes it. A wrong or turned-off value gets the ordinary 401. Loopback callers and the main token behave as they would without it. Every request made with it goes to `webapp/auth.log` with its method, path and outcome, never the token. It narrows only a gate that is on: with `auth_token` empty and no `api_tokens`, the API is open to every caller anyway.

The network side is the owner's to set up: give the agent a VPN identity that can reach only this PC's launcher port, nothing else on the machine or the tailnet.

## Refreshing model catalogs

Run `claude --version; claude --help`, `codex --version; codex --help; codex resume --help`, `pi --version; pi --list-models claude-agent-sdk; pi --list-models openai-codex`, and `copilot --version; copilot help config`. Update `src/model_catalog.py` only from those harness outputs. A documented API model is not launchable until the relevant installed subscription harness advertises its exact ID; keep rollout entries disabled meanwhile. **GitHub Copilot is deliberately absent from `src/model_catalog.py`** — `copilot help config` reports what the *CLI* accepts, not what the *account* may launch, so a catalogue here could only produce claims the launcher cannot keep; there is nothing to refresh.

## `config/apps.json`

Apps-tab registry — bat-based launchers only. Each row:

```json
{ "id": "...", "name": "...", "kind": "streamlit | webapp | tunnel | tray",
  "bat_path": "...", "added_at": "2026-...", "autostart": false }
```

`claude-code` projects are **not** stored here — the Coding tab
discovers them live by scanning `projects_dir` (minus `projects_ignore`).

Scan flow: tap **🔎 Scan** in Settings → `/api/apps/scan` returns a diff → checklist dialog → submit selections → `/api/apps/save` persists.

## Registered Trays (Apps tab)

A `tray` kind is surfaced by the same scan flow above — a
`.bat` file named exactly `tray.bat` whose body references the shared
`tray_lifecycle.ps1` helper (see `tray.bat`'s own header) is recognized as
a sister project's tray, not a plain streamlit/webapp/tunnel launcher.

Once scanned in, each `tray`-kind row is an **action-row** shaped like the
**Apps** bullet above, with the autostart switch as its one leading toggle.
It carries no visible label — the section is called Trays and it is the
row's only toggle — but its accessible name is the full
"Autostart &lt;name&gt; at boot". When app-launcher's tray starts (see
the Settings-tab boot toggle above), right after it tries to bring its own
webapp up, it walks every autostart-enabled tray one at a time. The walk
does not depend on that attempt succeeding: a webapp that misses its
ready-wait (a slow cold-boot import) writes the reason to
`webapp/registered_trays.log`, and the sister trays launch anyway.
They launch in the registry's existing
alphabetical order, no reordering UI yet — waiting for each to report
ready (via its `.fleet.toml`'s declared `port`, or a fixed delay if that's
missing) before starting the next. This avoids a boot-time CPU/disk spike
from launching several sister Python processes concurrently. One tray
failing to start doesn't block the rest. A tray launched this way is
started, not managed — `tray.bat --restart` on THIS machine never touches
another repo's tray process.
