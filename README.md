# 🚀 Launcher

Phone-first launcher hub. One tap on your phone → the home PC either:

- runs a coding agent — **Claude Code**, **Codex CLI**, **Antigravity CLI**, **GitHub Copilot CLI**, or **Pi** — in a project folder (**Coding** tab),
- spawns any registered Streamlit / FastAPI launcher (**Apps** tab),
- fires a one-shot Python script or scheduled job (**Jobs** tab — same trigger surface the Stream Deck and Task Scheduler use), or
- invokes a [`life-os`](https://github.com/ferraroroberto/life-os) productivity skill and browses what it knows about you (**Life OS** tab).

Sister project to [`photo-ocr`](https://github.com/) and [`voice-transcriber`](https://github.com/) — same FastAPI + SPA + PWA + Cloudflare-tunnel stack, but for kicking off other processes instead of doing work itself.

> Three ways to reach it from your phone:
> - **Local** (same Wi-Fi): `https://<pc-hostname>:8445`
> - **Tailscale** (anywhere): `https://<pc>.<tailnet>.ts.net:8445`
> - **Cloudflare named tunnel**: `https://launcher.<your-domain>` (no tailnet required)

---

## What it does, in one screen

The web UI has six tabs:

- **Coding** — opens with a one-line **summary head card** (issue #496, the fleet's vendored `home-head` component): app icon + title, live stats (`N sessions · M apps running · X dirty · Y off-main`), and the light/dark **theme toggle** pinned right — the same position as the other fleet apps. Below it, every project directory directly under your configured projects folder becomes a tile (no `.code-workspace` or `*-remote.bat` needed — the list is the directory listing, recomputed live; hide folders with a gitignore-style ignore list in Settings). The tile shows the **bare on-disk folder name** and carries **one launch button per coding agent**:
  - **Claude Code** (`claude`), **Codex CLI** (`codex`), **Antigravity CLI** (`agy`), **GitHub Copilot CLI** (`copilot`), and **Pi** (`pi`, on your Claude subscription via the Agent SDK) — each button bears the agent's icon. An agent's button is disabled with a hover hint when its CLI isn't installed (detection: the command resolves on `PATH`). See [Installing the Codex CLI](#installing-the-codex-cli), [Installing the Antigravity CLI](#installing-the-antigravity-cli), [Installing the GitHub Copilot CLI](#installing-the-github-copilot-cli), and [Installing Pi](#installing-pi) below.
  - A trailing **GitHub icon** opens the project's open-issues list, sorted by last updated, in a new browser tab — no process spawned, no session created. The repo URL is derived from the project's `origin` git remote; the icon is disabled with a hover hint when the folder has no GitHub remote.
  - Each launch has **two modes** chosen by the **☁️ Detached** toggle in the Projects card's header (issue #496 moved the launch-time toggles onto the launch surface itself). **Toggle off → full control:** the agent starts inside a **launcher-owned pseudo-console (ConPTY)** and the phone drops straight into a **live, fully interactive terminal** — real output, scrollback, typing, `Ctrl+C`, image paste. **Toggle on → detached:** the agent opens in its own console window on the PC; the launcher only *tracks* it (running-sessions list, killable from the phone) and it survives a launcher restart — including a full `tray.bat --restart` (issue #130).
  - A second toggle, **↺ Resume** (issue #151), reopens an existing conversation: with it on, the next agent tap launches that agent's **own native session picker** — `claude --resume`, `codex resume`, `copilot --resume` all show their list of recent sessions to pick from. (The launcher never builds its own session list; it only hands off to the agent's picker.) Antigravity has no picker flag, so its Resume **continues the most recent** conversation (`agy --continue`). Resume is **orthogonal to Detached** (issue #157): with Detached **off** the picker streams to the phone in a full-control terminal; with Detached **on** the picker renders in the **detached console window** on the PC (a real interactive console, so the list is pickable there) and the session shows as `☁️ detached` in the running-sessions list.

  - A **launch-model selector** (issue #540) sits in the Projects header beside those toggles: a compact **Sonnet / Opus / Fable** dropdown styled to match the Board tab's dispatch-bar model picker (a button + listbox rather than a native `<select>`, which WebKit's HTML parser can't survive inside a card `<summary>`) that stays in **sync** with the Claude Code **Model** segmented control in the ⚙️ options card — picking in either updates the other, since both read and write the same `claude_model`, so you never set the model twice. It's Claude-only (Codex is its own per-tile launch button; the Board's GPT-5.6 maps to Codex there, and Haiku stays a valid config value but isn't offered in the combo). To make room, **☁️ Detached** and **↺ Resume** render as compact **icon** buttons rather than text pills, keeping the same on/off shading.

  - Git state is **always on** (issue #496, reversing #115's on-demand contract): the launcher fetches `/api/claude-code/git-status` once at boot and re-polls every ~45 s while the Coding or Board tab is visible in a foreground page (paused when the PWA is backgrounded), so every tile carries its colour with no tap — **yellow** name = parked on a non-default branch (not a fresh start, with the branch name shown as a tag), **red** name = uncommitted changes (red wins when a project is both), with a tiny legend under the list. The aggregate (`2 dirty · 1 off-main`) also rides the summary head card. The **⎇ status** button above the sessions list remains as the drill-down: it re-fetches fresh state and opens the off-main popover (issue #139).

  - **★ Favorites** (issue #250) — each tile carries a **star toggle** (rightmost in its button strip): tap to pin the projects you actually work on. Favorites sort to the **top** of the list (alphabetical within the favorites group, then the rest alphabetically), so the default view always surfaces them first while keeping every project one scroll away. A **★ Favorites** toggle in the Projects header **filters the list down to only your starred projects** — one tap to hide the long tail when you just want the hot few, tap again to bring them all back. Favorites persist in `config/webapp_config.json` (`coding_favorites`, the same place as the ignore list — no extra file); the header filter is a client-side view that persists across reloads.

  Running sessions are listed above the project tiles, each marked with its agent's icon and tagged `⚡ full control` or `☁️ detached`. Each row carries a single **✕ Stop-and-kill** button (issue #253): one tap (no confirm) asks the agent to quit cleanly with its own command (`/quit`, Copilot's `/exit`) so its shutdown hooks run, waits briefly for the clean exit, then force-terminates as a fallback — and the window always closes. Tap a full-control one to re-attach; in the terminal view tap *‹* to come back, or the in-bar **✕** to stop and kill it right there without going back to the list first. The **⚙️ Coding options** card — now the **last card on the tab** (issue #496: configuration moves out of the way of launching; collapsible, collapsed by default) — has a Claude Code subsection (model / effort / permission mode / verbose / debug), an Antigravity subsection (`--dangerously-skip-permissions` / `--sandbox` toggles), and a GitHub Copilot subsection (a `--model` picker plus the `--allow-all` toggle). Antigravity has no launch-time model flag — pick its model with `/model` in-session. See [Interactive terminal](#interactive-terminal-from-the-phone) for the security model.

  A foldable **🗺️ System map** section (issue #173) sits below the project list (above the options card since #496). It surfaces the fleet system map — `architecture/system-map.png`, rendered by [`fleet-config`](https://github.com/ferraroroberto/fleet-config)'s `/system-map` job — so *"see my whole system"* is one tap from the phone, any time, instead of waiting for the weekly Slack image. The PNG loads lazily on first expand and opens full-screen (pan/zoom) on tap. The section hides unless a rendered map exists under the **Fleet-config dir** set in Settings (default sibling `../fleet-config`). The image endpoint is gated like the live terminal **minus the passkey** — bearer-token **and** Tailscale-only (refused over the Cloudflare tunnel) — so the map never leaves the tailnet.
- **Apps** — every `*.bat` under your scan root that the classifier recognises as Streamlit, a FastAPI webapp, or a Cloudflare-tunnel script. Tap → fresh CMD window runs the bat. Tunnel rows surface a live `📡 <url>` under the launch button, refreshed every 4 s.
- **Jobs** — one-shot Python scripts and scheduled jobs (`.py` or `.bat` targets). Every row reads as four fixed lines (name / type+schedule+countdown / duration percentiles + last-7 sparkline / last-run meta) so the same information lands in the same place across jobs. The list **defaults to Next-run order** (issue #229) — ascending by a next-fire time computed from each job's schedule, so the imminent dailies float above the weeklies and manual-only / paused jobs sink to the bottom — with a header toggle to flip to A–Z (the choice persists). Each scheduled row carries a relative **countdown chip** (`⏱ next in 3h`) next to its cadence chip; a concurrent execution is reported separately as `running now`, because a manual run does not consume the next scheduled fire. A foldable **🗓️ Schedule** panel above the list (issue #230, collapsed by default) shows the next 7 days of fires as a day-grouped agenda (`Today` / `Tomorrow` / weekday, each row `HH:MM · name · cadence`) — the mobile-native alternative to a 2D calendar grid; dense minutes/hourly jobs collapse to a "frequent" footer, and tapping a row reveals that job in the list below. Tap the row to expand recent run history and the most recent output tail; a live selection streams incremental output over WebSocket, while finalized logs load once. Jobs can preserve downloadable files by writing to `JOB_ARTIFACT_DIR`, and each run can be pinned to keep its record and artifacts outside normal retention. The card search box greps every indexed run output and jumps straight to the matching run. CPU and peak RSS surface on the selected run's output label. Tap the output pane itself to copy the whole log to the clipboard (issue #97) — one tap to grab an error trace for pasting elsewhere. Stuck runs (running > `max(p95 × 3, 300 s)`) get a ⚠️ marker and a "Kill stuck run" button. Failures can fire a Pushover push — optionally with an LLM-generated root-cause line — via `notify_on_failure` in `config/webapp_config.json`. A job can additionally be flagged **`alert_on_failure`** (issue #597) to push a Telegram alert on *that job's* failed runs only — opt-in per job (default off) so a shared Telegram chat isn't spammed by every failure; the Jobs tab marks it with a 🔔 bell icon next to the name. Schedules materialise as Windows Task Scheduler entries under the `\AppLauncher\` folder — same executor whether the run came from the phone, the Stream Deck, or the schedule. **Authoring safety** (issue #69): saving a job runs a pre-flight (missing script blocks the save; a `.py` with no `.venv` warns), edit mode adds a 🧪 dry-run check that resolves the invocation without spawning (plus a *Dry-run* checkbox in the run dialog that runs with `JOB_DRY_RUN=1`), and a job can be flagged to require confirmation before firing. A job can also be flagged **`visible`** (issue #91) so its scheduled fire runs in a real console window (under `python.exe` instead of the silent `pythonw.exe`) with the child's output teed to that console as well as `output.log` — for jobs you want to watch run on the PC while still capturing output for remote run-history. A job can be flagged **`elevated`** (issue #350) for a script whose target needs admin rights, e.g. restarting an app that requires elevation to launch — its real Task Scheduler entry (registered by hand with `/RL HIGHEST`, silent elevation, no UAC prompt) is externally managed. The Jobs tab marks it with a `🔒 external schedule` pill, keeps history available, and omits Run-now and pause/resume controls because the non-elevated launcher cannot perform those actions safely. A job can also be **webhook-target** (issue #73): an external service (GitHub, Stripe, or a generic POST) fires it over `POST /api/jobs/<id>/hook`, gated by its own provider signature instead of the app's bearer token, with a small JSONPath mapping turning the payload into the job's typed params — the Jobs tab marks it with a `🪝 <provider>` chip. See [Jobs tab](docs/jobs-tab.md) for the full reference.

- **Life OS** — one tile per skill in your [`life-os`](https://github.com/ferraroroberto/life-os) checkout (the directories under `<life_os_dir>/.claude/skills` whose name doesn't start with `_`, listed live and alphabetically — a new skill folder appears with no restart). Where the Coding tab answers *"run a coding agent in project X"*, this answers *"invoke productivity skill Y, ready for me."* Pinned above the skill list, a **📓 Weekly recap** tile carries a **staleness badge** driven by the mtime of life-os's `_recap/memory/ledger.json` — green when fresh, amber past 7 days, red past 14, plus a *"draft ready"* hint when a headless draft awaits review — and a **🚀** that launches `/weekly-recap` (the interactive review). The *drafting* half runs headless on a schedule: a weekly **Jobs** entry (`config/jobs.sample.json` → `weekly-recap-draft`, Sun 21:00) runs `life-os/.claude/skills/_recap/run-weekly.bat`, i.e. `claude -p "/weekly-recap draft"` (issue #167). Each skill tile shows just the skill name and carries two buttons: **🚀 Launch** fires a fresh Claude session cwd'd in `life-os` that auto-invokes the bare `/skill-name` (no free text is injected — you type your input into the live terminal once the skill reports ready), and **📖 Browse** opens a read-only viewer of what that skill knows about you (a full-screen file list; tapping a file opens it full-screen, with a **✕** in the bar to close it back to the list — and, when the open file is a disposable conversation log, a **🗑️** in the bar to delete it after a confirm, dropping you back to the list). The launch controls sit in the **Skills card's header** (issue #496 round 2 — the separate options card is gone; same structure as the Coding tab's Projects card, with the 📓 Weekly recap tile leading the tab): a **model selector** — a compact board-style **Sonnet / Opus / Fable** combo (issue #540, replacing the old on/off opus toggle; life-os skills are Claude `/skill` commands, so the Board's GPT-5.6 option is deliberately absent) — plus **☁️ Detached** (identical semantics to the Coding tab) and **↺ Resume** (issue #151), the latter two now compact **icon** buttons (issue #540, ☁️ / ↺) so the model combo fits. Resume **drops the `/skill-name` prompt** and opens Claude's native session picker by starting the Remote Control session first and invoking `/resume`, so the selected conversation remains available from Claude mobile/web. Like the Coding tab, Resume is **orthogonal to Detached** (issue #157, fixed for Life OS in #239): with Detached **off** the picker streams to the phone in a full-control terminal; with Detached **on** it renders in the **detached console window** on the PC (a real interactive console, so the list is pickable there) and the session shows as `☁️ detached`. Every other Claude flag (effort, permission, verbose, debug) comes from the shared **⚙️ Coding options** card. Launched sessions appear in the Coding tab's running-sessions list, re-attachable and killable like any other. **The 📖 Browse viewer is gated harder than the rest of the app** — it surfaces the skill's private, gitignored knowledge (`context/`, `memory/`, `examples/`, `conversations/`, plus the shared `identity/`), so its content endpoints are **Tailscale-only, refused over the Cloudflare tunnel, and passkey-gated** (the same gate as the live terminal), and the file-content endpoint is path-jailed to `life_os_dir`. Read-only in v1. See [Interactive terminal](#interactive-terminal-from-the-phone) for the gate.

- **Board** (issue #164, complete: #300 + #301 + #302 + #399) — one screen answering *"what needs me now, across everything"*: a read-only kanban over five **computed** columns, each holding one kind of card (a card moves because reality changed — there is deliberately no drag-and-drop). **Backlog** = open GitHub issues across the configured owner's repos; **Claude's turn** = live coding sessions that are working / idle / unknown; **Your turn** = sessions waiting on your input, and nothing else; **Other** = open PRs and today's failed or stuck job runs — everything that needs attention but isn't a terminal; **Done** = today's closed issues (a merged PR that closed one is already reflected there, so Done never shows PRs). On the phone the columns are a swipeable one-column-per-screen carousel with a count strip on top (the *Your turn* count highlights when nonzero); desktop shows all five side by side, each column's header carrying its own `(N)` item count too (issue #603). The session-host list is authoritative for launcher-owned presence and agent identity; [`fleet-config`](https://github.com/ferraroroberto/fleet-config)'s sessions-state file is only a semantic overlay, claimed by exact launcher id + agent when available and otherwise by an agent-aware cwd fallback. Legacy rows are Claude-only, so Codex/Pi can never borrow their state; an unsupported agent state renders truthfully as `unknown`. Unmatched external rows render only while an existing transcript has recent activity, preventing missing cloud/bridge transcripts and hard-kill leftovers from claiming work for 24 hours. GitHub data is `gh`-fetched server-side and cached (refreshed only by the ↻ button or on tab open, never on the 5 s poll). Backlog cards are **colour-coded by their repo's git state** (issue #496: red = dirty tree, yellow = off its default branch — "don't start this issue right now"), fed from the same client-side git-status cache as the Coding tiles, never from the board's 5 s poll. A separate shared `active-issues.json` lifecycle marker (issue #528; written by fleet-config's issue workflows) gives work already in flight an accent tint plus an explicit “in progress” label and disables both Start/YOLO actions; missing, corrupt, or older-than-24-hour markers degrade to an ordinary actionable card. Tapping a live session opens an inline **drill-down drawer** — the last exchange, a full-width reply box on its own line, then one right-aligned row of canonical 44px buttons: 🎤 dictate, ➤ send, icon-only ✏️ **Rename**, **✕ Stop** (kills a live PTY session straight from the Board via the unified stop path, #253), and **⚡ Terminal** last. Its conversation preview is agent-aware: structured Claude/Codex history wins when it correlates safely, with the launcher's exact-session PTY capture as the fallback when a hook transcript is missing; all parsing stays on the drawer request, never the 5 s poll. A pinned **dispatch bar** spawns a brand-new session from a spoken or typed goal (injection-safe by spawn-then-type); both are Tailscale-only + passkey-gated as terminal-grade content. The bar's mode dropdown — `add`/`build`/`yolo` plus a fourth value, **chat** (issue #245; collapsed from a 4-segment radiogroup into a `<select>` in #547 once it stopped fitting an iPhone-width row) — reroutes it to a standing **fleet chief** — a conversational orchestrator you can ask *"what's open in app-launcher?"* by voice and then direct (*"ok start 229"*) in the same conversation. The chief is a normal PTY session (crown-marked accent-tinted card, confirm-before-kill — consistently in the Board tab, the Coding tab's session list, and the terminal overlay's own header, #547) spawned in the fleet-config checkout so its brain — the `/chief` skill versioned there — and the fleet-only skill tier load while app-launcher's own context never does; it comes back via lazy ensure on first message, a daily fresh-respawn job (`chief-daily-respawn`, default 05:00), or a manual Start button (Restart when one is already alive, #617 — a graceful stop-then-respawn that lets the #442 handover log carry context to the fresh session, never a session-host restart), and a ⚙️ gear beside the chat bar edits its settings (model, respawn on/off + time, worker cap) in place. A chief spawned outside `ensure` (a manually typed `/chief`, or a Resume into a fresh PTY after a session-host restart) self-heals its `label` read-time from either the PTY's first submitted line or Claude Code's own persisted conversation identity — so the Board, the worker cap, and `fleet-config`'s `chief-sid` lookup never lose track of it. See [Board tab](docs/board.md) for the full reference — the columns and their data sources, the agent-aware session-state join, the transcript overlay (#305 + #309), the conversation-source hierarchy (#457), the dispatch spawn-then-type contract (#302), and the drill-down drawer's PTY-write path (#301).

The **Apps** tab is backed by a registry file (`config/apps.json`); the **Jobs** tab by `config/jobs.json`. The **Coding** and **Life OS** tabs need no registry — they list directories live. The sixth tab, **Settings** (issue #383 — previously an always-visible panel at the bottom of every tab), holds the occasional-use actions: **🔎 Scan** walks the apps scan root and shows what's new in a checklist; it's where you set the Coding projects folder and its ignored-folders list. (The light/dark **theme toggle** lives in the Coding tab's summary head card — issue #496.) **Edit mode** there reveals per-row ✏️ rename and 🗑️ remove on Apps rows plus the **➕ Add job** button (in the Registered-jobs panel header) + 🧪 dry-run / ✏️ / 🗑️ controls on Jobs rows (▶ run and ⏸ pause stay in the normal view) — off by default, so the lists stay icon-free in normal use. The same switch is also mirrored as a ✏️ toggle in the **Registered-jobs panel header** itself (issue #70 UX round), so turning on editing doesn't require a detour through Settings; both buttons flip the one shared state. Every top-level panel across the four original tabs is a **collapsible section** (issue #226) sharing the Code tab's chrome — same chevron, same collapsed height — so the Apps (Running apps / Port listeners / Registered apps), Jobs (Registered jobs) and Life (Skills) panels each fold away to cut scrolling on the phone. Defaults (#383 review round): the working-set panels (Running sessions, Running apps, Registered jobs, Skills) open; the long occasional lists (Projects on the Code tab, Port listeners, Registered apps) collapsed.

Smart-kill: the Apps tab's Port-listeners panel polls common app ports (8443, 8444, 8445, 8501, 5050) and lists what's actually listening. One tap stops the right PID — no hardcoded "kill :8501" buttons that fire blind. A parent app that owns dependent helper services (issue #224 grouping) keeps its child rows collapsed behind a tap on the parent row (issue #480) — the chevron marks the rows that expand; killing works from the collapsed parent or any expanded child.

---

## Install

```powershell
cd app-launcher
.\setup.bat
```

That creates `.venv`, installs deps, and generates the PWA icons. After this runs once, `tray.bat` is enough for day-to-day use.

If you came from the old `automation\launcher\` Flask version, your apps list and Claude flags survive — copy `automation\launcher\apps_config.json` → `app-launcher\config\apps.json` and `automation\launcher\config.json`'s contents into `app-launcher\config\webapp_config.json` under the matching `claude_*` keys.

### Installing the Codex CLI

The Coding tab can launch the **Codex CLI** (`codex`) — OpenAI's Rust terminal
coding agent — as well as Claude Code. The tab's Codex button stays disabled
until `codex` is on `PATH`. Install it with npm (needs Node.js 22+):

```powershell
npm install -g @openai/codex
```

A standalone installer and Homebrew tap are also offered — see the
[official docs](https://developers.openai.com/codex/cli) for the channel that
suits you. Verify with `codex --version`.

> **Authentication is not the launcher's job, and no API key is needed.** Sign
> in *inside the session* — run `codex login` (or the in-session login flow) and
> pick **Sign in with ChatGPT** so launches draw on your ChatGPT-plan quota
> rather than API-key billing. The launcher only resolves the `codex` binary on
> `PATH` and spawns it.
>
> Codex has no Claude-style model tiers — the Coding-options **Reasoning**
> selector (Low / Medium / High) maps to its reasoning effort, and the model
> stays the account default (`gpt-5-codex`). The **Permission** selector mirrors
> Claude's: *Auto mode* runs with no prompts but keeps the sandbox; *Skip
> permissions* is the all-bypass switch.
>
> Like `agy`, `codex` is detected on `PATH` at process start — after installing
> it, **restart the tray** (`🔄 Restart webapp` only refreshes the `:8445`
> webapp; the `:8446` session-host that spawns `codex` needs a full tray restart
> to inherit the new `PATH`).

### Installing the Antigravity CLI

The Coding tab can launch the **Antigravity CLI** (`agy`) — Google's Go-based
terminal coding agent — as well as Claude Code. The tab's Antigravity button
stays disabled until `agy` is on `PATH`. To install it:

```powershell
irm https://antigravity.google/cli/install.ps1 | iex
```

The official installer downloads `agy.exe` (checksum-verified) to
`%LOCALAPPDATA%\agy\bin\`, adds that folder to your **User PATH**, and the CLI
self-updates in the background thereafter. Verify with `agy --version`.

> **Not** `winget install Google.Antigravity` — that package is the Antigravity
> *IDE* (a desktop app), not the `agy` terminal CLI.
>
> The launcher detects `agy` on `PATH` at process start. After installing it,
> **restart the tray** (`🔄 Restart webapp` only refreshes the `:8445` webapp —
> the `:8446` session-host, which actually spawns `agy`, needs a full tray
> restart to inherit the new `PATH`). A bare `tray.bat` re-run is a no-op when a
> tray is already alive — use `tray.bat --restart` to stop the running tray and
> its tree (webapp, session-host, cloudflared, **any full-control Coding
> sessions**) and start a fresh one. **Detached (☁️) sessions survive** the
> restart — they are deliberately orphaned out of the tray's process tree so
> the `taskkill /T` teardown can't reach them (issue #130).

### Installing the GitHub Copilot CLI

The Coding tab can also launch the **GitHub Copilot CLI** (`copilot`) — GitHub's
terminal-native agentic coding agent. The tab's GitHub Copilot button stays
disabled until `copilot` is on `PATH`. Install it with WinGet:

```powershell
winget install -e --id GitHub.Copilot
```

It is also available via npm (`npm install -g @github/copilot`, needs Node.js 22+)
and a standalone installer — see the [official docs](https://docs.github.com/copilot/how-tos/set-up/install-copilot-cli)
for the channel that suits you. Verify with `copilot --version`.

> **Authentication is not the launcher's job.** The Copilot CLI signs in
> *inside the session* — run `/login` at the `copilot` prompt and follow the
> on-screen instructions; it needs an active GitHub Copilot subscription. The
> launcher only resolves the `copilot` binary on `PATH` and spawns it.
>
> Like `agy`, `copilot` is detected on `PATH` at process start — after
> installing it, **restart the tray** (`🔄 Restart webapp` refreshes only the
> `:8445` webapp; the `:8446` session-host that spawns `copilot` needs a full
> tray restart to inherit the new `PATH`). Use `tray.bat --restart` for that;
> a bare `tray.bat` is a no-op when a tray is already running.

### Installing Pi

The Coding tab can also launch the **Pi coding agent** (`pi`), driven by your
**Claude subscription** through the Claude Agent SDK *or* your **ChatGPT plan**
through pi's `openai-codex` provider — **no API credits** either way. The
tab's Pi button stays disabled until `pi` is on `PATH`. Install the CLI and the
SDK provider extension (needs Node.js):

```powershell
npm install -g --ignore-scripts @earendil-works/pi-coding-agent
pi install npm:claude-agent-sdk-pi
```

Verify with `pi --version`, `pi --list-models claude-agent-sdk` (lists
`claude-opus-4-8`, `claude-sonnet-4-6`, etc.) and `pi --list-models openai-codex`
(lists `gpt-5.5`, etc.). The Coding **options** card's *Pi* block then offers
segmented **model** (Opus / Sonnet / GPT), **effort** (low / medium / high,
default high → `--thinking`), and **project-trust** controls.

> **Why the SDK extension is required.** Pi's *native* `anthropic` provider
> bills metered API "extra usage" credits, **not** your subscription — so the
> launcher launches the Claude models as
> `pi --provider claude-agent-sdk --model claude-agent-sdk/<model>`, which routes
> through the Claude Code subscription quota instead, and the GPT option as
> `pi --provider openai-codex --model openai-codex/gpt-5.5` (your ChatGPT-plan
> login). Don't set `ANTHROPIC_API_KEY`. Authenticate the Claude subscription
> once with Claude Code (`npx @anthropic-ai/claude-code`, or your existing
> Claude Code login), and the ChatGPT plan once via pi's `openai-codex` OAuth.
> The native `anthropic` OAuth is left disconnected so a launch can never slip
> onto the billing path. The **project-trust** control maps to pi's
> `--approve`/`--no-approve` (whether pi loads project-local `.pi/` resources) —
> it is **not** a tool-permission gate, as pi ships no sandbox. Switch models
> and effort inside the session with `/model` / `Shift+Tab`. Details:
> [`docs/pi-coding-agent.md`](docs/pi-coding-agent.md).
>
> Pi is detected on `PATH` *and* registered in `src/agents.py`, which the
> `:8446` session-host imports at start — so after installing Pi (or upgrading
> the launcher to a build that adds it), a `🔄 Restart webapp` / `tray.bat
> --restart` is **not** enough: the session-host that spawns `pi` needs a
> **full tray restart** (which ends open Coding/PTY sessions) before the Pi
> button works, otherwise the launch fails with `unknown agent: pi`.

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

### Confirming which build the phone is running

Every `/static/*.{js,css}` URL carries a content-hash query string (`?v=<8 hex>`) computed at boot, so editing any asset busts iOS Safari's cache automatically — no more "did the deploy take?" guessing. Hashed assets are served with `Cache-Control: public, max-age=31536000, immutable`; `index.html` itself stays `no-cache, must-revalidate`.

To verify visually, the footer under every tab shows a build line:

```
Build: 35caad4 · 2026-05-19 21:34
```

- **`git_sha`** — `git rev-parse --short HEAD` at the moment the webapp process started. Changes only across commits.
- **`built_at`** — process start time. Changes on **every** restart, even with no code change — useful as a "did the tray actually restart?" anchor.

Backed by `GET /api/version`, which also returns the current `asset_hash` for quick diff against the PC. The line updates only when the webapp module re-imports (i.e., tray restart or 🔄 Restart webapp) — a phone refresh alone won't move it.

`GET /api/version` also reports a `session_host` block (`{"reachable", "git_sha", "started_at", "stale"}`, issue #615): the session-host on `:8446` is deliberately excluded from `tray.bat --restart`'s reclaim sweep to protect live PTYs (project-scaffolding#35), so it can keep running code that's days old with nothing else surfacing that. `session_host.git_sha` is the SHA that process loaded at *its own* start (not live git state); `stale` is `true` when that differs from `head_sha` (this repo's current `HEAD`, resolved fresh on every call), and `null` — never a confident false — when either SHA can't be resolved. See `CLAUDE.md`'s restart section for what a `stale: true` session-host means for shipping and the one supported way to restart it (`scripts/restart-session-host.ps1`).

### If the webapp stops answering

The tray runs a health watchdog: every 60 s it round-trips `GET /healthz` on its own webapp — a wedged uvicorn still *listens*, so only a real response proves it's alive. After 3 consecutive failed probes it raises a Windows toast and appends a timestamped line to `webapp/watchdog.log`; the first successful probe afterwards logs the recovery. Recovery stays manual and canonical: `tray.bat --restart`.

The webapp itself leaves request-level breadcrumbs in `webapp/slow-requests.log` (its stdout is discarded by the tray, so these are file-only): a line for any request slower than 3 s (`LAUNCHER_SLOW_REQUEST_S`) with method, path, status, elapsed and in-flight count, plus a rate-limited warning whenever more than 16 requests (`LAUNCHER_INFLIGHT_WARN`) are in flight at once, naming the oldest. Together with the watchdog timestamps, the next hang can be classified (event-loop blocked vs deadlocked handler vs socket exhaustion) without a live repro.

The root cause of the recurring wedge (#388) was asyncio's default Windows proactor event loop closing its listening socket on any aborted client connection (WinError 64 — a dropped Wi-Fi handoff, a browser tab closed mid-handshake). Every `app.webapp.server:app` uvicorn invocation now runs on the selector event loop instead (`app/webapp/event_loop.py`), whose accept path doesn't have this failure mode — the webapp process spawns no in-process asyncio subprocesses, so the selector loop's lack of subprocess support doesn't apply here. The watchdog + breadcrumbs above remain the detection net in case this regresses.

---

## Phone install (PWA)

The launcher is a PWA — installs to the iPhone home screen, full-screen, no Safari chrome. With the [Tailscale cert](#https-certificate-tailscale) there is no trust setup at all:

1. Open `https://<host>.<tailnet>.ts.net:8445?token=…` in Safari (tray menu → **📋 Copy Tailscale URL**). Lock icon should be solid, no "Not Secure".
2. **Share → Add to Home Screen**. The launcher rocket icon lands on your home screen.

On Android, Chrome shows an "Install app" prompt the second visit; the icon goes on the home screen the same way.

After that the launcher behaves like a native app — full-screen, no Safari chrome.

> **Debugging the phone:** when the [pre-ship gate](#verifying-changes-before-ship) is green but the iPhone still misbehaves, [`docs/iphone-debugging.md`](docs/iphone-debugging.md) walks through attaching PC DevTools to the live phone via `ios-webkit-debug-proxy`.

---

## HTTPS certificate (Tailscale)

Fleet standard: `ferraroroberto/project-scaffolding#89`. Provision a **real Let's Encrypt cert** via `tailscale cert` — no self-signed CA, no per-device trust dance (the legacy self-signed generator and its `/install-ca` iOS-profile detour were removed in #383):

```powershell
.\.venv\Scripts\python.exe scripts\gen_tailscale_cert.py
# then: tray.bat --restart
```

One-time prereq: enable **DNS → HTTPS Certificates** in the [Tailscale admin console](https://login.tailscale.com/admin/dns). The script auto-detects the MagicDNS name and writes `webapp/certificates/cert.pem` + `key.pem`. Every device on the tailnet then trusts `https://<host>.<tailnet>.ts.net:8445` natively — no CA install, no profile, no Certificate Trust toggle.

**Renewal is automatic.** The LE leaf lives ~90 days, so every uvicorn-boot path (`tray.bat` via the webapp manager, `webapp.bat`, `run_named_tunnel.py`) runs `gen_tailscale_cert.py --check` first, which renews only a `.ts.net` cert expiring within 30 days and no-ops on any other cert. No calendar entry needed.

> **Loopback and LAN URLs:** the Tailscale cert is issued *only* for the ts.net name, so `https://127.0.0.1:8445` and LAN-IP URLs show a hostname-mismatch warning by design — open the launcher via the ts.net URL on the PC too. The **PC mirror windows adapt automatically** (#356): with a Tailscale cert active they open the ts.net URL carrying their own credentials (`?token=` bearer bootstrap + a server-minted `?tt=` terminal token when the passkey gate is configured); with no Tailscale cert they keep the loopback URL and its auth bypass. The Cloudflare tunnel (`noTLSVerify`) and the e2e suite are unaffected either way. With no cert at all the server runs plain HTTP on loopback — fine for a fresh clone, but iOS Safari needs HTTPS for the PWA + mic features, so provision the Tailscale cert before phone use.

---

## Interactive terminal from the phone

The loopback-only session-host also exposes the same ConPTY/WebSocket engine to trusted sibling services. A caller can `POST http://127.0.0.1:8446/sessions` with `{"kind":"pty","agent":"ssh","flags":"user@host","project_dir":"<existing-dir>","cols":120,"rows":30}` to start `cmd /c ssh user@host`; the returned `session_id` uses the existing `/sessions/{id}/ws` input/resize/raw-output/shutdown protocol. SSH is deliberately session-host-only and does not appear as a Coding-tab agent.

Launching a Coding-tab project in **full control** mode (the default — the ☁️ Detached toggle off) opens a **live terminal** — the same thing you'd see in the CMD window on the PC, streamed to the phone: real output, scrollback, typing, `Ctrl+C`, `/quit`, and image paste. This works the same for any coding agent (Claude Code, Codex CLI, Antigravity CLI, GitHub Copilot CLI, or Pi). Tap a `⚡ full control` session in the list to re-attach.

A full-control launch — from the phone **or** from a desktop browser on the PC — opens the terminal in a **dedicated Edge `--app` window on the PC**, not inside the launching browser, so it closes independently when you stop the session, without touching your other tabs (issue #241). **Tapping a row in the running-sessions list re-opens the session the same way:** on the **phone** it streams *in-page* as an ordinary terminal overlay (stopping it just dismisses the overlay — never a browser window), while on a **desktop browser** it opens that dedicated PC Edge window too — or, if one is already open for that session, focuses it rather than spawning a second — so you can close it without fear while the session keeps running headless (issue #282). Either way, **stopping the session closes its mirror window** (issue #20). Each running-session row and PC window is **named from the conversation** (issue #266, extended #396, #458): a **manual rename** — tap the ✏️ next to a Coding-tab row, or the **Rename** button in a Board card's drawer — always wins first if one is set; it is the one title channel that works identically for every agent, including detached sessions, since it doesn't depend on any agent-native self-naming support (submit a blank title to clear it and revert to the automatic precedence below). The rename is a launcher-side title override only — it renames the session everywhere the launcher shows it (the row, the PC window, the Board) but is deliberately **not** typed into the agent's own CLI: forwarding it as a native `/rename` (issue #503) proved unfixably racy against the live TUI — its `Esc` interrupted the agent's active turn and its submit often failed, leaving the command stuck and duplicated in the prompt — so it was removed in issue #555 (revisiting a reliable `--resume`-picker sync is tracked separately). Absent a manual rename: a genuine shared title from `fleet-config`'s `session_state` hook (Claude Code's own `/resume`-picker title, joined in by cwd — the same cross-tab source the Board tab's session cards use, so the same session shows an identical title on both) wins next; otherwise the agent's own live per-conversation title when it emits one (Claude Code's evolving summary — kept as a same-poll-cycle-faster supplement to the shared title), then a short title derived from your **first prompt** — so two sessions in the same project folder stay distinguishable instead of both reading as the folder name. The agents differ here — only Claude self-names per conversation; Codex/Pi emit just the folder, and Antigravity/Copilot none — so the first-prompt fallback fills the gap, and a manual rename is the one way to fix a title that never settles into something useful. The PC window's title bar shows the resolved name (with a hidden `app-launcher-mirror-<sid>` marker kept in the title for the launcher's close/cleanup scan), and it updates live if the title later changes. When the same session is open on both phone and PC, the **phone drives the terminal size** and the PC window mirrors it — one ConPTY has one size, so the phone is the single authority and the two never fight over dimensions.

For a Board session whose issue title is known **before** the agent starts, the launcher also supplies that title at spawn through the agent's documented `--name` flag when available (currently Claude Code, Copilot, and Pi). This syncs the native resume picker without interacting with a live TUI. Agents without that interface (Codex and Antigravity), and titles unsafe to pass through `cmd.exe`, retain the launcher-only name.

**Terminal toolbar**

A small toolbar sits under the terminal for the things a phone keyboard can't do well:

- **⌨️ Keys** — a popover D-pad of arrow / `Esc` / `Tab` / `Enter` keys for iPhone keyboards (SwiftKey etc.) that lack them, so Claude's TUI prompts stay navigable (#36). Includes a sticky **`⇧` Shift** toggle (#137): tap it to hold Shift (it lights up), then `Tab` sends **Shift+Tab** — how Claude Code cycles permission modes (auto-accept edits → plan → dangerous-skip-permissions). It stays held across taps so you can chain the cycle; tap it again or close the popover to release.
- **✏️ Compose** — toggles a slim predictive `<textarea>` above the keyboard. xterm.js wipes its own helper textarea on every keystroke, so iOS/Android autocomplete can't suggest there; the compose bar is a plain textarea, so it can. `➤` Send forwards the buffered text + `Enter` to the PTY in one frame. Hidden in the PC mirror window (#37).
- **📋 Paste** — reads the clipboard. Bar **closed**: sends straight to the PTY. Bar **open**: drops the text into the textarea at the caret for review before Send.
- **🔊 Read aloud** (#190, #197, #203, #206, #210) — a top-bar control **between ↓ Jump and 📋 Paste** (not in the compose bar — that's for editing), the eyes-free other half of dictation for driving / walking. Tap to hear the agent's **last reply** spoken back — the final answer or the question it's asking you — so you can keep the phone in your pocket: dictate → send → 🔊 → listen → dictate again. The reply is lifted client-side from the xterm scrollback, which on the phone is a raw TUI of redraws, boxes and a live status footer — so detection keys off the same signal the Claude Code mobile app uses to separate reply text from tool output: the **filled bullet `●`** that opens every block, classified by its **terminal colour** (#197). A `●` in the **default / white** foreground is an **assistant reply**; a `●` in a **saturated colour** (green / red / …) is a **tool call** (Bash / Read / …). The colour is read straight from the xterm cell (the `translateToString` text drops it), so the buffer segments cleanly into an **ordered list of reply blocks** with no boundary-walk guessing — and `🔊` reads the **last** one by default (a future "read last N" depth-selector is just a slice of that list). The leading `●` is stripped and the phone's 51-column wraps are de-wrapped into one paragraph. The only residual filter is the per-turn epilogue the TUI prints *below* the final reply, which carries no bullet and so trails the last block: the block truncates at the first `recap:` line, **per-turn timing line** (`✻ Crunched for 5s …` / `Worked for 21m 17s`), **live thinking spinner** (`✻ Cogitating… (4m 39s · thinking)` — even the no-token form, #193) or the spinner's **`⎿ Tip:` hint** (#195) — all matched by shape, not verb, since Claude Code picks a random gerund. The composer box + status footer (folder/branch, permission mode, token count) are dropped wholesale. If the agent is mid-work with no completed reply anywhere it says nothing. When the reply finishes reading it resets the button and pops a **🔊 Finished reading** toast (with a watchdog backstop because iOS fires the speech-`end` event unreliably). Speaking has **two voices behind one button** (#203): when the sibling [`local-llm-hub`](https://github.com/ferraroroberto/local-llm-hub) is reachable, the reply is synthesized through its high-quality **Orpheus** voice (default `tara`). For low time-to-first-audio (#206) it plays **progressively, as the hub synthesizes** (first audio in ~1–1.5 s) — `POST /api/tts/speak` streams the reply as **headerless PCM16** (`audio/L16` + an `X-Sample-Rate` header) and the browser plays it through the **Web Audio API**: read the streaming fetch, convert each int16 chunk to float32, and schedule `AudioBufferSourceNode`s back-to-back on an `AudioContext` resumed in the tap gesture. (This is the technique the hub's own TTS UI uses; an `<audio>` element can't play the hub's open-ended streaming WAV progressively — it just buffers silently — so Web Audio sidesteps the container entirely.) The loopback-only hub never has to be reachable from the phone directly. When the hub is unconfigured, down, or lacks Web Audio, it falls back to the browser's built-in **Web Speech API** (`speechSynthesis`) — on-device, zero server, the iOS Siri-enhanced voices when installed. The button shows when the hub is configured (`state.status.tts`) **or** Web Speech is supported, and a live `GET /api/tts/health` probe decides which path the tap takes; the `/api/tts/speak` stream carries the live terminal's gate (Tailscale-only + passkey — the text is terminal content), while the health probe stays token-only. When the hub is reachable 🔊 becomes a small **dropdown** (#210): **Read aloud** speaks the reply verbatim, while **Summarize & read** first sends it to the hub's cheap `claude-haiku-4-5` for a short, driving-oriented summary — the essence plus any decision you need to take — then shows the summary in a **modal** and reads *that* aloud through the same Orpheus-then-Web-Speech path (the summary `POST /api/tts/summarize` carries the same terminal gate). The modal is readable on its own (so summarize doubles as a quick on-screen digest when you can't play audio) and **auto-closes when the read finishes** — tap it (or ✕) to dismiss early and stop. iOS autoplay needs the audio context to be **user-activated**, and the real audio only arrives after the LLM round-trip, so the tap gesture both arms *and* unlocks the context with a silent sample up front (then `resume()`s again before the audio) — without that the context is created in the gesture but stays muted by the time the summary is ready. With the hub unreachable the menu is suppressed and 🔊 keeps its original single-tap read-aloud. Tap again (or starting a new dictation, or leaving the tab) stops the read-aloud — whichever voice is playing. Hidden only when neither voice is available. Configure the hub URL with `llm_hub_url` (empty disables the hub path).
- **🎤 Dictate** (#165, #168) — lives **inside the compose bar** (beside ➤ Send), so dictation always goes through review-before-send and never streams raw into the PTY. Tap to start recording the mic, tap again to stop; the text drops into the textarea at the caret for editing before Send. While you speak it **streams live** (#168) — audio is chunked to the sibling [`voice-transcriber`](https://github.com/ferraroroberto/voice-transcriber) at a 1 s cadence and a Server-Sent-Events stream of rolling partial transcripts revises the dictated span in place, settling on the canonical text when you stop (so a long note is recoverable on the PC even if the phone dies mid-record). If streaming setup fails it falls back to a single-shot upload of the whole take. The phone never talks to the transcriber directly — the webapp proxies everything over loopback to its consumable session API. Gated exactly like the live terminal (Tailscale-only + passkey). Hidden when `voice_transcriber_url` is unset or the browser lacks `MediaRecorder`.
- **📷 Screenshot OCR** (#171) — lives **inside the compose bar** beside 🎤 Dictate (stacked vertically so the textarea keeps the width), the pixel counterpart to dictation. Tap 📷 to **stage** screenshots into a tray above the bar — tap again to add more, ✕ to drop one. Then tap **Extract text (N)**: all staged images go to the sibling [`photo-ocr`](https://github.com/ferraroroberto/photo-ocr) in **one** call (`POST /api/extract`), so it **collates them into a single deduplicated text** (overlapping shots of one long document are merged, duplicate boundary lines removed — staging is what makes the de-dup possible, vs. one isolated OCR per image). The text drops into the textarea for review before ➤ Send. The Extract button shows a ⏳ elapsed-seconds timer while the hub works. Unlike 🖼 Image (which pastes a file *path*), this pastes the *text read out of the pictures*; model/prompt are photo-ocr's own defaults. The phone never talks to photo-ocr directly. Hidden when `photo_ocr_url` is unset.
- **🖼 Image** — uploads one or more phone images (#448: the picker is multi-select, so a single gallery tap can pick several screenshots at once). A terminal can't hold an image, so each file is saved on the PC and Claude is handed its **file path** (Claude reads the image from that path). Uploads happen sequentially; a multi-pick fires one summary toast instead of one per file. Bar **closed**: each path is pasted straight into Claude's prompt. Bar **open** (`?inline=1`, #41): the session-host skips the paste and returns the path, which is inserted into the textarea — so several images + text can be composed and sent together, each on its own line.

**How it's wired**

- A separate long-lived **session-host** process (loopback-only, port `8446`) owns every `claude` ConPTY. The tray starts and owns it like it owns `cloudflared`. Because it's its own process, a *Restart webapp* doesn't kill running sessions (a PC reboot still does).
- The webapp proxies a WebSocket from the phone through to the session-host. The webapp is the single auth choke point.
- `xterm.js` renders the terminal in the SPA — no build step, vendored under `app/webapp/static/vendor/`.

**Security model — the terminal is not the same as the launcher**

Launching, listing, and stopping sessions stay public (bearer-token gated, reachable over the Cloudflare tunnel). The **live terminal itself does not**:

- **Tailscale-only.** The terminal WebSocket, image upload, and WebAuthn endpoints refuse any request that arrived over the public Cloudflare tunnel (they're rejected on the `Cf-Ray` header) and require a client IP in the Tailscale CGNAT range `100.64.0.0/10` (plus loopback, plus an optional `tailnet_allowlist`).
- **Passkey-gated.** When `webauthn_rp_id` + `webauthn_origin` are set, opening or driving a terminal requires a **WebAuthn platform passkey** — Face ID on the enrolled iPhone. A passkey assertion mints a short-lived (12 h) terminal token; the WebSocket and image endpoints require it.
- **Device whitelist you control.** Enrolled passkeys live in `config/webauthn_devices.json` (gitignored). Enrollment only works during a one-time window you open deliberately from the tray (**🔐 Enroll device** — 5 minutes). Revoke a device by removing its entry from that file (the Settings passkey section was removed in #383's review round — the gate runs headless; the `/api/webauthn/*` endpoints are unchanged).
- **Audited.** Every terminal action is logged: `webapp/terminal_audit.log` (enroll / unlock / session lifecycle, device, client IP) and per-session `webapp/sessions/<id>.log` (input chunks, image uploads) + `<id>.transcript` (full output).

> The Claude Code launch runs without permission prompts — by default in **auto mode** (`--permission-mode auto`: a classifier still blocks dangerous actions), or, if you switch the Coding-options selector, with the legacy `--dangerously-skip-permissions` (no safety net). The marginal risk over your existing Tailscale remote access is small (anyone on the tailnet could already RDP in) — the passkey gate + audit log make this surface *more* controlled than plain remote access, not less.

**Enrolling your iPhone**

1. On the PC, set `webauthn_rp_id` (bare tailnet hostname, e.g. `pc.tailnet.ts.net`) and `webauthn_origin` (full origin, e.g. `https://pc.tailnet.ts.net:8445`) in `config/webapp_config.json`, and restart the webapp.
2. On the iPhone, open the launcher over the Tailscale URL.
3. On the PC, tray menu → **🔐 Enroll device (5 min)**.
4. On the iPhone, run the enrollment ceremony. ⚠️ The **Settings → Terminal access → Enroll this device** button was removed in #383's review round; the `/api/webauthn/enroll/*` endpoints still work, but enrolling a *new* device needs that small UI wired back (see `webauthn.js`'s note) or a one-off page. Already-enrolled devices are unaffected.

After that, opening any session prompts Face ID once per 12 h.

**Terminal on the PC too.** With `claude_show_local_window: true` (the default), launching a session from the phone also opens an **interactive** terminal window for it on the PC. That window connects over loopback — so it bypasses the Tailscale + passkey gate — and because the session-host fans output to every connected client and accepts input from all of them, **you can type from the phone and the PC interchangeably**. Set it to `false` to launch silently. Launching from a **desktop browser** (even over the tunnel) skips this window — that browser already shows the terminal in-page, so a separate window would be redundant (issue #159); the mirror is recognized as superfluous by a fine/mouse pointer and suppressed.

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
- **Settings → API tokens** (issue #72) mints additional *job-scoped* bearer tokens: each can only fire its chosen Jobs-tab job (`POST /api/jobs/<id>/run`) and is rejected everywhere else, so the URL baked into a Stream Deck button no longer carries full-SPA access. The raw token is shown once at mint; revoke + re-mint to rotate without touching `auth_token`. See `docs/jobs-tab.md` → "Scoped API tokens".

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
│       ├── routers/           # split API routers (config, sessions, life_os, system_map, …)
│       └── static/            # SPA shell + PWA manifest + icons + vendored xterm.js
│
├── src/                       # logic layer (no UI imports)
│   ├── app_config.py          # log level, webapp embed section
│   ├── webapp_config.py       # host/port/scan-paths/claude flags/secrets/terminal knobs
│   ├── agents.py              # coding-agent registry (claude / codex / agy / copilot) + PATH detection
│   ├── registry.py            # apps registry (load/save/scan) + live claude-code rows
│   ├── scanner.py             # bat classifier + project-dir + life-os skill discovery
│   ├── launcher.py            # spawn_bat / spawn_claude_session helpers
│   ├── session_host.py        # PtySession + RemoteSession + SessionManager (ConPTY via pywinpty)
│   ├── vt_snapshot.py         # headless pyte VT mirror per fullscreen session (reconnect snapshot)
│   ├── _loopback_http.py      # shared loopback HTTP client base (session/voice/photo/tts)
│   ├── session_client.py      # webapp → session-host loopback HTTP client
│   ├── webauthn_gate.py       # passkey enrollment / assertion + terminal tokens
│   ├── audit.py               # terminal audit + per-session logs
│   └── diagnostics.py         # log ring buffer + port-owner introspection
│
├── scripts/
│   ├── gen_icons.py           # thin caller onto project-scaffolding's shared brand_gen.py (rocket master)
│   ├── gen_tailscale_cert.py  # tailscale cert (real LE) + --check auto-renew
│   ├── gen_token.py           # bearer token rotate / clear
│   ├── set_password.py        # login password set / clear
│   └── run_named_tunnel.py    # uvicorn + cloudflared (headless)
│
├── config/                    # *.sample.json committed, real files gitignored
│   ├── config.sample.json
│   ├── webapp_config.sample.json
│   └── apps.sample.json
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
| `projects_dir` | parent of this repo | Master folder whose direct child directories the Coding tab lists as projects |
| `projects_ignore` | `[]` | gitignore-style folder-name patterns (case-insensitive, `*`/`?` globs) hidden from the Coding tab |
| `coding_favorites` | `[]` | Project ids (scanner slugs) starred as favorites in the Coding tab (issue #250). Managed by the per-tile ★ — favorites pin to the top of the list and the header **★ Favorites** toggle filters to just these. Not normally hand-edited. |
| `apps_scan_root` | parent of this repo | Where the Apps tab scans recursively for `*.bat` |
| `life_os_dir` | sibling `../life-os` | Root of the `life-os` checkout the Life OS tab surfaces (skills at `<life_os_dir>/.claude/skills`, identity at `<life_os_dir>/identity`). When the skills dir doesn't exist the tab shows disabled, the same way the Coding tab handles a missing `projects_dir`. |
| `claude_config_dir` | sibling `../fleet-config` | Root of the `fleet-config` checkout whose `architecture/system-map.png` the Coding tab's 🗺️ System map section surfaces (issue #173). When the rendered PNG is absent the section hides. The image endpoint is bearer-token **and** Tailscale-only (refused over the Cloudflare tunnel). |
| `terminal_history_lines` | `10000` | Bounded scrollback (200-50000 lines) a full-screen agent (Codex, etc.) session keeps for a (re)connect, Settings-tab configurable (issue #435 follow-up). Too low and the true start of a real conversation becomes unreachable; too high risks a slower reconnect paint on a weak mobile connection. |
| `sessions_state_file` | `~/.claude/hooks/state/sessions-state.json` | The sessions-state file fleet-config's `session_state` hook writes (fleet-config#91), read by the Board tab. Absent/corrupt/stale degrades to `unknown` session status, never an error. |
| `rate_limits_file` | `~/.claude/hooks/state/rate-limits.json` | The Claude 5h/7d usage % cache a fleet-config statusline writer maintains (fleet-config#259, issue #326), read by the Board tab's usage badges. No writer exists yet as of this issue — absent/corrupt/stale degrades to the badges hiding, never an error. |
| `github_owner` | `"ferraroroberto"` | GitHub owner whose repos the Board tab's `gh` searches span (Backlog / PRs / Done-today). |
| `chief_model` | `"fable"` | Model the fleet chief spawns on (issue #245; `sonnet`/`opus`/`fable`). Edited from the Board's chief-settings dialog. |
| `chief_respawn_enabled` | `true` | Whether the daily fresh-respawn job fires (issue #245). Saving a change resyncs the registered `chief-daily-respawn` job (pause/resume). |
| `chief_respawn_at` | `"05:00"` | Daily fresh-respawn time, `HH:MM` (issue #245). Saving a change re-registers the job's schedule. |
| `chief_worker_cap` | `3` | Max concurrent worker sessions the `/chief` skill may keep running (1-10, issue #245; ceiling raised 8→10 in #547). Read by the skill over loopback via `GET /api/board/chief/settings` — its dispatch rail, phone-tunable. |
| `claude_model` | `"opus"` | Default `--model` for `claude` (Claude Code button only) |
| `claude_effort` | `"high"` | Default `--effort` (use `"off"` to omit the flag) |
| `claude_verbose` | `true` | Pass `--verbose` |
| `claude_debug` | `false` | Pass `--debug` |
| `claude_permission_mode` | `"auto"` | Permission flag: `"auto"` → `--permission-mode auto`, `"skip"` → `--dangerously-skip-permissions` |
| `auth_token` | `""` | Bearer token. Empty = gate off (unless `api_tokens` has entries). |
| `auth_password` | `""` | Optional companion for `/api/login`. |
| `secrets` | `{}` | One gitignored place for job secret values (issues #73, #72): a job's `webhook.secret` and any `Job.env` value can be `$secret:<key>` resolved against this dict at fire time. Legacy key `webhook_secrets` still loads. |
| `api_tokens` | `[]` | Scoped bearer tokens minted from **Settings → API tokens** (issue #72): salted-hash records whose job-scoped kind can only call `POST /api/jobs/<id>/run` for its allowed jobs — safe to bake into a Stream Deck URL. Don't hand-edit. |
| `session_host_port` | `8446` | Loopback port the PTY session-host binds. Never network-reachable; must differ from `port`. |
| `tailnet_allowlist` | `[]` | Extra IPs / CIDRs allowed to reach the terminal endpoints, on top of loopback + `100.64.0.0/10`. |
| `claude_show_local_window` | `true` | Open an interactive terminal window on the PC when a session is launched from the phone. |
| `webauthn_rp_id` | `""` | Passkey relying-party ID — the bare tailnet hostname. Empty disables the passkey gate. |
| `webauthn_rp_name` | `"Launcher"` | Display name shown in the passkey prompt. |
| `webauthn_origin` | `""` | Full https origin the phone connects to (scheme + host + port). |
| `voice_transcriber_url` | `https://127.0.0.1:8443` | Base URL of the sibling voice-transcriber webapp the compose bar's 🎤 dictation proxies to over loopback (issue #165). Empty string disables dictation (the button hides). |
| `photo_ocr_url` | `https://127.0.0.1:8444` | Base URL of the sibling photo-ocr webapp the compose bar's 📷 screenshot OCR proxies to over loopback (issue #171). Empty string disables OCR (the button hides). |
| `llm_hub_url` | `http://127.0.0.1:8000` | Base URL of the sibling local-llm-hub the 🔊 read-aloud's Orpheus voice proxies to over loopback (issue #203). Plain HTTP — the hub serves no TLS. Empty string disables the hub path (🔊 falls back to the on-device Web Speech voice). |
| `pushover_api_token` / `pushover_user_key` | `""` | Pushover credentials for Jobs-tab failure notifications (issue #66). Both must be set; missing creds = no-op. |
| `notify_on_failure` | `false` | Master switch — even with creds set, no push fires until this flips on. |
| `notify_failure_streak` | `0` | When > 0, also fire a separate "N consecutive failures" push when the failure streak ticks to exactly this count. |
| `notify_failure_summary` | `false` | When `true`, pipe the output tail through the local LLM hub at `llm_hub_url` (`claude-haiku-4-5`) for a one-line root-cause line prepended to the push body. |
| `telegram_bot_token` / `telegram_chat_id` | `""` | Telegram credentials for the per-job `alert_on_failure` channel (issue #597). Both must be set; missing creds = no-op. Independent of the Pushover settings above — this fires only for jobs with `alert_on_failure: true`, not globally. |

`--remote-control` is **always** added to the **Claude Code** launch — that's the whole point of the remote tab. The permission flag is set by the Coding-options **Permission** selector: `--permission-mode auto` (default) or `--dangerously-skip-permissions`. The **Codex CLI** launches with its **Reasoning** tier (`-c model_reasoning_effort=<low|medium|high>`) plus a **Permission** pair — `--ask-for-approval never --sandbox workspace-write` (Auto mode) or `--dangerously-bypass-approvals-and-sandbox` (Skip permissions). Full-control Codex sessions additionally carry `-c disable_paste_burst=true`: the launcher already supplies explicit bracketed-paste framing and paced ConPTY writes, so Codex's fallback burst heuristic would otherwise turn the compose bar's first Enter into a newline (#436). Detached Codex consoles keep Codex's default detector because their native terminal owns paste delivery. The Antigravity CLI and the GitHub Copilot CLI launch with no flags unless their opt-in Coding-options toggles are set.

### `config/apps.json`

Apps-tab registry — bat-based launchers only. Each row:

```json
{ "id": "...", "name": "...", "kind": "streamlit | webapp | tunnel | tray",
  "bat_path": "...", "added_at": "2026-...", "autostart": false }
```

`claude-code` projects are **not** stored here — the Coding tab
discovers them live by scanning `projects_dir` (minus `projects_ignore`).

Scan flow: tap **🔎 Scan** in Settings → `/api/apps/scan` returns a diff → checklist dialog → submit selections → `/api/apps/save` persists.

### Registered Trays (Apps tab)

A `tray` kind (issue #456) is surfaced by the same scan flow above — a
`.bat` file named exactly `tray.bat` whose body references the shared
`tray_lifecycle.ps1` helper (see `tray.bat`'s own header) is recognized as
a sister project's tray, not a plain streamlit/webapp/tunnel launcher.

Once scanned in, each `tray`-kind row gets an autostart switch inline with
its path, in the collapsible **Autostart registered trays** panel. When app-launcher's own
webapp comes up (see the Settings-tab boot toggle above), it walks every
autostart-enabled tray one at a time — in the registry's existing
alphabetical order, no reordering UI yet — waiting for each to report
ready (via its `.fleet.toml`'s declared `port`, or a fixed delay if that's
missing) before starting the next. This avoids a boot-time CPU/disk spike
from launching several sister Python processes concurrently. One tray
failing to start doesn't block the rest. A tray launched this way is
started, not managed — `tray.bat --restart` on THIS machine never touches
another repo's tray process.

---

## Auto-start at log on

Toggle **Start app-launcher at log on** in the **Settings** tab — it writes a
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

---

## Verify

```powershell
& .\.venv\Scripts\python.exe -m py_compile launcher.py
& .\.venv\Scripts\python.exe -m uvicorn app.webapp.server:app --host 127.0.0.1 --port 8445
# then in another terminal:
curl http://127.0.0.1:8445/healthz
```

### Pytest API tests

In-process FastAPI `TestClient` suite under `tests/` (the sister-project pattern) covering `/healthz`, `/api/config` (GET + POST allow-list, incl. `projects_ignore`), `/api/login` + bearer-token gate, `/api/apps` CRUD, live Coding-tab directory discovery (`src/scanner.py` + the ignore list), coding-agent detection + dual launch (`src/agents.py`, `/api/agents`), `/api/claude-code/sessions` (list + stop), and the **Life OS** tab (`src/scanner.py:scan_skills`, `/api/life-os/*` — skill discovery, the bare `/skill-name` launch wiring + opus model override, the content browser's path-jail, and the Tailscale/Cloudflare gate on the content endpoints). Session-host loopback client is mocked — no live tray, no port :8446 needed.

```powershell
& .\.venv\Scripts\python.exe -m pytest tests -m "not smoke" -v
```

Runs in about a second. The `-m "not smoke"` flag excludes the live-tray Playwright suite below.

### Playwright smoke + regression tests

A `pytest-playwright` suite under `tests/e2e/` covers two things:

- **Boot smoke** (`test_smoke.py`) — JS error on boot, empty config form, broken tab switch, the single ✕ stop button per session row (issue #253), missing login overlay.
- **iPhone regression net** — one focused test per closed iOS-only bite, so the next regression of any of them surfaces locally before a deploy instead of after an hour of phone-PC round-trips:

  | File | Pins fix from | What would regress without it |
  | --- | --- | --- |
  | `test_cache_busting.py` | `35caad4` + `bf76d0d` (#30) | `?v=<hash>` stamps in served `index.html` diverge from on-disk asset bytes (forgot to restart tray after editing JS) |
  | `test_iphone_revalidate.py` | `696b723` | iOS Safari serves stale `index.html` and references a `?v=<old>` script that no longer exists → empty Model/Effort controls |
  | `test_terminal_reconnect.py` | `142e2b4` (#28), extended (#444) | Live terminal WS drops on iOS suspend, overlay sticks on "Disconnected." until manual re-open; or the reconnect's scrollback-ring replay stops arriving with the clear-frame preamble prepended — so on a long (ring-saturated) Claude session every reconnect appends the whole ring below the stale buffer, duplicating the conversation tail (#444) |
  | `test_paste_button.py` | (#29) | 📋 paste button in iOS PWA reaches `navigator.clipboard.readText()` but bytes never arrive at the session-host |
  | `test_paste_framing.py` | (#64, #111) | 📋 / compose ➤ Send stop wrapping a paste in bracketed-paste markers (DECSET 2004) — so a multi-KB block reaches the agent as a raw keystroke burst the Windows console input queue drops spans of, instead of one atomic paste |
  | `test_ports_probe.py` | `d564114` | Pywinpty's loopback ephemerals leak into the Running-apps panel under bogus high ports |
  | `test_edge_mirror_close.py` | `b946bc8` (#20) | `terminal.js` stops marking the mirror page with `document.title = 'app-launcher-mirror-<sid>'`, EnumWindows can't find the HWND, Stop & Close leaves the Edge `--app` window hanging |
  | `test_shutdown_frame.py` | (#181) | `terminal.js` `routeFrame` stops recognising the cooperative `{"type":"shutdown"}` WS frame — so the mirror window's Win32-`WM_CLOSE` fallback dies again (window leaks on Stop & Close) and the shutdown JSON prints into the terminal as garbage instead of being dropped on the phone / closing the mirror |
  | `test_inpage_terminal_not_mirror.py` | (#241) | `terminal.js` goes back to deriving `isMirror` from the loopback reason alone, so a session opened **in-page** over loopback (the phone's row-tap, or a desktop with mirroring disabled) is mis-classified as the PC mirror — Stop & Close then `window.close()`s the user's own browser instead of dismissing the overlay. *(Phone/WebKit projection only since #282 — a desktop row-tap now opens a mirror window.)* |
  | `test_desktop_session_mirror.py` | (#282) | a **desktop** row-tap stops opening the dedicated PC Edge mirror window and falls back to hijacking the controlling browser with an in-page terminal — so closing the view feels like it tears down everything. The tap must POST `/mirror` (open-or-focus, never a duplicate) and leave the in-page overlay closed; Stop still dismisses the window (#20) |
  | `test_viewport.py` | (#31) | WebKit projection silently loses the iPhone 15 Pro Max descriptor — the whole projection becomes desktop-shaped and the table above stops catching iOS bugs |
  | `test_terminal_native_scroll.py` | (#23) | `.xterm-screen` stops being `pointer-events:none`, so touches no longer fall through to `.xterm-viewport` and the phone loses iOS native momentum scrolling |
  | `test_keys_popover.py` | (#36, #137) | `⌨️` popover stops sending arrow/Esc/Tab/Enter escape sequences over the WS, so iPhone keyboards without those keys can't drive Claude's TUI prompts; also pins the sticky `⇧` Shift toggle so `⇧`+`Tab` keeps delivering back-tab (`\x1b[Z`) for mode-cycling |
  | `test_compose_bar.py` | (#37, #41) | `✏️` compose bar's `➤` Send stops forwarding `<text>\r` to the PTY, the bar leaks into the PC mirror window, or `🖼` stops dropping the uploaded image path into the bar when it's open |
  | `test_voice_dictation.py` | (#165, #168) | The `🎤` dictation button leaves the compose bar; live SSE `partial` transcripts stop revising the textarea span or `finish` stops settling the canonical text (#168); or the single-shot `/api/transcribe` fallback stops working when streaming setup fails |
  | `test_voice_readback.py` | (#190, #197) | The `🔊` read-aloud button leaves the terminal toolbar (it must sit between ↓ Jump and 📋 Paste, not in the compose bar); the colour-block segmenter stops returning the last `●` reply de-wrapped, stops dropping the composer box + status footer / `recap:` / `Worked for …` / spinner / `⎿ Tip:` epilogue, stops exposing the ordered block list (the #197 depth-selector seam), or the live cell-colour classifier stops telling a default/white `●` (assistant) from a green `●` (tool) in a real xterm buffer; `speak()` stops queuing per-sentence utterances or `cancelSpeech()` stops them; or starting a new dictation stops silencing the in-flight read-aloud |
  | `test_hub_readback.py` | (#203, #206) | The `🔊` button's hub voice regresses: `probeHub()` stops caching the `/api/tts/health` verdict, `speakHub()` stops creating + resuming an `AudioContext` and scheduling the streamed PCM16 from `POST /api/tts/speak` on the Web Audio timeline, a failed POST stops rejecting for Web Speech fallback, or `cancelHub()` stops closing the context + resetting the button |
  | `test_summarize_readback.py` | (#210) | The `🔊` **summarize & read** dropdown regresses: `summarizeReply()` stops POSTing to `/api/tts/summarize` (or stops rejecting on a hub error), the gesture-split compose (`prepareHub` → `summarizeReply` → `speakHubInto`) stops playing the summary, the menu stops offering both actions when the hub is reachable, stops suppressing the menu (single-tap read) when the hub is down, or the summary **modal** stops auto-closing when reading ends / stops dismissing on tap |
  | `test_git_status_flags.py` | (#115, always-on #496) | The Coding tiles stop colouring themselves automatically from the boot-time `/api/claude-code/git-status` fetch (red dirty / yellow off-main, red winning when both), the legend stops revealing without a tap, or the summary head card stops aggregating the dirty count |
  | `test_home_head.py` | (#496) | The `home-head` summary card stops leading the Coding tab (title + stats line + right-pinned theme toggle), the options card stops being the tab's last card, or the Detached/Resume toggles leave the Projects card header (or start toggling the panel on tap) |
  | `test_board_tab.py` (`…rename_first_icon_only_and_stop_kills_session`) | (#496) | The Board drawer regresses: the reply box stops spanning its own full line, the button row stops right-aligning as Rename → Stop → Terminal-last, any button (mic included) drops under the 44px canonical footprint, or the ✕ Stop button stops POSTing the unified `{mode: 'quit'}` stop / closing the drawer |
  | `test_life_os_tab.py` (`…toggles_live_in_skills_summary_without_options_card`) | (#496, #540) | The Life OS tab grows back a separate options card, the model combo / Detached / Resume controls leave the Skills card's summary, or interacting with one (toggle tap or combo change) starts collapsing the panel |
  | `test_coding_model_selector.py` | (#540) | The Coding tab's Projects-header model combo stops staying in sync with the options-card **Model** segmented control (picking in either must update both and persist the same `claude_model`), or Haiku stops being filtered out of the picker |
  | `test_life_os_tab.py` (`…launch_posts_mode_and_model`) | (#540) | A Life OS skill launch stops carrying the model combo's value (regressing to the old `opus` bool) in its POST body |
  | `test_board_tab.py` (`…color_coded_from_shared_git_cache`) | (#496) | Backlog cards stop carrying the red/yellow repo-state annotation fed from the shared client-side git-status cache |
  | `test_board_tab.py` (`…in_progress_is_tinted_and_actions_disabled`) | (#528) | A backlog issue already owned by an active workflow loses its accent tint / “in progress” label, or either duplicate Start/YOLO path becomes clickable again |
  | `test_board_tab.py` (`…matches_sibling_button_shape_on_phone`) | (#496) | The dispatch bar's model `<select>` regresses to native iOS pill chrome on the phone instead of the row's shared button geometry (36px control height, 12px radius, flattened appearance) |
  | `test_status_popover.py` | (#139) | The **⎇ status** button stops opening its compact off-main popover — the at-a-glance list of one line per project parked off its default branch (red dirty / yellow off-main, branch tag), or the second-tap toggle-close stops working |
  | `test_coding_favorites.py` | (#250) | The Coding tab's **★ favorites** regress: a starred project stops pinning to the top of the list (favorites-first, alphabetical within each group), the per-tile star click stops persisting via `POST /api/claude-code/favorites` + reordering on re-fetch, or the header **★ Favorites** toggle stops filtering the list down to only starred projects |
  | `test_life_os_tab.py` (`…_keeps_name_and_buttons_on_one_row`) | (#124) | Life tiles inherit the Coding tab's narrow-phone stack rule (#120) via the shared `.coding-item` class and break the name + 📖 + 🚀 onto separate stacked lines, wasting vertical space when the two buttons fit inline beside the name |
  | `test_life_os_tab.py` (`…_detached_resume_posts_remote_console`) | (#239) | On the Life OS tab, Resume+Detached regresses to forcing a full-control PTY (the pre-#157 "Resume wins over Detached" behaviour) instead of sending `mode: remote` so the picker renders in the detached console |
  | `test_keyboard_overlay.py` | (#135) | The terminal overlay stops pinning to `visualViewport.height` when the iOS keyboard is up, so the active prompt row renders hidden behind the keyboard again (and won't expand back when the keyboard drops) |
  | `test_resume_toggle.py` | (#151, #157) | The **↺ Resume** toggle stops POSTing `resume: true`; or Resume+Detached stops sending `mode: remote` (regressing #157's detached-console picker) / Resume-alone starts sending it — so a resume tap would open the picker in the wrong place |
  | `test_stop_unify_and_terminal_kill.py` | (#253) | The running-sessions row grows back a second stop button (the old ⏹ "leave window open"), or the terminal bar loses its in-view ✕ Kill button beside the ‹ back arrow — so killing a session needs a back-then-stop round-trip again |
  | `test_session_title_naming.py` | (#266, extended #396, #458) | `sessionTitle()` precedence regresses — a manual rename stops winning outright over everything else, a genuine shared cross-tab title stops winning outright, a real Claude summary stops winning over a derived shared name, a `<folder>`-echo title (Codex/Pi) stops yielding to the first-prompt title, or an agent with no title stops falling back to it; or `mirrorDocTitle()` drops the human title or the `app-launcher-mirror-<sid>` close marker |
  | `test_session_rename.py` | (#458, extended #484) | The Coding tab's ✏️ rename button or the Board drawer's Rename button stop opening the shared dialog, the rename POST stops carrying the typed title, the Coding row stops reflecting the rename after its re-fetch, or the Board card stops updating optimistically in place; or the rename dialog's Cancel/Save footer regresses from an equal-width, equal-height pair back to a ballooned full-width Save beside a caption-sized Cancel, the title field stops spanning the full dialog width, or the dialog's uniform 12px internal rhythm breaks (field-to-footer gap collapsing, or the Cancel-to-Save gap drifting from that same step) (#484) |
  | `test_shared_session_title.py` | (#396) | The Board tab's session cards and the Coding tab's Running-sessions row stop showing an identical title for the same live session — i.e. the two tabs' title sources drift apart again |
  | `test_board_tab.py` (`…distinguishes_empty_from_source_failure`) | (#457) | The Board drawer regresses to describing a missing/unparseable conversation source as “No exchange yet,” instead of keeping the true-empty and sanitized error lifecycle states distinct |
  | `test_rotation_overlay_reset.py` | (#446) | `terminal.js` stops releasing a stale keyboard-heuristic pin on the terminal overlay when `orientationchange` fires — a portrait-landscape-portrait rotation cycle leaves the overlay stuck at a shrunk height/top, bleeding the Coding tab's session list and Projects grid through underneath it |
  | `test_board_chief.py` | (#245, #617) | Chat mode stops routing to the chief (ensure + the `{data, submit:true}` input proxy) and leaks into `/api/board/dispatch`, the chief card loses its distinct crown/tint or its reply stops rendering in the drawer, the chief ✕ loses its confirm (or worker cards gain one), the manual Start affordance disappears when no chief is alive, Restart disappears (or Start reappears) when one is alive, or the settings dialog stops round-tripping GET → edit → PUT |
  | `test_coding_chief.py` | (#547) | The Coding tab's session-list row (or the terminal overlay's title) loses the chief's crown, the Coding tab's ✕ or the terminal overlay's kill button loses its confirm (or a worker row gains one), or the Coding tab's manual Start-chief affordance stops appearing/POSTing when no chief is alive |

Every test runs in **two projections** — Chromium-desktop and WebKit on an iPhone 15 Pro Max viewport — so engine-specific iOS bugs get caught on Windows before they reach a real phone. A few tests skip on the duplicate projection where the check is browser-agnostic (server-side header inspection, etc.). Pin a single engine with `--browser chromium` (or `webkit`) for a faster dev loop.

One-time setup:

```powershell
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
& .\.venv\Scripts\python.exe -m playwright install chromium webkit
```

**Run after every webapp/SPA edit** with the tray up (`tray.bat`):

```powershell
.\scripts\run-e2e.ps1                       # both projections (~9 min — full suite)
.\scripts\run-e2e.ps1 --browser chromium    # Chromium-only — the faster dev loop
# or directly — the env var is the explicit live-tray opt-in (see below):
$env:LAUNCHER_E2E_LIVE = "1"; & .\.venv\Scripts\python.exe -m pytest -m smoke -v tests/e2e
```

The suite runs against the live tray on `https://127.0.0.1:8445` — it does not boot anything itself. If the tray isn't up, every test is skipped with a clear message instead of hanging. Loopback access auto-bypasses the bearer-token middleware and the passkey gate, so no credentials are needed.

Because that live instance is the one the phone is using, targeting it is an **explicit opt-in**: `run-e2e.ps1` sets `LAUNCHER_E2E_LIVE=1` for you, and a bare `pytest tests/e2e` without it (and without autoboot) exits immediately with a guard message instead of silently load-testing the live webapp.

The terminal-related regression tests get a live PTY session from one of two fixtures (issue #534) — neither requires any test-only product hooks (no `LAUNCHER_TEST_HOOKS=1` env var):

- **`launched_pty_session`** — the default for UI-only assertions (toolbar, overlay geometry, dictation, readback, session rows, WS wiring, input logged by the webapp). Under the autoboot gate the child is a **deterministic lightweight stub** instead of the real Claude CLI: the harness prepends a generated `claude.cmd` shim to the *disposable* session-host's `PATH` that routes the `--e2e-stub` sentinel to an instant Python echo loop, so ~55 tests × 2 projections stop spawning a real Claude/node process each — removing the host-load variance that ballooned loaded runs, and letting these tests run on CI (the stub needs only Python) — while still exercising the production webapp ↔ session-host ↔ ConPTY boundary. Against the live tray (`run-e2e.ps1`) there is no shim, so it falls back to a real `claude` launch.
- **`launched_claude_pty_session`** — a real `claude` child, only for tests whose assertions depend on the real agent's rendered output (currently the #444 reconnect-replay scrollback pin).

The WebSocket-drop probe and the clipboard mock are injected via `page.add_init_script` from inside each test, so the production surface is untouched.

Byte-loss at the PTY write boundary itself has a dedicated **non-browser** guard, `tests/test_session_host_pty_realpty.py` (in the `pytest tests -m "not smoke"` suite, Windows/pywinpty-gated): it pushes multi-KB payloads through `PtySession.write` into a *real* ConPTY and asserts a byte-for-byte lossless readback. A `MagicMock` PtyProcess can never drop bytes, so this real-PTY readback is what proves the write path is clean — the unit tests in `test_session_host_pty_write.py` only pin the chunk-and-pace shape and the #13 no-retry contract.

### Verifying changes before ship

`run-e2e.ps1` above is the dev loop — fast, but it *skips* the whole e2e suite if the tray isn't up, which is the wrong default for a final check (a forgotten tray looks like a green run). The pre-ship gate closes that hole:

```powershell
pwsh -File scripts\verify-before-ship.ps1
```

It runs the full pipeline as one pass/fail — byte-compile (`app`, `src`, `tests`), the non-e2e pytest suite, then a **diff-proportionate slice** of the Playwright e2e suite (issue #568) — and **boots its own disposable webapp + session-host** on a free port, so it never silently skips:

- A tray on `:8445` may be running or not. Autoboot picks a free port for its webapp and **always spawns its own disposable session-host** on a free port (never the live `:8446`, whose sessions include the user's real Claude PTYs — issue #260). The existing tray is left untouched.
- The disposable instance serves HTTPS reusing `webapp/certificates/` (plain HTTP if no cert pair exists). Subprocess output is captured to `webapp/e2e-autoboot-*.log`.
- The disposable webapp reads and writes a **temp copy** of `config/webapp_config.json` (`webapp/e2e-autoboot-webapp-config.json`, via `LAUNCHER_WEBAPP_CONFIG`), so an e2e test that saves settings can never mutate the real config file — the gate asserts the real file is byte-identical after the run and fails loud if not (issue #441).
- It persists a live progress log to `webapp/verify-progress.log` (gitignored, overwritten each run): phase markers from the script plus one `START`/`DONE` line per test — with per-test totals including fixture cost — and a slowest-15 summary at the end of each pytest phase. If the gate wedges or an outer timeout kills it, the last `START` without a `DONE` names the active test, so a genuinely slow test is distinguishable from aggregate overhead (issue #534).
- **The e2e phase is routed to the diff (issue #568).** Byte-compile and the non-e2e pytest suite always run; only the *browser* slice is scaled to what the branch actually changed, classified by `scripts/classify_e2e.py` against `main`: a **static-asset-only** diff (images, fonts, webmanifest, vendored HTML sprite fragments) runs `tests/e2e/test_smoke.py` **Chromium-only** (~15 s, no WebKit exposure); a **backend/docs/non-e2e-test-only** diff runs **no browser suite** at all (the non-e2e pytest already covers it); everything on the real browser surface (any `.js`/`.css`, real app pages, webapp/session-host/launcher Python, `tests/e2e/**`) — plus any *mixed*, ambiguous, or unrecognized diff — runs the **full dual-projection** suite unchanged. Routing is **fail-safe**: uncertainty always escalates to the full suite, never narrows it. The chosen tier and the triggering paths are printed to the console and to `webapp/verify-progress.log`. On CI the full suite always runs — the local gate is where routing is proven. The path→tier rules live in one reviewable place (`scripts/classify_e2e.py`); `python scripts/classify_e2e.py` prints how the current branch would route.
- It exits non-zero on the first failure and prints total wall time. Measured contract for the **full-suite path** (2026-07-17, #534, idle dev box): **~10–11 min** — ~5 s byte-compile, ~70 s non-e2e pytest, ~9 min e2e (360 nodes across both projections). The bulk is the browser suite itself, not agent startups; treat a full run past ~15 min as wedged and read `webapp/verify-progress.log` for the stuck node before killing anything. A routed narrow run finishes far faster (a static-asset diff completes the whole gate in ~90 s).

Run it before declaring any change to `app/webapp/`, `src/launcher.py`, or `src/session_host*.py` done. The same autoboot path is available to a plain pytest run with `--e2e-autoboot` (or `LAUNCHER_E2E_AUTOBOOT=1`).

The same gate also runs on CI (`.github/workflows/e2e.yml`, `windows-latest`) on every push to a non-`main` branch and on pull requests into `main` — so the gate runs without relying on remembering to. The local `verify-before-ship.ps1` stays the contract; CI is supplementary.

Since the #534 fixture split, the UI-only terminal tests run on CI too: their lightweight stub child needs only Python, so the CI runner exercises them instead of skipping. Only the `launched_claude_pty_session` tests (real-Claude rendered-output assertions) still check `claude` is on `PATH` and **skip** cleanly where it isn't — notably the CI runner, which never installs it (issue #58). A failed run keeps the autoboot and per-session logs as a downloadable `e2e-logs` artifact on the run page, so any e2e failure can be diagnosed without a local repro.

---

## Files

- `launcher.py` — argparse → `tray` | `webapp` | `scan` | `session-host`
- `app/webapp/server.py` — FastAPI server + all `/api/*` routes, Tailscale gating, WS proxy
- `app/webapp/manager.py` — adopt-or-spawn uvicorn for the tray
- `app/session_host/server.py` — loopback PTY host (HTTP + WebSocket)
- `app/tray/tray.py` — pystray icon + cloudflared + session-host lifecycle
- `src/session_host.py` — `PtySession` + `SessionManager` (ConPTY via pywinpty)
- `src/vt_snapshot.py` — headless pyte VT mirror per fullscreen session (reconnect snapshot, issue #432)
- `src/session_client.py` — webapp → session-host loopback client
- `src/webauthn_gate.py` — passkey enrollment / assertion + terminal tokens
- `src/audit.py` — terminal audit + per-session logs/transcripts
- `src/registry.py` — unified apps registry
- `src/scanner.py` — bat classifier + project-directory discovery
- `src/agents.py` — coding-agent registry (Claude Code / Codex CLI / Antigravity CLI / GitHub Copilot CLI / Pi) + PATH detection
- `src/webapp_config.py` — persisted UI prefs + auth secrets + terminal knobs
- `scripts/gen_*.py` — token / password / icons / SSL cert / tunnel
- `config/*.sample.json` — committed templates; real files are gitignored
- `webapp/` — runtime state (certs, tunnel URL, audit logs, per-session logs)
