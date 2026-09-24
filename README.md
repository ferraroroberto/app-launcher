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

The web UI has five tabs, plus **Settings** (issue #1131: six tabs forced 11px labels, so Settings, the rarely used one, moved behind a **⚙ gear** in every page header — tap any tab to leave it). Every tab opens with the same **page header**: the tab's name, one context line, the theme toggle and that gear.

On a **wide desktop window** (1100px and up, mouse pointer — issue #1135, the fleet's wide layout) the tabs move into an 80px **left rail**, icon over label, and the Board spans everything right of it, header included. The **Code tab becomes list-and-detail**: a session's view (Chat, or Terminal where it is in-page) opens in a pane beside the session list instead of covering it, the row it shows stays tinted, tapping another row swaps the view in place, and with nothing open the pane says so. A full-control session's terminal still opens in its own PC window (#282). Leaving the Code tab closes the view. Below 1100px, and on a phone or tablet at any width, the layout is unchanged: the tabs sit in the top control of the centred 772px column (floating bottom pill on the phone).

- **Coding** — opens with a one-line **summary head card** (issue #496, the fleet's vendored `home-head` component): app icon + title, live stats (`N sessions · M apps running · X dirty · Y off-main`), and the light/dark **theme toggle** pinned right — the same position as the other fleet apps. Below it, every project directory directly under your configured projects folder becomes a tile (no `.code-workspace` or `*-remote.bat` needed — the list is the directory listing, recomputed live; hide folders with a gitignore-style ignore list in Settings). Each project is an **action-row** (issue #1128, the fleet's vendored `action-row`): the **bare on-disk folder name**, with the leading **★ star**, and **tapping the row launches your favourite agent** (Settings → *Favourite agent*) in it. Every other agent, and every other project action, sits behind the row's one trailing **⋮** kebab. The agents:
  - **Claude Code** (`claude`), **Codex CLI** (`codex`), **Antigravity CLI** (`agy`), **GitHub Copilot CLI** (`copilot`), **Pi** (`pi`, on your Claude subscription via the Agent SDK), and **Grok Build** (`grok`, xAI's terminal agent) — each menu row bears the agent's icon. An agent is greyed out with a hint when its CLI isn't installed (detection: the command resolves on `PATH`). See [Installing the Codex CLI](#installing-the-codex-cli), [Installing the Antigravity CLI](#installing-the-antigravity-cli), [Installing the GitHub Copilot CLI](#installing-the-github-copilot-cli), [Installing Pi](#installing-pi), and [Installing the Grok Build CLI](#installing-the-grok-build-cli) below.
  - A trailing **GitHub icon** opens the project's open-issues list, sorted by last updated, in a new browser tab — no process spawned, no session created. The repo URL is derived from the project's `origin` git remote; the icon is disabled with a hover hint when the folder has no GitHub remote.
  - Each launch has **two modes** chosen by the **☁️ Detached** toggle in the Projects card's toolbar (issue #496 moved the launch-time toggles onto the launch surface itself; #1132 took them out of the card's header, where a near-miss folded the card). A **filter** field above the list narrows it by name as you type (issue #1132; the Apps list has one too), remembered per list on the device, with an empty-state when nothing matches. **Toggle off → full control:** the agent starts inside a **launcher-owned pseudo-console (ConPTY)** and the phone drops straight into a **live, fully interactive terminal** — real output, scrollback, typing, `Ctrl+C`, image paste. **Toggle on → detached:** the agent opens in its own console window on the PC; the launcher only *tracks* it (running-sessions list, killable from the phone) and it survives a launcher restart — including a full `tray.bat --restart` (issue #130).
  - A second toggle, **↺ Resume** (issue #151), reopens an existing conversation: with it on, the next agent tap launches that agent's **own native session picker** — `claude --resume`, `codex resume`, `copilot --resume` all show their list of recent sessions to pick from. (The launcher never builds its own session list; it only hands off to the agent's picker.) Antigravity has no picker flag, so its Resume **continues the most recent** conversation (`agy --continue`); Grok's bare `grok --resume` likewise reopens the working folder's **most recent** session. Resume is **orthogonal to Detached** (issue #157): with Detached **off** the picker streams to the phone in a full-control terminal; with Detached **on** the picker renders in the **detached console window** on the PC (a real interactive console, so the list is pickable there) and the session shows as `☁️ detached` in the running-sessions list.

  - A **launch-model selector** sits in the Projects header beside those toggles. It offers provider-qualified **Claude · Sonnet/Opus/Fable** and **Codex · Luna/Terra/Sol/Astra** choices, remembers each provider's default, and sends the selected value when its matching agent button is tapped. Every model selector across Coding, Board, Skills, history, and agent settings uses the same keyboard-ready menu while preserving each surface's own catalog, availability, saved value, and launch payload. The options card exposes the same server-owned catalog and model-compatible effort choices.

  - The Running sessions header shows **two quota rows — Claude Code above Codex, always both**, one compact line each (`Claude Code · 5h 39% ↻ 14:20 · 1w 19% ↻ Sep 11`), read from fleet-config's shared snapshot contract. The rows do not follow the model selector: the number you need before launching is usually the one for the agent you have *not* selected. Each line is a single nowrap line on the phone and takes its colour from the worse of its two windows (green <60%, amber 60–79%, red ≥80%) — there is no separate status dot. Codex reports several native buckets at the same duration, so each duration collapses to its fullest window, keeping internal bucket ids out of the UI; measured `0%` stays distinct from unknown, and a source that is absent, unsupported or erroring still renders its own degraded line rather than dropping out. Claude's shard is only rewritten when a session paints its statusline and expires after ten minutes, so an idle stretch routinely returns a bare `unknown`; the row then keeps its **last reading, dimmed and labelled** (`… 1w 19% ↻ Sep 11 · unknown`) instead of blanking the numbers you opened the tab to read — never presented as a current, confident value. Codex refreshes through fleet-config's bounded native one-shot collector when its cached observation expires — now regardless of which agent is selected.

  - Git state is **always on** (issue #496, reversing #115's on-demand contract): the launcher fetches `/api/claude-code/git-status` once at boot and re-polls every ~45 s while the Coding or Board tab is visible in a foreground page (paused when the PWA is backgrounded), so every tile carries its colour with no tap — **yellow** name = parked on a non-default branch (not a fresh start, with the branch named on the row's context line), **red** name = uncommitted changes, also spelled out on the context line (red wins when a project is both), with a tiny legend under the list. The aggregate (`2 dirty · 1 off-main`) also rides the summary head card. The **⎇ Git status** button above the sessions list remains as the drill-down: it re-fetches fresh state and opens the off-main popover (issue #139).

  - **★ Favorites** (issue #250) — each row leads with a **star toggle**: tap to pin the projects you actually work on. Favorites sort to the **top** of the list (alphabetical within the favorites group, then the rest alphabetically), so the default view always surfaces them first while keeping every project one scroll away. A **★ Favorites** toggle in the Projects toolbar **filters the list down to only your starred projects** — one tap to hide the long tail when you just want the hot few, tap again to bring them all back. Favorites persist in `config/webapp_config.json` (`coding_favorites`, the same place as the ignore list — no extra file); the header filter is a client-side view that persists across reloads.

  - **⋮ Project menu** (issue #977, widened by #1070 and #1128) — the row's one kebab, the same floating icon + label menu as a running session's ⋮: a **Launch <agent>** row per visible non-favourite agent, **GitHub issues**, and three project actions:
    - **Open in VS Code** (issue #802) — opens that project in the local editor via its `<name>.code-workspace` file — a **sibling** of the project folder, directly under `projects_dir`, the layout VS Code itself already uses here — **creating that file first** with a minimal one-folder shape when it doesn't exist yet; an existing workspace file is opened, never rewritten. Deliberately *not* a coding-agent launch: no PTY, no session, nothing in Running sessions and no Stop button. The item greys out with a hover hint when the `code` CLI isn't on PATH (VS Code installs it through its own *Shell Command: Install 'code' command in PATH* step).
    - **Show changes** — a **read-only, phone-first view of the working tree**, so a red tile no longer means "open VS Code just to look". A full-screen overlay (same chrome as the transcript) lists every changed and untracked file grouped **Changes / Untracked**, each row with a status badge (`M` modified · `A` added · `D` deleted · `R` renamed · `U` untracked · `C` conflict), a green pip when the change is staged, the path, and `+N −N` counts; tapping a row expands its **unified diff** inline (GitHub "Files changed" style — added/removed lines tinted, long lines wrapped), fetched lazily per file and **capped at 200 KB** (the row says so and points at VS Code). Everything is measured against `HEAD` (or the empty tree before a first commit), so the list is "what a commit would contain"; a clean tree shows *Working tree clean*. A red or yellow name flags a row worth opening this for; since #1128 the row itself launches, so the name is no longer its own shortcut in. The item is hidden for a folder that isn't a git repository. A viewer only: nothing here stages, commits, or discards.
    - **Open folder** — opens the project directory in **Windows Explorer** on the PC. Nothing is tracked afterwards.
    - VS Code and Explorer are **local-machine** actions, like every Coding-tab launch: they open on the PC the launcher runs on, not on the phone that tapped them. The whole menu is hideable from the **Visible agents** list below under the `vscode` pseudo-id (an existing hidden list keeps working).

  - **Visible agents** (issue #666) — with six agent buttons plus VS Code plus GitHub plus the star, the tile's icon strip gets crowded on the phone, and most people only ever use two or three harnesses. The ⚙️ options card's **Visible agents** list carries one switch per button (every registered agent, plus the VS Code and GitHub issues icons); flip one off and it disappears from every project row immediately, tap it back on and it returns. The list is **generated from the live agent registry**, so a newly added harness shows up in it automatically — nothing to configure per agent. The choice persists in `config/webapp_config.json` (`coding_hidden_agents`, a *hidden* list so new agents default to visible). The favorite star is never hideable, and hiding is strictly about **launch clutter**: a hidden agent's icon still appears on its running sessions and Board cards, so the app never misreports what's actually running.

  Running sessions are listed above the project tiles, each marked with its agent's icon and tagged `⚡ full control` or `☁️ detached`. Each row carries one **⋮ kebab** (issue #953, the glyph since #1025) pinned to its right edge and centred against the full row height, which opens a floating menu of the row's actions — a vertical list of icon + label rows (issue #967): **⌨ Terminal** (full-control rows), **💬 Chat** (every row whose agent has a transcript reader), **✏️ Rename / link**, and **✕ Stop-and-kill** (issue #253). There is no chevron: #1025 replaced the row's ⚙️ gear with the kebab and dropped the chevron, keeping the menu exactly as it was. Tapping the kebab opens the menu and never the session. **Tap the row itself** to open the **session view** in the mode it was last viewed in (issue #982): a full-control row starts in Terminal, and a detached row whose agent has a transcript reader — Claude, Codex, Grok Build, Pi, Antigravity CLI and GitHub Copilot CLI (issues #966/#1012/#1013/#1014/#1015) — opens in Chat. **Every** row opens, including a detached one of an agent with no reader: both mode segments are greyed and Chat shows the reason, because the view is where that session's Rename and Stop live. The same **Rename / link** and **✕ Stop and kill** are also in the view's **⋮** menu: stop asks the agent to quit cleanly with its own command (`/quit`, Copilot's `/exit`) so its shutdown hooks run, waits briefly for the clean exit, then force-terminates as a fallback — and the window always closes (no confirm, except the fleet chief). The quit command is typed as **three separate keystrokes with a beat between them** — Esc, the command, then Enter (issue #1016) — because sending them as one burst broke it two different ways, on four of the six agents: a TUI that binds Alt-keys reads an Esc arriving together with the following `/` as a single meta-key press and swallows the slash (Pi, Antigravity and GitHub Copilot all did, so the harness saw a bare `quit` and answered it as a prompt), and a command arriving together with its Enter races the harness's own slash-command popup (Codex left `/quit` sitting in an open popup and never acted on the Enter, so **every** Codex stop used to fall through to the force-terminate). Same bytes, same order, just not glued together. The Board tab's drill-down drawer offers the same four actions as one row of equal buttons (issue #984). The session view is **one full-screen overlay with two modes** and a shared bar — *‹* back, the title, an icon-only **Terminal ⇄ Chat** toggle, 🔊 read-aloud, and the **⋮** menu (Rename · Copy link · Stop and kill, plus **Show tool calls** and **Reload** while Chat is showing). Switching modes never reconnects or reloads anything: the live terminal keeps its socket and scrollback while Chat is up (no repaint, no duplicated output), and Chat keeps its loaded pages while the terminal is up. A segment the session can't offer is greyed, and a tap on it says why — *Detached session — no terminal* (also shown under a detached chat), or *No transcript reader for <agent>*. **Chat mode** shows the session's whole conversation as a chat-style list that reads on the phone: your typed prompts and the agent's replies are expanded, and everything else — tool calls with their results, thinking, harness plumbing, sub-agent traffic — is folded per run into one line (`3 tool calls · 1 thinking`) you can open, then open item by item — and those groups are **hidden by default** (the ⋮ menu's **Show tool calls** reveals them, still folded, so the view opens as a plain you ↔ agent exchange). A **tool call that failed is marked** (issue #1020): a red glyph and a `failed` chip on its row, and a `1 failed` chip on the group's closed header so you see it without unfolding — previously a run where every file read 404'd looked identical to one where they all worked. How far that can be trusted **depends on the agent**, and the card says so rather than letting silence read as success: Claude Code, Grok Build and Pi record every tool failure in their own history, so an unmarked call there really did work; Antigravity CLI and GitHub Copilot CLI record only some of them (a tool that could not run is recorded, a shell command that merely *exits non-zero* is not), and Codex records none at all — for those three an opened card whose call is unmarked says the outcome was never recorded. Nothing is guessed from the text of a result: a `grep` that successfully finds the word "error" is not a failure, and no reader here pretends otherwise. **A question the agent asks you is a card you can answer from Chat** (issue #1149): when Claude Code calls `AskUserQuestion`, Chat shows the question with its numbered options (bold label, muted description) as its own card, visible even with tool calls hidden, instead of a folded `AskUserQuestion` row. While it is the question the session is waiting on, tap an option to answer it: one tap sends a single-choice question, and a multi-choice or several-question call takes your picks and a **Submit answers**. A single-choice question also takes a typed answer (Claude Code's *Type something*). The launcher re-checks against the transcript that the question is still waiting before it types anything, then types the picker's own keystrokes: raw keys over the terminal socket for a full-control session, one console-input call per key for a detached one. The card locks until the agent's answer lands in the transcript, then shows what was picked; an older question, or one the agent has moved past, is plain history with nothing to tap, and so is every card in the Life OS conversation viewer. **A plan the agent asks you to approve is a card too** (issue #1151): Claude Code's `ExitPlanMode` shows the whole plan rendered as markdown, like a reply, and how it was answered: *Approved* (or *Approved after your edits*), *Sent back with feedback* with your feedback quoted, *Not approved*, or *Never shown* when the agent called it outside plan mode. It is read-only: a plan still waiting says *answer it in the terminal* (or the PC console for a detached session). Answering plans from Chat is deliberately not offered yet, because Claude Code can hold a pending plan back from its history file until it is answered, and the first option on its picker changes with the session's permission mode ("auto-accept edits" or "switch to BYPASS PERMISSIONS"), so a blind tap could approve into a mode you never saw. Each prompt/reply card collapses on tap, URLs in any text are clickable, and **the conversation keeps itself up to date while you are reading it** (issue #1050): new prompts, replies and tool calls appear on their own, with no pull-to-refresh and no tap. That runs **only for the chat you are actually looking at** — a window showing Terminal does not also fetch chat, a closed session view fetches nothing, and a backgrounded tab or a locked phone stops entirely and does one catch-up fetch when you come back, rather than replaying every tick it missed. It matters on the PC, where several session windows can be open at once: each window refreshes only its own conversation, and only while that window is showing Chat. Refresh is incremental — only what the agent has appended is fetched and added to the bottom, so your scroll position, an open tool-call group and read-aloud are never disturbed by it, and an unchanged session costs the PC one file-size check rather than a re-read (measured flat at ~2.8 ms of server time per check whatever the session's size, versus 5-45 ms to re-read the newest page of a long one). A session that ends stops refreshing and says so instead of polling a dead session. **Reload** in the same menu is still there as the manual escape hatch — it restarts refresh after repeated network failures or a finished session, and re-reads the conversation from scratch. It reads the agent's own history (Claude Code's session JSONL, a Codex rollout, Grok Build's `updates.jsonl` stream, a Pi session JSONL, an Antigravity CLI conversation log, or a GitHub Copilot CLI `events.jsonl`) in bounded pages: the newest turns load first and **Load older** (or scrolling to the top) pulls in the previous page, so a multi-MB session never gets read whole. One tap always surfaces an older prompt or reply (issue #1120): a stretch of heavy tool output (screenshot results are ~1 MB each) used to fill a page with nothing but hidden tool calls, so the tap looked dead. Now the reader charges a huge line only 64 KB against the page's 2 MiB budget, capped at 16 MiB of real reads per request (worst case measured at ~75 ms, against ~6 ms before). The phone also keeps fetching, up to 8 pages or 2.5 s, until a turn arrives. If it hits that bound, the button says how many tool calls it loaded. If the file start holds only tool calls, the list ends on *Start of transcript — no older messages*. **Chat mode sends too, for both session kinds** (issue #983): the same composer as the terminal — tall predictive textarea plus mic · keys / image · send — is docked under the chat, and ➤ Send posts the message to the session's input route, and the sent turn appears a few seconds later through the same live refresh as everything else, appended without rebuilding the pane. The toast says what actually happened rather than just "sent": *Sent* when the session-host confirmed the submit, *Sent, not confirmed* when Enter went in but nothing verified it, *Queued* when the agent was still busy and Enter goes in once it settles, and *Not submitted* (in red) if the text reached the agent but Enter never did; a failed send — the terminal never echoed it, the console did not take it, the session exited — says *Send failed* and keeps the text to retry. On a desktop browser with a real keyboard, **Ctrl+Enter sends** (⌘+Enter on a Mac) so a message can be typed and sent without reaching for the button (issue #1072). The plain return key is unchanged everywhere and still inserts a newline — multi-line prompts are the normal case, and a phone's on-screen keyboard has no Ctrl or Cmd key to press, so nothing about the soft keyboard changes. The shortcut goes through the same Send as a tap, so it respects the same gate: on a session whose Send is greyed out it says why rather than sneaking the message through. Chat mode only — the terminal has its own input path and keeps it. A full-control session sends this way even while its terminal is connected, because Chat can't show the terminal and the route's verdict is the only feedback it has. The image button attaches a file the same way it does in the terminal: the file is stored next to the project and its path is added to the message for review, so attach works for a detached session too. The ⌨ keys drive a full-control session's live terminal even while Chat is showing (answer a y/n prompt without switching) once Terminal has been shown for that session; a detached session has no terminal, so its keys are greyed out. The phone's floating tab bar hides while the session view is open. Served behind the same Tailscale + passkey gate as the terminal. Chat never needs the launcher's terminal capture, so a detached session of a readable agent started on the PC reads exactly the same from the phone (a detached row of another agent has no Chat item). Agents without a history file and missing or unreadable files say so explicitly ("No transcript found" is a different line from "Couldn't read the transcript"). The menu's pencil opens **Rename / link** for Claude and Codex full-control sessions: Claude exposes and copies the provider-native `claude.ai/code/session_…` remote-control URL captured from its terminal, while Codex says **Not available yet** because its local sessions currently have no web URL. Detached sessions and other agents keep the rename-only dialog. For a **detached** session (issue #967), Send types the text into the session's console window on the PC and presses Enter, so a long detached run can be steered from the phone without converting it to a full-control session; its chat shows *Detached session — no terminal. Delivery is typed into the PC console and not confirmed.* above the composer, and the placeholder reads *Message for the agent*. Send is enabled only for agents a recorded probe proved take console input this way, and **all six now do** — Claude Code, Codex, Pi, GitHub Copilot and Antigravity from issue #967's probe, and Grok Build since #1069 re-ran that probe for the one agent #967 had to leave out (it was blocked on a device-code login at the time, never on a failure). An agent added later without a probe keeps the composer usable but greys ➤ Send out, and **a tap on it says why**: it is `aria-disabled`, not a genuinely disabled button, because a disabled button fires no tap at all on a phone — which is how a detached Grok session came to sit there doing nothing, with the reason stranded in a hover tooltip no phone can show (#1069). Delivery is **unconfirmed by design**: a detached console has no output stream the launcher can read, so the API reports `delivered: "unconfirmed"` (never `true`) and the toast says *Sent, not confirmed* — check the console, or the chat after its reload, if it matters. If the PC's session-host is still running a build from before detached input, the toast says *session-host restart needed* (with the running build's sha) rather than a bare HTTP error. A newline in the message becomes a soft line break in the agent's composer; the one Enter that submits is sent separately after a short settle. The **⚙️ Coding options** card — now the **last card on the tab** (issue #496: configuration moves out of the way of launching; collapsible, collapsed by default) — has a Claude Code subsection (model / effort / permission mode / verbose / debug), an Antigravity subsection (`--dangerously-skip-permissions` / `--sandbox` toggles), and a GitHub Copilot subsection (the `--allow-all` toggle). Neither Antigravity nor GitHub Copilot has a launch-time model picker — pick the model with `/model` in-session. For Copilot that is a deliberate removal (issue #1017): the launcher used to send `--model`, but Copilot resolves the id against the **account's entitlement** at launch and silently falls back to `auto` when it refuses, printing one line that scrolls away — so the Settings picker was asserting a model the session was not running. Five ids from Copilot's own `copilot help config` list were refused five times out of five when measured, and Copilot's `/model` picker says so itself — it warns that *"the --model argument will be overridden"* and lists what the current plan excludes. Which ids those are depends on the plan and changes with it, so no list in this repo can predict them: the launcher sends nothing and Copilot's own `/model` (session) and `/config model` (default) own the choice, showing live what the plan actually allows. See [Interactive terminal](#interactive-terminal-from-the-phone) for the security model.

  A foldable **🗺️ System map** section (issue #173) sits below the project list (above the options card since #496). It surfaces the fleet system map — `architecture/system-map.png`, rendered by [`fleet-config`](https://github.com/ferraroroberto/fleet-config)'s `/system-map` job — so *"see my whole system"* is one tap from the phone, any time, instead of waiting for the weekly Slack image. The PNG loads lazily on first expand and opens full-screen (pan/zoom) on tap. The section hides unless a rendered map exists under the **Fleet config folder** set in Settings (default sibling `../fleet-config`). The image endpoint is gated like the live terminal **minus the passkey** — bearer-token **and** Tailscale-only (refused over the Cloudflare tunnel) — so the map never leaves the tailnet.
- **Apps** — every `*.bat` under your scan root that the classifier recognises as Streamlit, a FastAPI webapp, or a Cloudflare-tunnel script. Each row is an **action-row** (issue #1128): **tapping it** runs the bat in a fresh CMD window you can watch (#790's ⚡), and its one **⋮** kebab offers **Launch hidden**, which runs the identical command line with **no window on screen** (#790's 🚫👁 stealth) — the phone-first case, where a console popping up on an unattended PC is pure noise. Both modes spawn the same way (`cmd /k`, direct child), so a stealth-launched app still binds a port and still appears in **Running apps** with a working Stop. A request that names no mode launches visibly, so an older cached PWA bundle never goes silently invisible. **Trays** rows work the same, with the tray's **autostart switch** as the row's one leading toggle. Every row is the name, then one context line — the kind in sentence case, and for a tunnel whether it is **Up** or **Down** (refreshed every 4 s); in **Edit mode** that line shows the bat path instead, and the kebab gains **Rename** and **Remove**. A tunnel's live URL is never rendered as text (a cloudflared URL carries a `?token=…` and wrapped to three lines): the kebab's **Open link** and **Copy URL** carry it. **Running apps** and **Port listeners** rows (issue #1129) are action-rows too: tapping one opens the app over Tailscale (from `tailnet_host`, greyed out with the reason when it's unset), and a listener row with helper services folds them open instead. **Stop** and **Stop process** sit last in the row's ⋮ menu, after a divider and in the danger colour, and ask before they act. Every destructive row-menu item app-wide, including a session's Stop, a job's Remove and a conversation's Delete, gets that same last-after-a-divider treatment; no row shows a visible danger button.
- **Jobs** — one-shot Python scripts and scheduled jobs (`.py` or `.bat` targets), fired from the phone, the Stream Deck, or Task Scheduler through one shared executor with a uniform run history. Every row reads as four fixed lines (name / type+schedule+countdown / duration percentiles + last-7 sparkline / last-run meta) so the same information lands in the same place across jobs.
  - **Ordering & schedule** — the list defaults to next-run order (ascending by each job's computed next-fire time, so imminent dailies float above weeklies and manual/paused jobs sink to the bottom), with a header toggle to A–Z (the choice persists). Each scheduled row carries a relative countdown chip (`⏱ next in 3h`); a concurrent run is reported separately as `running now`, since it doesn't consume the next scheduled fire. A foldable **🗓️ Schedule** panel above the list shows the next 7 days as a day-grouped agenda (`Today` / `Tomorrow` / weekday), with dense minutely/hourly jobs collapsing to a "frequent" footer.
  - **Run history** — tap a row to expand recent runs and the most recent output tail; a live selection streams over WebSocket, finalized logs load once. Jobs can preserve downloadable files (`JOB_ARTIFACT_DIR`), and a run can be pinned to survive normal retention. The card search box matches job names first (issue #1132), then greps every indexed run output. CPU and peak RSS surface on the selected run; tap the output pane to copy the whole log.
  - **Health & alerts** — a stuck run (over `max(p95 × 3, 300 s)`) gets a ⚠️ marker and a kill button. A schedule that isn't firing at all — missing/disabled Task Scheduler entry, an elapsed slot with no run record, or a `session_less` job whose entry is still registered "Interactive only" and so does nothing while the box is logged out — gets a red **⚠ not firing** pill, re-scanned in the background and reporting `unknown` rather than a false alarm when Task Scheduler can't be queried. Failures can push a Pushover notification (optionally with an LLM-generated root-cause line) via `notify_on_failure`; a job flagged `alert_on_failure` additionally pushes a per-job Telegram alert (🔔 bell icon) — opt-in, so a shared chat isn't spammed by every failure. **A run is only called failed when it is one** (issue #916): fleet-config's scheduled-run adapter reserves several exit codes for "delivery was never established", and those render as a third, amber **not confirmed** state — on the row, its sparkline, the Board card and the alert alike — rather than as red. They are excluded from the 30-day success rate and break rather than extend a failure streak. See [Jobs tab](docs/jobs-tab.md) → "Terminal outcomes".
  - **Authoring safety** — saving a job runs a pre-flight (a missing script blocks the save; a `.py` with no `.venv` warns); edit mode adds a 🧪 dry-run check plus a *Dry-run* run-dialog checkbox (`JOB_DRY_RUN=1`); a job can require confirmation before firing. Flags: `visible` (fires under a real, output-teed console window instead of silently), `elevated` (an externally managed `/RL HIGHEST` Task Scheduler entry, no Run-now/pause controls), `session_less` (runs whether or not anyone is logged on — an externally managed S4U entry, mutually exclusive with `visible`, with the elevated registration command generated for you), and webhook-target (fired by an external provider's signed `POST /api/jobs/<id>/hook`, mapped into typed params via JSONPath).

  Schedules materialise as Windows Task Scheduler entries under `\AppLauncher\` — the same executor regardless of trigger. See [Jobs tab](docs/jobs-tab.md) for the full data model, job kinds, and API reference.

- **Life OS** — one tile per skill in your [`life-os`](https://github.com/ferraroroberto/life-os) checkout (the directories under `<life_os_dir>/.claude/skills` whose name doesn't start with `_`, listed live and alphabetically — a new skill folder appears with no restart). Where the Coding tab answers *"run a coding agent in project X"*, this answers *"invoke productivity skill Y, ready for me."*
  - **📓 Weekly recap** — pinned above the skill list, its staleness badge tracks the mtime of life-os's `_recap/memory/ledger.json` (green fresh, amber past 7 days, red past 14) plus a *"draft ready"* hint when a headless draft awaits review; **🚀** launches the interactive `/weekly-recap` review. The draft half runs headless on a schedule — a weekly Jobs entry (`weekly-recap-draft`, Sun 21:00) runs `life-os/.claude/skills/_recap/run-weekly.bat` (`claude -p "/weekly-recap draft"`).
  - **Launching a skill** — **tap a skill's row** to launch it (issue #1128); its ⋮ kebab holds **Read** (browse what it knows) and **Conversations**, and a leading ★ pins it to the top. The Skills toolbar mirrors the Coding selector: Claude Sonnet/Opus/Fable invokes the native slash command, while Codex Luna/Terra/Sol/Astra receives an explicit instruction to load the same project skill from its `.claude/skills/<name>/SKILL.md`. History offers Claude and Codex models; native resume requires a model from the capture’s original harness.
  - **🕘 Conversations** — the row's ⋮ → **Conversations** opens that skill's conversation index (date + digested topic, expandable decisions and open loops); 🔎 searches across skills. A **sort toggle** in the overlay's top bar orders the list — **Recent** (the default: most recently interacted with first, derived from when the capture file was last written, so a conversation you resumed yesterday floats above one merely created later) or **Created** (the capture's own date-stamped order). Rows carry both dates — the active sort's on top, the other beneath it when the two differ — the choice is remembered on the device, and it re-orders without a refetch. **While a query is active the toggle gains a third state, Relevance** — the server's own rank order, best match first, and the default whenever you are searching; tap through it to sort the hits by Recent or Created instead. Clearing the query drops that transient choice and returns the browse list to its remembered date sort. Each expanded row shows its source harness and leads with **Resume in Claude/Codex** (issue #1137) — greyed out with its reason on screen when the selected model's provider doesn't match, beside **Start new in Claude/Codex** when a handoff applies, and following the model combo in place — then a **📖 Read** button, which opens the capture in a read-only **conversation viewer** (issue #1119), and **Copy link** (issue #1170): the same chat-style cards as a session's Chat mode, your prompts and the agent's replies expanded, rendered by the same code. There is no composer; to continue a conversation, resume it. The viewer's **⋮** menu carries every action: **Resume in Claude/Codex** / **Start new in Claude/Codex** (same rules as before, and when Resume is unavailable the reason is shown under the bar), **Show tool calls** (greyed out, because captures don't record tool calls), **Copy link**, **Rename**, **Open raw**, which opens the capture file as plain text, and **Delete** (with the same confirmation), last. **Copy link** — on the row and in the ⋮ menu — copies this launcher's `?convo=<skill>/<file>` link, toasting *Launcher link copied (tailnet only)*: opening it lands on the Life OS tab with that conversation open in the viewer, over its skill's Conversations list. Like every conversation read it only resolves on the tailnet from an enrolled device; a link whose skill or capture is gone (renamed, deleted) says so in the Conversations overlay. On a row, Resume (or Start new) is the one tinted action, Read and Copy link are outlined, and all of them are the same height. A capture the viewer can't parse into turns says so and offers the raw file instead. Native resume validates the selected capture's current bytes, source harness and session ID server-side; index/search metadata cannot substitute another session. Choose a matching model to resume, or select another harness and explicitly confirm **Start new in Claude/Codex**. That starts a new conversation carrying only the selected capture and its source provenance, with transcript instructions treated as historical data. Native IDs are never converted. Memory promotion and knowledge edits still require your approval.
  - **History limits and legacy captures** — missing files, missing IDs, unknown harnesses (including unverified Pi/Grok native resume), unavailable CLIs and an unavailable shared reader each show an explicit reason. Captures without verified identity remain readable. A cached Claude client sending only an ID is accepted only when one existing capture in that skill proves that exact Claude session; ambiguous or unverifiable selections require refreshing history. A new capture view detects edits/renames before launch and asks you to refresh. Captures over 256 KiB remain viewable but cannot launch; a handoff includes at most the first 24,000 characters and states truncation before confirmation and in the new session. Handoffs are private JSON files in the app runtime-data directory's `life-os-handoffs/`, never shell text or session logs. Failed launches remove their artifact; subsequent handoffs remove owned artifacts older than 24 hours. The source capture remains unchanged. Handoffs transfer the selected capture only; until you invoke the skill in the new session, automatic capture may save to the archive. The shared reader/search require **Fleet-config dir** to point at an installed fleet-config checkout with its existing `.venv`. Native history availability is ultimately checked by the original CLI when it resumes; the launcher never scans unrelated native transcript stores.
  - **Access** — Browse, Conversations, targeted resume/handoff, and the skill and weekly-recap launches (#1036: anything that spawns a coding session is terminal-grade, like the Board's issue-start) are gated harder than the rest of the app: Tailscale-only, refused over the Cloudflare tunnel, passkey-gated (the same gate as the live terminal), and the file-content endpoint is path-jailed to `life_os_dir`. See [Interactive terminal](#interactive-terminal-from-the-phone) for the gate.

- **Board** — one screen answering *"what needs me now, across everything"*: a read-only kanban over five **computed** columns, each holding one kind of card (a card moves because reality changed — there is deliberately no drag-and-drop). **Backlog** (open GitHub issues), **Claude's turn** (live sessions working / idle / unknown / finished-clean), **Your turn** (sessions stalled, blocked on a decision, or otherwise waiting on your input — the only column that needs you), **Other** (open PRs plus today's failed or stuck job runs), and **Done** (today's closed issues). On the phone the columns are a swipeable one-column-per-screen carousel with a count strip on top (the *Your turn* count highlights when nonzero); desktop shows all five side by side, each with its own `(N)` header count. See [Board tab](docs/board.md) for the columns' exact data sources, the agent-aware session-state join, and the transcript/conversation-source hierarchy.
  - **Presence & ghost suppression** — the session-host list is authoritative for launcher-owned presence and agent identity; [`fleet-config`](https://github.com/ferraroroberto/fleet-config)'s sessions-state file is only a semantic overlay. An unmatched external row is suppressed outright once it fails two deterministic ghost checks (its own session is no longer live, or its transcript is already claimed by a live matched card) before falling back to a transcript-recent-activity window — so a missing cloud/bridge transcript or a hard-kill leftover can't claim work for 24 hours.
  - **Git-state colour & in-progress marker** — Backlog cards are colour-coded from the same client-side git-status cache as the Coding tiles (red = dirty tree, yellow = off its default branch), never from the board's 5 s poll. A separate `active-issues.json` lifecycle marker (written by `fleet-config`'s issue workflows) tints a card already in flight, labels it "in progress", and disables both Start/YOLO actions; a missing, corrupt, or stale (>24h) marker degrades to an ordinary actionable card. A marker whose owner lane is provably gone reads "stale claim" and stays startable; an unverifiable owner keeps the lock, labelled "in progress (unverified)" (#948).
  - **Drill-down drawer** — tapping a live session opens an inline drawer: the last exchange, the same shared composer as the session view (🎤 mic · ⌨️ keys / 🖼 image · ➤ send — keys stay disabled there, since the Board opens no terminal socket), then four equal buttons — ✏️ Rename, ✕ Stop (the unified stop path), 💬 Chat and ⌨ Terminal last (#984) — in one row wherever the drawer is wide enough, folding to 2×2 and then one column in a narrow desktop Board column so none drops under the 44px floor (#1174). Rename opens the same **Rename / link** dialog as the Coding tab, and since #1096 shows the same provider-native link for a live Claude session — the Board payload carries `web_url` too, where it used to say *Not available yet* for a session the Coding tab linked fine. Detached sessions get the composer too: a reply is typed into the PC console with the "Sent, not confirmed" wording. An action a session can't take (Terminal for a detached session, Chat for an agent with no transcript reader) stays in the row, disabled, and a tap toasts why. The conversation preview is agent-aware — structured Claude/Codex history wins when it correlates safely, the launcher's own PTY capture is the fallback — and all parsing happens on the drawer request, never the 5 s poll.
  - **Dispatch bar & the fleet chief** — a pinned dispatch bar spawns a brand-new session from a spoken or typed goal (injection-safe by spawn-then-type; Tailscale-only + passkey-gated). Its mode dropdown — `add` / `build` / `yolo` plus **chat** — reroutes to a standing **fleet chief**: a conversational orchestrator you can ask *"what's open in app-launcher?"* by voice and then direct (*"ok start 229"*) in the same conversation. The chief is a normal PTY session (crown-marked, confirm-before-kill everywhere it appears) spawned in the `fleet-config` checkout so its brain — the `/chief` skill versioned there — loads while app-launcher's own context never does. It comes back via lazy ensure on first message or a manual Start/Restart button (a graceful stop-then-respawn that hands its own compact-and-continue handover log to the fresh session), and a ⚙️ gear beside the chat bar edits its settings (model, worker cap) in place. There is deliberately no scheduled restart — an unattended one would discard a live batch's context instead of protecting it, so a chief restart is a deliberate operator action only. A chief spawned outside `ensure` self-heals its label from the PTY's first submitted line or Claude Code's own persisted conversation identity, so the Board, the worker cap, and `fleet-config`'s lookup never lose track of it.
  - The Board carries the same two quota rows as the Coding tab — this is where the heavier sessions get launched, so both agents' headroom is readable without touching the dispatch model selector. Reset stamps use the browser's local timezone (clock for the 5-hour window, day for the weekly one).

The **Apps** tab is backed by a registry file (`config/apps.json`); the **Jobs** tab by `config/jobs.json`. The **Coding** and **Life OS** tabs need no registry — they list directories live. **Settings** (issue #383 — previously an always-visible panel at the bottom of every tab; a tab until #1131, now opened from any page header's ⚙ gear) holds the occasional-use actions: **🔎 Scan** walks the **Apps folder** and shows what's new in a checklist; it's where you set the **Projects folder** and its **Hidden projects** list (each Settings field carries one help line saying what it is for, #1176), and it carries the **terminal-access passkey enrollment** section (#795, restoring the #383 review round's drop). (The light/dark **theme toggle** sits in every page header — issue #496, #1131.) Its first card is **Text size** (issue #1134, the fleet's vendored `text-size`): **Small · Default · Large** scale the root font-size (15 / 16 / 18px body text), so all type grows while the tab bar, rows and hit targets keep their size — the app locks pinch-zoom, so this is the way to read bigger. The choice is stored per device (`app-launcher.textsize`, beside `app-launcher.theme`) and applied by the pre-paint boot script, so a reload or PWA relaunch never flashes the old size. The Settings, API tokens, and Context filter cards are each a **collapsible disclosure**, closed by default (issue #719). **Edit mode** — a single ✏️ toggle in the **Jobs tab's Registered-jobs panel header** (issue #719 removed an earlier duplicate control from the Settings card) — reveals per-row ✏️ rename and 🗑️ remove on Apps rows plus the **➕ Add job** button + 🧪 dry-run / ✏️ / 🗑️ controls on Jobs rows (▶ run and ⏸ pause stay in the normal view) — off by default, so the lists stay icon-free in normal use. Every top-level panel across the four original tabs is a **collapsible section** (issue #226) sharing the Code tab's chrome — same chevron, same collapsed height — so the Apps (Running apps / Port listeners / Apps / Trays), Jobs (Registered jobs) and Life (Skills) panels each fold away to cut scrolling on the phone. Defaults (#383 review round): the working-set panels (Running sessions, Running apps, Registered jobs, Skills) open; the long occasional lists (Projects on the Code tab, Port listeners, Apps, Trays) collapsed.

Smart-kill: the Apps tab's Port-listeners panel polls common app ports (8443, 8444, 8445, 8501, 5050) and lists what's actually listening. One tap stops the right PID — no hardcoded "kill :8501" buttons that fire blind. A parent app that owns dependent helper services (issue #224 grouping) keeps its child rows collapsed behind a tap on the parent row (issue #480) — the chevron marks the rows that expand; killing works from the collapsed parent or any expanded child.

---

## Install

```powershell
cd app-launcher
.\setup.bat
```

That creates `.venv`, installs deps, and generates the PWA icons. After this runs once, `tray.bat` is enough for day-to-day use.

**The icon step needs a `project-scaffolding` checkout beside this repo** (or `PROJECT_SCAFFOLDING_DIR` pointing at one): `scripts/gen_icons.py` is a thin caller onto that repo's shared `brand_gen.py`, and `setup.bat` propagates its failure, so without it the install stops at step 3. The generated icons are **committed**, so on a fresh clone that step is only re-running what is already in the tree — if you don't have the sibling checkout, run the first two steps by hand (`python -m venv .venv` then `.venv\Scripts\python.exe -m pip install -r requirements.txt`) and skip it.

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
> The Coding options explicitly select **Luna**, **Terra**, or **Sol** and show
> only reasoning levels supported by that model. Each model remembers its
> effort; Luna defaults to **Extra High**. The **Permission** selector mirrors
> Claude's: *Auto mode* runs with no prompts but keeps the sandbox; *Skip
> permissions* is the all-bypass switch.
>
> Like `agy`, `codex` is resolved against the **effective** `PATH` (issue #668
> — the inherited environment plus the machine and user registry values), so
> installing it enables the button on the next detection poll, no restart
> required.

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
> The launcher resolves `agy` against the **effective** `PATH` — the inherited
> environment plus the machine and user registry values (issue #668) — so an
> install lands on the next detection poll with **no restart at all**. A tray
> restart is only needed when `src/agents.py` itself changes (a newly
> *registered* agent, not a newly *installed* one). A bare `tray.bat` re-run is a no-op when a
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
> Like `agy`, `copilot` is resolved against the **effective** `PATH` (issue
> #668 — inherited plus both registry values), so installing it enables the
> button on the next detection poll without restarting anything. Only a change
> to `src/agents.py` itself needs a restart.

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

Verify with `pi --version`, `pi --list-models claude-agent-sdk`, and `pi --list-models openai-codex`. The Coding options offer Claude 5 **Opus/Sonnet/Fable** and GPT-5.6 **Luna/Terra/Sol**, plus Pi's full `off` through `max` thinking ladder.

> **Why the SDK extension is required.** Pi's *native* `anthropic` provider
> bills metered API "extra usage" credits, **not** your subscription — so the
> launcher launches the Claude models as
> `pi --provider claude-agent-sdk --model claude-agent-sdk/<model>`, which routes
> through the Claude Code subscription quota instead, and the GPT option as
> `pi --provider openai-codex --model openai-codex/gpt-5.6-sol` (your ChatGPT-plan
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
> Two different things are needed for the Pi button to work, and only one of
> them costs a restart. **Installing** the CLI is free: `pi` is resolved
> against the effective `PATH` (issue #668), so a fresh install lands on the
> next detection poll. **Registering** the agent in `src/agents.py` is not:
> the `:8446` session-host imports that module at *its* start, so upgrading
> the launcher to a build that adds a new agent needs a session-host restart
> (`scripts/restart-session-host.ps1 -Confirm`, which ends every live PTY)
> before the button works — otherwise the launch fails with
> `unknown agent: pi`.

### Installing the Grok Build CLI

The Coding tab can also launch **Grok Build** (`grok`) — xAI's Rust terminal
coding agent. The tab's Grok button stays disabled until `grok` is on `PATH`.
Install it with the official installer:

```powershell
irm https://x.ai/cli/install.ps1 | iex
```

The installer downloads `grok.exe` (plus the headless `agent.exe`) to
`%USERPROFILE%\.grok\bin\`, adds that folder to your **User PATH**, and the CLI
self-updates thereafter. Verify with `grok --version`.

> **Authentication is not the launcher's job.** Sign in *inside the session* —
> Grok's welcome screen starts a browser/device-code OAuth on first launch
> (`grok login` works too, and an `XAI_API_KEY` env var is the headless
> alternative). The launcher only resolves the `grok` binary on `PATH` and
> spawns it.
>
> The ⚙️ Coding options card carries a **Grok Build** subsection (issue #667):
> **Reasoning** (`low`/`medium`/`high` → `--reasoning-effort`) and
> **Permission** (Auto mode → `--permission-mode auto`; Skip permissions →
> `--permission-mode bypassPermissions`). Both persist as `grok_effort` /
> `grok_permission_mode` and ride a Resume launch too. There is deliberately
> **no model picker** while `grok models` lists only `grok-4.5` — the same
> call the launcher makes for Antigravity — so the model stays the account
> default, switchable in-TUI with `/model`. Resume reopens the folder's
> **most recent** session (bare `--resume`, Antigravity's shape rather than a
> Claude-style picker).
>
> Same split as Pi above: **installing** `grok` needs no restart — it resolves
> against the effective `PATH` (issue #668) and appears on the next detection
> poll — but **registering** the agent (`src/agents.py`, imported by the
> `:8446` session-host at its own start) does, so upgrading to the build that
> first added Grok needed a session-host restart before the button worked,
> otherwise the launch failed with `unknown agent: grok`.

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

`GET /api/version` also reports a `session_host` block (`{"reachable", "git_sha", "started_at", "stale", "stale_relevant"}`, issue #615, scoped by #635): the session-host on `:8446` is deliberately excluded from `tray.bat --restart`'s reclaim sweep to protect live PTYs (project-scaffolding#35), so it can keep running code that's days old with nothing else surfacing that. `session_host.git_sha` is the SHA that process loaded at *its own* start (not live git state); `stale` is `true` when that differs from `deployed_sha` (the repo's resolved default remote branch tip, e.g. `origin/main`, resolved fresh on every call) — a raw fact, true after *any* merge anywhere in the repo. This is deliberately **not** `head_sha` (also in the same payload, but informational only): #641 found that comparing against the live checkout's current branch tip reports a false `stale_relevant: false` whenever the primary checkout transiently sits on an unrelated feature branch. `stale_relevant` scopes that to whether a declared session-host path (`app/session_host/`, `src/session_host.py`, `src/vt_snapshot.py`, `src/agents.py`, `src/audit.py` and the rest of the host's import closure — parsed live from `CLAUDE.md`'s `## session-host` block, which `tests/test_session_host_paths.py` holds to that closure so the list can't silently drift out of date, #923) was actually touched between the two SHAs. Both read `null` — never a confident false — when either SHA, or the scoped diff itself, can't be resolved. See [`docs/restart-and-liveness.md`](docs/restart-and-liveness.md) for what a `stale_relevant: true` session-host means for shipping, and `CLAUDE.md`'s `## session-host` block for the one supported way to restart it (`scripts/restart-session-host.ps1`).

### If the webapp stops answering

The tray runs a health watchdog: every 60 s it round-trips `GET /healthz` on its own webapp — a wedged uvicorn still *listens*, so only a real response proves it's alive. After 3 consecutive failed probes it raises a Windows toast and appends a timestamped line to `webapp/watchdog.log`; the first successful probe afterwards logs the recovery. Recovery stays manual and canonical: `tray.bat --restart`.

The same distinction is now built into the tray's own view of the webapp (#1005). A probe can establish three things, not two: `/healthz` answered (**answering**), something holds the port and would not say (**bound but not answering** — health *unknown*), or nothing is listening (**down**). The middle state is never folded into "running": the tray refuses to adopt a port it can't confirm is serving, so a boot onto a wedged `:8445` now fails loudly with a toast naming the port instead of announcing "Launcher webapp ready" for a webapp that answers nothing; **ℹ️ Status** titles it *"Launcher status — NOT answering"*; and **🔄 Restart webapp** says the port is wedged rather than the misleading "started externally". A wedge the tray started itself is still stoppable and restartable from the menu — only one it doesn't own is left alone. A uvicorn that has bound but not finished booting is given the same startup budget a fresh spawn gets before any of that fires.

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

Launching a Coding-tab project in **full control** mode (the default — the ☁️ Detached toggle off) opens a **live terminal** — the same thing you'd see in the CMD window on the PC, streamed to the phone: real output, scrollback, typing, `Ctrl+C`, `/quit`, and image paste. This works the same for any coding agent (Claude Code, Codex CLI, Antigravity CLI, GitHub Copilot CLI, Pi, or Grok Build). Tap a `⚡ full control` session in the list to re-attach. The terminal is the **Terminal mode** of the session view (issue #982): the bar's Terminal ⇄ Chat toggle switches to **Chat mode** (issue #953 — the whole conversation, prompts and replies expanded, tool calls folded; see the Coding-tab overview above) and back without reconnecting the terminal, and the ⋮ menu's **Terminal** / **Chat** items open the view straight into either mode. Its ✏️ action opens **Rename / link**: below the editable launcher title, a read-only URL and link button copy the session's **provider-native** link — for Claude Code the `claude.ai/code/session_…` remote-control URL captured from its own terminal output, which is authenticated by the Anthropic account and so opens from any browser on any network (Codex sessions show **Not available yet**: its local sessions have no web URL). Detached sessions keep the same rename action but do not show a link because they have no streamable PTY; they are steered from Chat mode's composer instead (issues #967/#983), which types a follow-up into the PC console — delivery unconfirmed, see the Coding-tab overview above.

A full-control launch — from the phone **or** from a desktop browser on the PC — opens the terminal in a **dedicated Edge `--app` window on the PC**, not inside the launching browser, so it closes independently when you stop the session, without touching your other tabs (issue #241). **Tapping a row in the running-sessions list re-opens the session the same way:** on the **phone** it streams *in-page* as an ordinary terminal overlay (stopping it just dismisses the overlay — never a browser window), while on a **desktop browser** it opens that dedicated PC Edge window too — or, if one is already open for that session, focuses it rather than spawning a second — so you can close it without fear while the session keeps running headless (issue #282). Either way, **stopping the session closes its mirror window** (issue #20). Each running-session row and PC window is **named from the conversation** (issue #266, extended #396, #458): a **manual rename** — tap ⋮ then ✏️ on a Coding-tab row, or the **Rename** button in a Board card's drawer — always wins first if one is set; it is the one title channel that works identically for every agent, including detached sessions, since it doesn't depend on any agent-native self-naming support (submit a blank title to clear it and revert to the automatic precedence below). The rename is a launcher-side title override only — it renames the session everywhere the launcher shows it (the row, the PC window, the Board) but is deliberately **not** typed into the agent's own CLI: forwarding it as a native `/rename` (issue #503) proved unfixably racy against the live TUI — its `Esc` interrupted the agent's active turn and its submit often failed, leaving the command stuck and duplicated in the prompt — so it was removed in issue #555 (revisiting a reliable `--resume`-picker sync is tracked separately). Absent a manual rename: a genuine shared title from `fleet-config`'s `session_state` hook (Claude Code's own `/resume`-picker title, joined in by cwd — the same cross-tab source the Board tab's session cards use, so the same session shows an identical title on both) wins next; otherwise the agent's own live per-conversation title when it emits one (Claude Code's evolving summary — kept as a same-poll-cycle-faster supplement to the shared title), then a short title derived from your **first prompt** — so two sessions in the same project folder stay distinguishable instead of both reading as the folder name. The agents differ here — only Claude and Grok Build self-name per conversation (each emits an evolving LLM-generated title over the terminal-title channel); Codex/Pi emit just the folder, and Antigravity/Copilot none — so the first-prompt fallback fills the gap, and a manual rename is the one way to fix a title that never settles into something useful. The PC window's title bar shows the resolved name (with a hidden `app-launcher-mirror-<sid>` marker kept in the title for the launcher's close/cleanup scan — stamped before the page signs in, so a mirror stuck on the sign-in screen stays closable, #940), and it updates live if the title later changes. When the same session is open on both phone and PC, the **phone drives the terminal size** and the PC window mirrors it — one ConPTY has one size, so the phone is the single authority and the two never fight over dimensions.

For a Board session whose issue title is known **before** the agent starts, the launcher also supplies that title at spawn through the agent's documented `--name` flag when available (currently Claude Code, Copilot, and Pi). This syncs the native resume picker without interacting with a live TUI. Agents without that interface (Codex, Antigravity, and Grok Build — Grok exposes no spawn-time name flag, though it self-names once running), and titles unsafe to pass through `cmd.exe`, retain the launcher-only name.

**Terminal toolbar**

A small bar sits above the terminal for the things a phone keyboard can't do well, and the **composer** is always docked under it (issue #980, Step 1 of #979 — the session-parity project that gives every session view the same composer and a five-control bar). The composer keeps one shape everywhere: a tall predictive `<textarea>` plus a 2×2 button grid — **🎤 mic · ⌨️ keys** on the first row, **🖼 image · ➤ send** on the second. There is no Compose toggle any more: xterm.js wipes its own helper textarea on every keystroke, so iOS/Android autocomplete can't suggest there, while the composer is a plain textarea, so it can. Type (or dictate, or attach), then **➤ Send** forwards the buffered text to the PTY and the submitting `Enter` as a separate frame (#166). Paste goes into the textarea like any text field — the old bar buttons for Paste, Image, Compose and Keys are gone, folded into the composer. A mic or keys button that can't work here (no voice-transcriber configured; no PTY behind the session) stays in the grid **disabled** rather than hidden, so the grid never changes shape. The composer is rendered by one module, `composer.js`, with a single mount entry point; the terminal mounts it from `terminal-compose.js`, Chat mode from `session-transcript.js` (#983) and the Board drawer from `board.js` (#984). Hidden in the PC mirror window (#37), which has a real keyboard.

- **⋮ Session menu** (#981, Step 2 of #979) — the bar's last control. It is the bar's whole session-management surface, so the bar stays at five controls on a 390px phone (‹ Back · title · mode toggle · 🔊 · ⋮) without scrolling sideways: **Rename** opens the same Rename / link dialog as the sessions list's ⋮ kebab and updates the bar title as soon as you save; **Copy link** copies whichever link actually travels, and the toast names which one it wrote (#1096): for a Claude session whose remote-control card has been captured that is the same provider-native `claude.ai/code/session_…` URL the Rename / link dialog shows — *Claude web link copied* — which opens from any browser on any network. Otherwise it falls back to this launcher's own `?session=<sid>` link — *Launcher link copied (tailnet only)* — which opens straight into this session on any device signed in to the launcher *and on the tailnet* (the terminal endpoints refuse anything arriving over the Cloudflare tunnel, so that link is dead elsewhere by design; unlike the PC mirror's `?terminal=` link, it never takes over the phone's terminal size); **Stop and kill** is the unified stop path (#253) — no confirm, except for the fleet chief. It replaced the permanent in-bar ✕.
- **↓ Latest** (#981) — replaces the bar's old ↓ Jump button. A small pill floats at the bottom-right of the terminal, just above the composer, but only while you are scrolled more than one screen above the newest output; tap it to jump back down and it hides again. At the tail, or a few lines up, it stays out of the way. The same pill (#1140, one shared `latest-pill.js`) floats over the **Chat** pane whenever you have scrolled up off the newest message — tapping it lands on the newest turn and live turns follow again — and over the Life OS **conversation viewer** whenever the end of a capture is off screen (the viewer still opens at the top).
- **⌨️ Keys** — the composer grid's second button opens a popover D-pad **above the composer**, where the thumb reaches it, of arrow / `Esc` / `Tab` / `Enter` keys for iPhone keyboards (SwiftKey etc.) that lack them, so Claude's TUI prompts stay navigable (#36). Disabled when the session has no PTY to drive. Includes two sticky toggles, mutually exclusive — engaging one releases the other: **`⇧` Shift** (#137), which sends **Shift+Tab** — how Claude Code cycles permission modes (auto-accept edits → plan → dangerous-skip-permissions) — and **`Ctrl`** (#986), which exposes **C · U · X** (interrupt `Ctrl+C`, clear-the-line `Ctrl+U`, `Ctrl+X`); those three letters are otherwise disabled so a stray tap can't type a plain letter into the prompt. Either modifier stays held across taps so you can chain a cycle or hit `Ctrl+C` twice; tap the same toggle again, engage the other one, or close the popover to release. `Esc` sends a bare Esc byte and what it does is the agent's own call (#987): Claude Code interrupts a running turn on it, but **Grok Build cancels a turn only with `Ctrl+C`** — Esc there just shows "Press Ctrl+c to cancel the turn" — so use `Ctrl` then `C` to stop Grok. **One tap of `C` is enough** (#1024): each tap sends exactly one `\x03`, and Grok cancels a running turn on the first one (it answers "Cancelling…", then "Turn cancelled by user", and puts the cancelled prompt back in its composer). What Grok does with `Ctrl+C` depends on its state, which is the trap: with **no turn running** the same byte clears its composer, and on an **already-empty** composer it answers **"Ctrl+c: press again to quit"** — a quit confirmation, meaning there was nothing to cancel, not that the tap was lost. Claude Code's `Ctrl+C` is not state-dependent that way, so `Ctrl` stays armed after a tap for chaining; on Grok an extra tap or two is harmless (two idle taps 2.5 s apart only re-showed the hint, so its quit wants a faster double press than a thumb on a phone will manage).
- **🔊 Read aloud** (#190, #197, #203, #206, #210) — a top-bar control **right before the ⋮ menu** (not in the composer — that's for editing), the eyes-free other half of dictation for driving / walking. Tap to hear the agent's **last reply** spoken back — the final answer or the question it's asking you — so you can keep the phone in your pocket: dictate → send → 🔊 → listen → dictate again. The reply is lifted client-side from the xterm scrollback, which on the phone is a raw TUI of redraws, boxes and a live status footer — so detection keys off the same signal the Claude Code mobile app uses to separate reply text from tool output: the **filled bullet `●`** that opens every block, classified by its **terminal colour** (#197). A `●` in the **default / white** foreground is an **assistant reply**; a `●` in a **saturated colour** (green / red / …) is a **tool call** (Bash / Read / …). The colour is read straight from the xterm cell (the `translateToString` text drops it), so the buffer segments cleanly into an **ordered list of reply blocks** with no boundary-walk guessing — and `🔊` reads the **last** one by default (a future "read last N" depth-selector is just a slice of that list). The leading `●` is stripped and the phone's 51-column wraps are de-wrapped into one paragraph. The only residual filter is the per-turn epilogue the TUI prints *below* the final reply, which carries no bullet and so trails the last block: the block truncates at the first `recap:` line, **per-turn timing line** (`✻ Crunched for 5s …` / `Worked for 21m 17s`), **live thinking spinner** (`✻ Cogitating… (4m 39s · thinking)` — even the no-token form, #193) or the spinner's **`⎿ Tip:` hint** (#195) — all matched by shape, not verb, since Claude Code picks a random gerund. The composer box + status footer (folder/branch, permission mode, token count) are dropped wholesale. If the agent is mid-work with no completed reply anywhere it says nothing. When the reply finishes reading it resets the button and pops a **🔊 Finished reading** toast (with a watchdog backstop because iOS fires the speech-`end` event unreliably). Speaking has **two voices behind one button** (#203): when the sibling [`local-llm-hub`](https://github.com/ferraroroberto/local-llm-hub) is reachable, the reply is synthesized through its high-quality **Orpheus** voice (default `tara`). For low time-to-first-audio (#206) it plays **progressively, as the hub synthesizes** (first audio in ~1–1.5 s) — `POST /api/tts/speak` streams the reply as **headerless PCM16** (`audio/L16` + an `X-Sample-Rate` header) and the browser plays it through the **Web Audio API**: read the streaming fetch, convert each int16 chunk to float32, and schedule `AudioBufferSourceNode`s back-to-back on an `AudioContext` resumed in the tap gesture. (This is the technique the hub's own TTS UI uses; an `<audio>` element can't play the hub's open-ended streaming WAV progressively — it just buffers silently — so Web Audio sidesteps the container entirely.) The loopback-only hub never has to be reachable from the phone directly. When the hub is unconfigured, down, or lacks Web Audio, it falls back to the browser's built-in **Web Speech API** (`speechSynthesis`) — on-device, zero server, the iOS Siri-enhanced voices when installed. The button shows when the hub is configured (`state.status.tts`) **or** Web Speech is supported, and a live `GET /api/tts/health` probe decides which path the tap takes; the `/api/tts/speak` stream carries the live terminal's gate (Tailscale-only + passkey — the text is terminal content), while the health probe stays token-only. When the hub is reachable 🔊 becomes a small **dropdown** (#210): **Read aloud** speaks the reply verbatim, while **Summarize & read** first sends it to the hub's cheap `claude-haiku-4-5` for a short, driving-oriented summary — the essence plus any decision you need to take — then shows the summary in a **modal** and reads *that* aloud through the same Orpheus-then-Web-Speech path (the summary `POST /api/tts/summarize` carries the same terminal gate). The modal is readable on its own (so summarize doubles as a quick on-screen digest when you can't play audio) and **auto-closes when the read finishes** — tap it (or ✕) to dismiss early and stop. iOS autoplay needs the audio context to be **user-activated**, and the real audio only arrives after the LLM round-trip, so the tap gesture both arms *and* unlocks the context with a silent sample up front (then `resume()`s again before the audio) — without that the context is created in the gesture but stays muted by the time the summary is ready. With the hub unreachable the menu is suppressed and 🔊 keeps its original single-tap read-aloud. Tap again (or starting a new dictation, or leaving the tab) stops the read-aloud — whichever voice is playing. Hidden only when neither voice is available. Configure the hub URL with `llm_hub_url` (empty disables the hub path).
- **🎤 Dictate** (#165, #168) — the composer grid's first button, so dictation always goes through review-before-send and never streams raw into the PTY. Tap to start recording the mic, tap again to stop; the text drops into the textarea at the caret for editing before Send. While you speak it **streams live** (#168) — audio is chunked to the sibling [`voice-transcriber`](https://github.com/ferraroroberto/voice-transcriber) at a 1 s cadence and a Server-Sent-Events stream of rolling partial transcripts revises the dictated span in place, settling on the canonical text when you stop (so a long note is recoverable on the PC even if the phone dies mid-record). If streaming setup fails it falls back to a single-shot upload of the whole take. The phone never talks to the transcriber directly — the webapp proxies everything over loopback to its consumable session API. Gated exactly like the live terminal (Tailscale-only + passkey). Disabled (greyed, still in the grid) when `voice_transcriber_url` is unset or the browser lacks `MediaRecorder`.
- **🖼 Image** — the composer grid's third button. With photo-ocr configured it carries a small dot and opens a **two-option menu** (the same floating menu component as the sessions list's ⋮ kebab): **Attach image or file** and **Extract text from screenshots**. Without photo-ocr the OCR option hides and the button opens the picker directly. *Attach* uploads one or more phone images or files (#448: the picker is multi-select, so a single gallery tap can pick several screenshots at once; no type filter, so Files works too — #366). A terminal can't hold an image, so each file is saved on the PC and the agent is handed its **file path** (`?inline=1`, #41: the session-host returns the path instead of pasting it), which is appended to the textarea on its own line — so several images + text can be composed and sent together. Uploads happen sequentially; a multi-pick fires one summary toast instead of one per file. Send with an attached path holds the submitting Enter back a beat so Claude Code's path→attachment conversion doesn't swallow it (#450).
- **📷 Extract text from screenshots** (#171) — the 🖼 Image button's second option, the pixel counterpart to dictation. Pick it to **stage** screenshots into a tray above the composer — pick it again to add more, ✕ to drop one. Then tap **Extract text (N)**: all staged images go to the sibling [`photo-ocr`](https://github.com/ferraroroberto/photo-ocr) in **one** call (`POST /api/extract`), so it **collates them into a single deduplicated text** (overlapping shots of one long document are merged, duplicate boundary lines removed — staging is what makes the de-dup possible, vs. one isolated OCR per image). The text drops into the textarea for review before ➤ Send. The Extract button shows a ⏳ elapsed-seconds timer while the hub works. Unlike 🖼 Image (which pastes a file *path*), this pastes the *text read out of the pictures*; model/prompt are photo-ocr's own defaults. The phone never talks to photo-ocr directly. The option hides when `photo_ocr_url` is unset (the Image button then opens the picker directly).

**How it's wired**

- A separate long-lived **session-host** process (loopback-only, port `8446`) owns every `claude` ConPTY. The tray starts and owns it like it owns `cloudflared`. Because it's its own process, a *Restart webapp* doesn't kill running sessions (a PC reboot still does).
- The webapp proxies a WebSocket from the phone through to the session-host. The webapp is the single auth choke point.
- `xterm.js` renders the terminal in the SPA — no build step, vendored under `app/webapp/static/vendor/`.

**Security model — the terminal is not the same as the launcher**

Launching, listing, and stopping sessions stay public (bearer-token gated, reachable over the Cloudflare tunnel). The **live terminal itself does not**:

- **Tailscale-only.** The terminal WebSocket, image upload, and WebAuthn endpoints refuse any request that arrived over the public Cloudflare tunnel (they're rejected on the `Cf-Ray` header) and require a client IP in the Tailscale CGNAT range `100.64.0.0/10` (plus loopback, plus an optional `tailnet_allowlist`).
- **Passkey-gated.** When `webauthn_rp_id` + `webauthn_origin` are set, opening or driving a terminal requires a **WebAuthn platform passkey** — Face ID on the enrolled iPhone. A passkey assertion mints a short-lived (12 h) terminal token; the WebSocket and image endpoints require it.
- **Device whitelist you control.** Enrolled passkeys live in `config/webauthn_devices.json` (gitignored). Enrollment only works during a one-time window you open deliberately from the tray (**🔐 Enroll device** — 5 minutes). Revoke a device from **Settings → Terminal access**.
- **Audited.** Every terminal action is logged: `webapp/terminal_audit.log` (enroll / unlock / session lifecycle, device, client IP) and per-session `webapp/sessions/<id>.log` (input chunks, image uploads) + `<id>.transcript` (full output). The per-session files are kept for **365 days** (`session_retention_days`, see the config table below) and then deleted by a daily sweep; the cross-session `terminal_audit.log` is not touched by it.

> The Claude Code launch runs without permission prompts — by default in **auto mode** (`--permission-mode auto`: a classifier still blocks dangerous actions), or, if you switch the Coding-options selector, with the legacy `--dangerously-skip-permissions` (no safety net). The marginal risk over your existing Tailscale remote access is small (anyone on the tailnet could already RDP in) — the passkey gate + audit log make this surface *more* controlled than plain remote access, not less.

**Enrolling your iPhone**

1. On the PC, set `webauthn_rp_id` (bare tailnet hostname, e.g. `pc.tailnet.ts.net`) and `webauthn_origin` (full origin, e.g. `https://pc.tailnet.ts.net:8445`) in `config/webapp_config.json`, and restart the webapp.
2. On the iPhone, open the launcher over the Tailscale URL.
3. On the PC, tray menu → **🔐 Enroll device (5 min)**.
4. On the iPhone, **Settings → Terminal access → 📲 Enroll this device** → Face ID.

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
.\.venv\Scripts\python.exe scripts\gen_token.py --show     # also echo the value
```

- The generated value is written to `config/webapp_config.json` and is **not** printed unless you pass `--show`. The tray bakes it into the URL it copies (`Tray → Copy Cloudflare URL`), so the normal flow never needs to read it off the console — and a run inside a launcher PTY session has its whole stdout captured to a transcript kept for `session_retention_days`.

- Loopback callers still bypass — *unless* the request carries Cloudflare's own edge headers (#793). cloudflared runs on this PC and dials the webapp over loopback, so a tunnelled request only looks remote once uvicorn rewrites the client address from `X-Forwarded-For`; the edge headers are the signal that survives however that address resolves.
- Remote (tailnet, Cloudflare) callers must present `Authorization: Bearer <token>` *or* `?token=…`. This includes the terminal WebSocket, which re-applies the same gate by hand (Starlette middleware never sees a WS handshake) — a full-scope minted token works there exactly as it does on HTTP, and a job-scoped one is refused there exactly as it is on HTTP.
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
│   ├── api_tokens.py           # job-scoped bearer tokens (Settings → API tokens)
│   ├── notifications.py        # notify_on_failure dispatch
│   ├── notify/                 # provider package behind notifications.py (Telegram, …)
│   ├── github_client.py        # `gh`-backed client for Board backlog/PR/issue data
│   ├── llm_client.py           # local-llm-hub client (root-cause lines, fleet chief, summarize)
│   ├── photo_ocr_client.py     # photo-ocr sibling-service client
│   ├── tts_client.py           # local-llm-hub TTS client (🔊 Read aloud)
│   ├── voice_client.py         # voice-transcriber sibling-service client
│   ├── chief_pointer.py        # fleet-chief PTY session lookup for the Board dispatch bar
│   ├── board.py                # Board backend: kanban column computation
│   ├── board_state.py          # session-state join (session-host + fleet-config overlay)
│   ├── board_sessions.py       # live-session enumeration for the Board
│   ├── board_exchange.py       # drill-down drawer's last-exchange read
│   ├── board_transcript.py     # transcript overlay + conversation-source hierarchy
│   ├── life_os_index.py        # Life OS conversation-artefact reconciliation (index.json/index.md/search db)
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
│   ├── classify_e2e.py         # diff-proportionate e2e-tier routing against `main`
│   ├── e2e-gate-route.ps1      # turns the classifier's verdict into the gate's pytest args + mutex flag
│   ├── run-e2e.ps1             # dev-loop e2e runner (dual-projection, or --browser chromium)
│   ├── restart-session-host.ps1 # confirmation-gated `:8446` restart — see CLAUDE.md `## session-host`
│   └── verify-before-ship.ps1  # the pre-ship gate — disposable webapp + session-host, no tray needed
│
├── config/                     # *.sample.json committed, real files gitignored
│   ├── config.sample.json
│   ├── webapp_config.sample.json
│   ├── apps.sample.json
│   └── jobs.sample.json
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

The `webapp` section also accepts three optional tuning knobs, omitted
above because the defaults are almost always right:
`startup_timeout_seconds` (`15.0`) — how long the tray waits for uvicorn
to answer on `:8445` before declaring the boot failed; raise it on a
loaded box that boots slowly. `request_timeout_seconds` (`1.0`) — per
health-probe HTTP timeout. `poll_interval_seconds` (`0.4`) — gap between
those probes while waiting for startup.

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
| `coding_hidden_agents` | `[]` | Coding-row launch buttons hidden from the project rows (issue #666) — agent ids plus the pseudo-ids `vscode` (since #977 the whole ⋯ project menu: VS Code · Show changes · Open folder) and `github`. Managed by the **Visible agents** switches in the ⚙️ Coding options card. A *hidden* list, so a newly registered agent appears by default. |
| `apps_scan_root` | parent of this repo | Where the Apps tab scans recursively for `*.bat` |
| `life_os_dir` | sibling `../life-os` | Root of the `life-os` checkout the Life OS tab surfaces (skills at `<life_os_dir>/.claude/skills`, identity at `<life_os_dir>/identity`). When the skills dir doesn't exist the tab shows disabled, the same way the Coding tab handles a missing `projects_dir`. |
| `claude_config_dir` | sibling `../fleet-config` | Root of the `fleet-config` checkout whose `architecture/system-map.png` the Coding tab surfaces and whose canonical `skills/_lib/quota_snapshot.py` / `quota_sources.py` own quota validation and Codex refresh. Missing contract code is reported as a quota source error; the launcher does not duplicate its collectors. |
| `terminal_history_lines` | `10000` | Bounded scrollback (200-50000 lines) a full-screen agent (Codex, etc.) session keeps for a (re)connect, Settings-tab configurable (issue #435 follow-up). Too low and the true start of a real conversation becomes unreachable; too high risks a slower reconnect paint on a weak mobile connection. |
| `sessions_state_file` | `~/.claude/hooks/state/sessions-state.json` | The sessions-state file fleet-config's `session_state` hook writes (fleet-config#91), read by the Board tab. Absent/corrupt/stale degrades to `unknown` session status, never an error. |
| `rate_limits_file` | `~/.claude/hooks/state/rate-limits.json` | Legacy Claude 5h/7d cache path and the directory anchor for fleet-config's versioned `quota-v1/` provider shards. The legacy file is used only while the canonical Claude shard is absent; an unknown timestamp never counts as fresh. |
| `context_filter_mode_file` | `~/.fleet-context-filter/mode.json` | The machine-wide off/shadow/rewrite switch for the fleet's PreToolUse context-filter hook (fleet-config#392/#541/#544, issue #713) — the Settings tab's **Context filter** card reads and writes it. A plain file write, effective immediately for every coding-agent session on the machine; absent file reads as the true "off" default, a corrupt file degrades the panel, never an error. |
| `context_filter_log_file` | `~/.fleet-context-filter/shadow.jsonl` | The context filter's savings telemetry (issue #713) — one JSON row per hook invocation (tokens saved, agent, command). The Settings card's stats block aggregates it (cached, recomputed only when the file changes); absent/corrupt degrades to the stats block hiding, never an error. The writer rotates the file at 20 MB keeping one prior generation (fleet-config#549), so the stats totals reflect the current generation and reset at rotation. |
| `github_owner` | `"ferraroroberto"` | GitHub owner whose repos the Board tab's `gh` searches span (Backlog / PRs / Done-today). |
| `chief_model` | `"fable"` | Model the fleet chief spawns on (issue #245; `sonnet`/`opus`/`fable`). Edited from the Board's chief-settings dialog. |
| `chief_worker_cap` | `3` | Max concurrent worker sessions the `/chief` skill may keep running (1-10, issue #245; ceiling raised 8→10 in #547). Read by the skill over loopback via `GET /api/board/chief/settings` — its dispatch rail, phone-tunable. |
| `claude_model` | `"opus"` | Default `--model` for `claude` (Claude Code button only) |
| `claude_effort` | `"high"` | Default `--effort` (use `"off"` to omit the flag) |
| `claude_verbose` | `true` | Pass `--verbose` |
| `claude_debug` | `false` | Pass `--debug` |
| `claude_permission_mode` | `"auto"` | Permission flag: `"auto"` → `--permission-mode auto`, `"skip"` → `--dangerously-skip-permissions` |
| `grok_effort` | `"high"` | Reasoning tier for `grok` (`low`/`medium`/`high`) → `--reasoning-effort` (issue #667). Edited from the ⚙️ Coding options card's Grok Build subsection. |
| `grok_permission_mode` | `"auto"` | Permission flag for `grok`: `"auto"` → `--permission-mode auto`, `"skip"` → `--permission-mode bypassPermissions` (issue #667). |
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
| `voice_transcriber_url` | `https://127.0.0.1:8443` | Base URL of the sibling voice-transcriber webapp the composer's 🎤 dictation proxies to over loopback (issue #165). Empty string disables dictation (the mic button stays in the grid, disabled). |
| `photo_ocr_url` | `https://127.0.0.1:8444` | Base URL of the sibling photo-ocr webapp the composer's 📷 screenshot OCR proxies to over loopback (issue #171). Empty string disables OCR (the option leaves the 🖼 Image button's menu). |
| `llm_hub_url` | `http://127.0.0.1:8000` | Base URL of the sibling local-llm-hub the 🔊 read-aloud's Orpheus voice proxies to over loopback (issue #203). Plain HTTP — the hub serves no TLS. Empty string disables the hub path (🔊 falls back to the on-device Web Speech voice). |
| `pushover_api_token` / `pushover_user_key` | `""` | Pushover credentials for Jobs-tab failure notifications (issue #66). Both must be set; missing creds = no-op. |
| `notify_on_failure` | `false` | Master switch — even with creds set, no push fires until this flips on. |
| `notify_failure_streak` | `0` | When > 0, also fire a separate "N consecutive failures" push when the failure streak ticks to exactly this count. |
| `notify_failure_summary` | `false` | When `true`, pipe the output tail through the local LLM hub at `llm_hub_url` (`claude-haiku-4-5`) for a one-line root-cause line prepended to the push body. |
| `telegram_bot_token` / `telegram_chat_id` | `""` | Telegram credentials for the per-job `alert_on_failure` channel (issue #597). Both must be set; missing creds = no-op. Independent of the Pushover settings above — this fires only for jobs with `alert_on_failure: true`, not globally. |
| `jobs_coverage_interval_minutes` | `60` | Minutes between background missed-fire coverage scans (issue #697) — a scheduled job whose `\AppLauncher\` Task Scheduler entry is missing/disabled, or whose slot elapsed with no run record. A job with *no* run history at all is reported `unknown`, never "not firing" (#737) — an empty run-history store can't distinguish a missed fire from a missing record, and the badge/alert would be a false positive; the structural half still flags a missing or disabled entry regardless. `0` disables the background tick; the Jobs-tab **⚠ not firing** badge still computes on poll. On by default because it pushes nothing on its own — alerts route through the two opt-in gates above. Only the **canonical** instance runs the tick — see below. |
| `session_retention_days` | `365` | **Retention window for `webapp/sessions/`** (issue #902, decided 2026-09-11) — both `<id>.log` and `<id>.transcript`. Once a day the webapp deletes every such file whose mtime is older than this, off the event loop (`src/session_retention.py`). It never deletes a file belonging to a **live** session (checked against the session-host's list), never follows a symlink/junction, and treats anything it can't establish as `unknown` and keeps it: a file it can't stat or whose mtime is in the future is logged and skipped; an unreachable session-host, an unlistable directory, or a clock under which *every* file looks expired stands the whole sweep down. `0` keeps everything forever. Hand-edit only — deliberately not patchable from the Settings tab. Canonical instance only, like the coverage tick. Preview without deleting: `.\.venv\Scripts\python.exe scripts\session_retention.py` (`--days N` to try another window). |

**Only the canonical checkout may alert (issue #736).** The coverage tick pushes to a real phone, so it is gated on `src/instance_role.py::canonical_instance`, not just on the e2e autoboot marker. A **linked git worktree** — a `.git` *file* rather than a `.git` *directory* — is never canonical: it carries the real `config/jobs.json` and the real Telegram/Pushover credentials, but `webapp/jobs/` resolves under *its own* root, so it reads an empty run history and concludes every scheduled fire was missed. That is exactly what happened on 2026-08-10, when a webapp booted out of `app-launcher-wt-727` pushed three false "scheduled run never fired" alerts. A non-canonical instance logs its reason and its root at `WARNING` and starts no tick. Inference cannot distinguish a *second full clone* from the real one, so `LAUNCHER_CANONICAL_INSTANCE=0` force-silences an instance and `=1` force-arms one (e.g. a deployment with no `.git` at all, which otherwise stands down rather than guessing). The `LAUNCHER_SESSION_HOST_PORT` autoboot exemption is checked first and cannot be overridden.

`--remote-control` is always added to Claude Code. Codex fresh and resume launches pass `--model <id>` plus a compatible `-c model_reasoning_effort=<level>`; permission flags are omitted from `codex resume`, whose subcommand rejects them. Full-control Codex sessions additionally carry `-c disable_paste_burst=true`.

### Refreshing model catalogs

Run `claude --version; claude --help`, `codex --version; codex --help; codex resume --help`, `pi --version; pi --list-models claude-agent-sdk; pi --list-models openai-codex`, and `copilot --version; copilot help config`. Update `src/model_catalog.py` only from those harness outputs. A documented API model is not launchable until the relevant installed subscription harness advertises its exact ID; keep rollout entries disabled meanwhile. **GitHub Copilot is deliberately absent from `src/model_catalog.py`** (issue #1017) — `copilot help config` reports what the *CLI* accepts, not what the *account* may launch, so a catalogue here could only produce claims the launcher cannot keep; there is nothing to refresh.

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

Once scanned in, each `tray`-kind row gets an autostart switch in the
collapsible **Trays** panel, sharing the row's control cluster with the ⚡ and
🚫👁 launch buttons. It carries no visible label — the panel is called Trays
and it is the row's only toggle — but its accessible name is the full
"Autostart &lt;name&gt; at boot". When app-launcher's own
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

---

## Verify

```powershell
& .\.venv\Scripts\python.exe -m py_compile launcher.py
& .\.venv\Scripts\python.exe -m uvicorn app.webapp.server:app --host 127.0.0.1 --port 8445 --loop app.webapp.event_loop:selector_loop_factory
# then in another terminal:
curl http://127.0.0.1:8445/healthz
```

### Pytest API tests

In-process FastAPI `TestClient` suite under `tests/` (the sister-project pattern) covering `/healthz`, `/api/config` (GET + POST allow-list, incl. `projects_ignore`), `/api/login` + bearer-token gate, `/api/apps` CRUD, live Coding-tab directory discovery (`src/scanner.py` + the ignore list), coding-agent detection + dual launch (`src/agents.py`, `/api/agents`), `/api/claude-code/sessions` (list + stop), and the **Life OS** tab (`src/scanner.py:scan_skills`, `/api/life-os/*` — skill discovery, the bare `/skill-name` launch wiring + opus model override, the content browser's path-jail, the Tailscale/Cloudflare gate on the content endpoints, and — issue #727 — the conversation index's ordering + derived capture path, the search shell's every-failure-degrades contract, and the targeted resume's UUID validation). Session-host loopback client is mocked — no live tray, no port :8446 needed.

```powershell
& .\.venv\Scripts\python.exe -m pytest tests -m "not smoke" -v
```

Runs in about a second. The `-m "not smoke"` flag excludes the live-tray Playwright suite below.

The same suite carries a few static convention guards that parse the tree rather than exercise it — `test_icon_sprite_coverage.py` (every `#i-NAME` resolves to a vendored sprite `<symbol>`) and `test_subprocess_flags_guard.py` (every `subprocess.*` spawn under `src/`, `app/`, `scripts/` passes `creationflags` resolving to `src/subprocess_flags.py`'s `NO_WINDOW` / `NO_WINDOW_NEW_GROUP`). The spawn guard exists because an unsuppressed spawn only misbehaves under a *console-less* parent — the `pythonw` tray and its descendants — so it is invisible in the terminal where tests normally run, and drifted unnoticed after #585 consolidated the constant. A deliberately-visible console (the Apps tab's `cmd /k` window) is carved out by an explicit, reviewable `path::function` entry in that file's `_VISIBLE_CONSOLE_EXEMPT`.

### Playwright smoke + regression tests

A `pytest-playwright` suite under `tests/e2e/` covers two things:

- **Boot smoke** (`test_smoke.py`) — JS error on boot, empty config form, broken tab switch, the single ✕ stop button per session row (issue #253), missing login overlay.
- **iPhone regression net** — one focused test per closed iOS-only bite, so the next regression of any of them surfaces locally before a deploy instead of after an hour of phone-PC round-trips. `tests/e2e/` is its own index — don't re-enumerate the closed bites here; each regression test's docstring names the issue it pins and what would regress without it (e.g. `tests/e2e/test_paste_framing.py:1`, `tests/e2e/test_bottom_tab_bar.py:1`).

Every test runs in **two projections** — Chromium-desktop and WebKit on an iPhone 15 Pro Max viewport — so engine-specific iOS bugs get caught on Windows before they reach a real phone. A few tests skip on the duplicate projection where the check is browser-agnostic (server-side header inspection, etc.). Pin a single engine with `--browser chromium` (or `webkit`) for a faster dev loop.

One-time setup:

```powershell
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
& .\.venv\Scripts\python.exe -m playwright install chromium webkit
```

**Run after every webapp/SPA edit** with the tray up (`tray.bat`):

```powershell
.\scripts\run-e2e.ps1                       # both projections — the full suite
.\scripts\run-e2e.ps1 --browser chromium    # Chromium-only — the faster dev loop
# or directly — the env var is the explicit live-tray opt-in (see below):
$env:LAUNCHER_E2E_LIVE = "1"; & .\.venv\Scripts\python.exe -m pytest -m smoke -v tests/e2e
```

The suite runs against the live tray on `https://127.0.0.1:8445` — it does not boot anything itself. If the tray isn't up, every test is skipped with a clear message instead of hanging. Loopback access auto-bypasses the bearer-token middleware and the passkey gate, so no credentials are needed.

Because that live instance is the one the phone is using, targeting it is an **explicit opt-in**: `run-e2e.ps1` sets `LAUNCHER_E2E_LIVE=1` for you, and a bare `pytest tests/e2e` without it (and without autoboot) exits immediately with a guard message instead of silently load-testing the live webapp.

The terminal-related regression tests get a live PTY session from one of two fixtures (issue #534) — neither requires any test-only product hooks (no `LAUNCHER_TEST_HOOKS=1` env var):

- **`launched_pty_session`** — the default for UI-only assertions (toolbar, overlay geometry, dictation, readback, session rows, WS wiring, input logged by the webapp). Under the autoboot gate the child is a **deterministic lightweight stub** instead of the real Claude CLI: the harness prepends a generated `claude.cmd` shim to the *disposable* session-host's `PATH` that routes the `--e2e-stub` sentinel to an instant Python echo loop, so ~55 tests × 2 projections stop spawning a real Claude/node process each — removing the host-load variance that ballooned loaded runs, and letting these tests run on CI (the stub needs only Python) — while still exercising the production webapp ↔ session-host ↔ ConPTY boundary. Against the live tray (`run-e2e.ps1`) there is no shim, so it falls back to a real `claude` launch.
- **`launched_claude_pty_session`** — a real `claude` child, only for tests whose assertions depend on the real agent's rendered output (currently the #444 reconnect-replay scrollback pin). It launches in the first checkout of this repository that has already cleared Claude Code's **folder-trust prompt**: the checkout under test, else the main checkout a linked worktree hangs off. The autoboot config pins its Coding scan root to that checkout's parent, so the launch never depends on a sibling folder being named `app-launcher` (#932). A never-opened directory, such as a fresh detached merge-verification clone, shows the trust prompt where the composer should be, and `--dangerously-skip-permissions` does not clear it. With no trusted checkout the test **skips and names trust** (an unreadable `~/.claude.json` counts as *unknown*, never trusted). The harness never answers the prompt or writes that global state. Under autoboot, a launch that still fails is a hard failure, not a skip.

The WebSocket-drop probe and the clipboard mock are injected via `page.add_init_script` from inside each test, so the production surface is untouched.

Byte-loss at the PTY write boundary itself has a dedicated **non-browser** guard, `tests/test_session_host_pty_realpty.py` (in the `pytest tests -m "not smoke"` suite, Windows/pywinpty-gated): it pushes multi-KB payloads through `PtySession.write` into a *real* ConPTY and asserts a byte-for-byte lossless readback. A `MagicMock` PtyProcess can never drop bytes, so this real-PTY readback is what proves the write path is clean — the unit tests in `test_session_host_pty_write.py` only pin the chunk-and-pace shape and the #13 no-retry contract.

**Concurrent first paint** has a second non-browser guard in the same suite, `tests/test_session_host_concurrent_paint.py` (issue #610, Windows/pywinpty-gated). It boots the real session-host ASGI app on a real single uvicorn event loop, spawns five real ConPTY sessions as a concurrent burst through the real `POST /sessions` handler, and attaches five real WebSocket clients — each the instant its own create returns, so late spawns race already-attached pumps. Each session prints a unique sentinel, so the test asserts both that every terminal paints *something* and that it paints *its own* output (the #537 cross-wiring failure mode). A `TestClient` cannot replace it: it runs the app on a separate portal thread, and same-loop contention is the entire failure class. The test prints its measurements — worst event-loop tick gap during the burst, plus per-session spawn and first-paint latency — on a pass as well as a failure, since those numbers are what distinguish a loop-contention regression (the #639/#660 class) from a spawn or transport failure.

**Leaked browser helpers are swept after every session** (issue #709). `tests/e2e/_browser_sweep.py` is a **vendor-verbatim** copy of `project-scaffolding`'s canonical helper (project-scaffolding#203/#204) — do not adapt or re-derive it; re-vendor it byte-for-byte and bump the `sha` in `.fleet.toml`'s `[vendored]` block. `tests/e2e/conftest.py::pytest_sessionfinish` calls it once the whole session — fixtures included — has torn down, scoped to this checkout, and prints a one-line summary naming what it killed and what it deliberately left alone. A kill needs all three of: the process is really running, its parent is dead (PID-reuse-checked), and its working directory sits under this checkout. Everything else gets its own verdict instead — an already-exited-but-handle-held `zombie` (unkillable, harmless, and **never** a gate failure), a live-parent session, a sibling checkout, an unreadable cwd. Chromium is deliberately **out** of the sweep set, so the user's own Chrome is never a target; only WebKit helpers plus WebKit's `Playwright.exe` browser-main process are. The sweep is advisory — it never changes the exit status. It also runs standalone, which is worth doing before a `git worktree remove` that fails as "busy": `& .\.venv\Scripts\python.exe tests\e2e\_browser_sweep.py <path> [--dry-run]`.

### Verifying changes before ship

`run-e2e.ps1` above is the dev loop — fast, but it *skips* the whole e2e suite if the tray isn't up, which is the wrong default for a final check (a forgotten tray looks like a green run). The pre-ship gate closes that hole:

```powershell
pwsh -File scripts\verify-before-ship.ps1
```

It runs the full pipeline as one pass/fail — byte-compile (`app`, `src`, `tests`), the non-e2e pytest suite, then a **diff-proportionate slice** of the Playwright e2e suite (issue #568) — and **boots its own disposable webapp + session-host** on a free port, so it never silently skips:

- A tray on `:8445` may be running or not. Autoboot picks a free port for its webapp and **always spawns its own disposable session-host** on a free port (never the live `:8446`, whose sessions include the user's real Claude PTYs — issue #260). The existing tray is left untouched.
- The disposable instance serves HTTPS reusing `webapp/certificates/` (plain HTTP if no cert pair exists). Subprocess output is captured to `webapp/e2e-autoboot-*.log`.
- The disposable webapp reads and writes its **own temp config** (in pytest's temp dir, via `LAUNCHER_WEBAPP_CONFIG`), so an e2e test that saves settings can never mutate the real config file — the gate asserts the real file is byte-identical after the run and fails loud if not (issue #441).
- **No live credential in a test run (issue #907).** That temp config is derived from `config/webapp_config.json` with every credential dropped (`CREDENTIAL_KEYS` in `src/webapp_config.py` — bearer token, password, Pushover/Telegram credentials, job secrets, minted-token records) and a per-run disposable `auth_token` in their place; the e2e `auth_token` fixture seeds that disposable value, never the real one (loopback bypasses the bearer gate, so no test needs it), and the gate fails loud if a real credential value ever survives into the temp config. Independently, every test report — both suites — is scrubbed before it's written (`tests/_credential_hygiene.py`): known credential values and any `Authorization` / `x-terminal-token` header or `?token=` / `?tt=` query value are masked, because Playwright's failure call log prints request headers verbatim.
- **No test session in the real `webapp/sessions` (issue #913).** `src/audit.py` resolves its directory from `__file__`, so before this the disposable webapp *and* session-host — both spawned from the checkout under test — appended their throwaway `<id>.log` / `<id>.transcript` pairs to the very directory the phone reads — 228 files per full run, against a directory measured at 94,672 files growing ~400/day with ~90% of it test-shaped — and the reason #911's move of the temp *config* into pytest's temp dir didn't help. Both processes now get `LAUNCHER_AUDIT_DIR` pointing at a per-run temp dir, and the gate fails loud if any transcript carrying the `[e2e-stub]` banner (a marker only this harness can write) lands in the real directory during a run. One residual, by design: `LAUNCHER_E2E_LIVE=1` drives the *live* tray on `:8445`, whose env was fixed when it started — those sessions still land in the real directory, which is what opting into live mode means.
- **No test upload in the checkout's `.launcher-tmp` (issue #922).** The same defect as #913 in a different directory: `app/session_host/server.py` saves an upload under `<project>/.launcher-tmp`, and the disposable session-host runs its sessions with `project_dir` set to the checkout under test, so every compose-bar attach test left a real file there — 3,423 of the directory's 3,600 files were the harness's by the time it was measured, with nothing pruning them (a file-count leak, not a disk-space one: they are 1×1 PNGs totalling ~15 KB, and the directory's bulk is real phone attachments). The disposable session-host now gets `LAUNCHER_UPLOAD_ROOT` pointing at a per-run temp root (the `.launcher-tmp` leaf is kept, so a redirected path is still recognisably an upload), and the gate fails loud if any file carrying the harness's `e2e-stub-` filename marker lands in the real directory during a run — marker-keyed for the same reason as the transcript check, so a photo attached from the phone to the live tray mid-run can never trip it. Only the session-host needs the variable: the webapp proxies the upload route and never writes the file. Same residual, same reason: `LAUNCHER_E2E_LIVE=1` drives the live tray, whose env was fixed at start.
- It persists a live progress log to `webapp/verify-progress.log` (gitignored, overwritten each run): phase markers from the script plus one `START`/`DONE` line per test — with per-test totals including fixture cost — and a slowest-15 summary at the end of each pytest phase. If the gate wedges or an outer timeout kills it, the last `START` without a `DONE` names the active test, so a genuinely slow test is distinguishable from aggregate overhead (issue #534). A failing setup or call also leaves its **traceback** under its `FAILED` line — the last 40 lines of the failure text, where the crash message and its frames sit, each line prefixed `    | ` and capped at 240 chars, for the first 30 failures of a run — so a red is diagnosable from disk after the console that ran the gate is gone; the excerpt is taken after `tests/_credential_hygiene.py` has redacted the report, so no credential reaches the log (issue #943). The hooks live in `tests/_progress_log.py`.
- **The e2e phase is routed to the diff (issue #568).** Byte-compile and the non-e2e pytest suite always run; only the *browser* slice is scaled to what the branch actually changed, classified by `scripts/classify_e2e.py` against `main`: a **static-asset-only** diff (images, fonts, webmanifest, vendored HTML sprite fragments) runs `tests/e2e/test_smoke.py` **Chromium-only** (~15 s, no WebKit exposure); a **backend/docs/non-e2e-test-only** diff runs **no browser suite** at all (the non-e2e pytest already covers it); everything on the real browser surface (any `.js`/`.css`, real app pages, webapp/session-host/launcher Python, `tests/e2e/**`) — plus any *mixed*, ambiguous, or unrecognized diff — runs the **full dual-projection** suite unchanged, or only one declared *surface*'s tests when every such path belongs to that surface (next bullet). Routing is **fail-safe**: uncertainty always escalates to the full suite, never narrows it. The chosen tier and the triggering paths are printed to the console and to `webapp/verify-progress.log`. On CI the browser suite does not run at all (#1041): that job is the clean-machine half — byte-compile plus the non-e2e suite on a fresh `.venv` from `requirements.txt` — so the local gate is the only place the browser suite runs, and the only place routing is proven. The path→tier rules live in one reviewable place, the `[e2e]` table in `.fleet.toml`; `scripts/classify_e2e.py` is project-scaffolding's classifier copied byte-verbatim (#955, schema in that repo's `docs/e2e-routing.md`), so never edit it here. `python scripts/classify_e2e.py` prints how the current branch would route.
- **Surfaces narrow a `full` diff to one tab (issue #955).** `.fleet.toml` declares `[[e2e.surface]]` entries — today **board** (`board.js`, `board-dispatch.js`, the `board*` routers, their e2e tests) and **lifeos** (`life-os.js`, the `life_os*` routers, their e2e tests). A diff whose every browser-surface path sits in one surface runs only that surface's `pytest_targets`, still on both projections and still under the machine-wide e2e mutex: the tab's own tests, the tests that drive its modules from elsewhere, plus `test_smoke.py`, `test_primary_nav.py` and `test_bottom_tab_bar.py`, since a broken tab module takes the whole SPA down. Shared files (`styles.css`, `index.html`, `main.js` and the other shared JS, `server.py`, the session-host paths, both conftests) belong to no surface. A diff touching one of those, spanning two surfaces, carrying an unclassified path, or editing `.fleet.toml` / the classifier runs the whole suite. `tests/test_classify_e2e.py` pins each surface and fails when an e2e test that names a surface's modules is missing from its targets.
- It exits non-zero on the first failure and prints total wall time. **Runtimes and the wedged threshold live in `CLAUDE.md`'s pre-ship gate block and only there** — they are a property of the machine measuring them, not of the suite, so a number copied into a second place goes stale without anyone noticing. This section used to carry its own figures, and a reader following them killed a healthy run (#1008). The shape is stable and worth knowing: byte-compile and the non-e2e pytest suite are a small fraction of the total and the dual-projection browser slice is nearly all of it, so a routed narrow run finishes in a small fraction of a full one. Re-measure any baseline on your own box before trusting it; when a run looks wedged, read `webapp/verify-progress.log` for the stuck node rather than timing it out.
- **Never pipe the gate when its exit code matters (issue #1086).** A pipeline's status is the status of its *last* command, so `pwsh -File scripts/verify-before-ship.ps1 | tail -40` hands the caller `tail`'s 0 and the gate's own 1 vanishes — a failed run reported as `exited with code 0`, with nothing but a human reading scrollback to catch it (bash's `pipefail` is off by default, and the agents' Bash tool does not set it). Redirect and read the file instead:

  ```bash
  pwsh -File scripts/verify-before-ship.ps1 > gate.log 2>&1; echo "exit=$?"
  tail -40 gate.log
  ```

  Where a pipe is genuinely unavoidable, the gate gives the caller something a pipe cannot rewrite: its last line is always `GATE-RESULT: PASS` or `GATE-RESULT: FAIL`, emitted on **every path the script can still run code on** — the success path, the `Fail` path, and (via a script-scope `trap`) an unhandled terminating error. That last one was the quietest failure in the repo: `$ErrorActionPreference = "Stop"` sent the error to **stderr** and exited 1, so `gate | tail -40` showed a caller *zero lines and an exit status of 0* — an empty, apparently-successful run. The marker's *absence* is now a narrower third state: the run was killed, an outer timeout fired, or PowerShell could not parse the file — none of which is a pass. Assert on that line, or on `${PIPESTATUS[0]}` under bash. The exit code itself is unchanged and stays correct for the unpiped case, which is the normal one. `tests/test_verify_gate_result_marker.py` drives the two failure paths for real and pins that no exit path can skip the marker.
- **Full-tier runs on this machine serialize against each other (issue #685).** Two overlapping full-tier gates — the normal outcome of the fleet's own claim-or-worktree concurrency model (a primary session's gate and a worktree session's gate running at the same time) — each spinning up ~400×2 browser contexts is the ephemeral-port burst `fleet-config#498` traced to this suite (`Tcpip` event 4231, `TIME_WAIT` 206→979). The e2e leg (not byte-compile or the non-e2e pytest phase) now waits on a kernel-managed named mutex (`Global\AppLauncherFullE2EGate`, `System.Threading.Mutex` — auto-released if a holder crashes, no stale-lockfile cleanup needed) before booting its disposable webapp; a queued run logs `e2e gate: queued behind another full gate run...` so a stall is diagnosable, and it fails loud after a 30-minute bounded wait rather than hanging forever. `static`-tier (Chromium-smoke-only) runs are cheap enough not to need it.

Run it before declaring any change to `app/webapp/`, `src/launcher.py`, or `src/session_host*.py` done. The same autoboot path is available to a plain pytest run with `--e2e-autoboot` (or `LAUNCHER_E2E_AUTOBOOT=1`).

The same gate also runs on CI (`.github/workflows/e2e.yml`, `windows-2025`) on every push to `main` and on pull requests into `main` — so the gate runs without relying on remembering to. Since #1041 it runs the **clean-machine half only**: a fresh `.venv` installed from `requirements.txt` on a runner that has never seen this repo, byte-compile, and the non-e2e pytest suite against committed files alone. That is the half a local run structurally cannot do — it is how an uncommitted file, a dev-box-only dependency, or drift in an unpinned dependency gets caught. The browser suite is skipped there; the local `verify-before-ship.ps1` is the contract for it, and CI cannot cover for a lane that skips it.

The `launched_claude_pty_session` tests (real-Claude rendered-output assertions) check `claude` is on `PATH` and **skip** cleanly where it isn't; the rest of the terminal tests use the #534 lightweight stub child, which needs only Python. A failed CI run keeps `verify-progress.log` as a downloadable `gate-progress-log` artifact on the run page, so a red is diagnosable from the run page without a local repro. The autoboot webapp/session-host logs are no longer collected there — since #1041 that job boots neither.
