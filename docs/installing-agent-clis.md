# Installing the agent CLIs

The launcher drives each coding agent through its own CLI. Install the ones you use; the launcher only offers an agent whose command it can find. Back to [Install](../README.md#install).

## Installing the Codex CLI

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
> Launch defaults explicitly select **Luna**, **Terra**, or **Sol** and show
> only reasoning levels supported by that model. Each model remembers its
> effort; Luna defaults to **Extra High**. The **Permission** selector mirrors
> Claude's: *Auto mode* runs with no prompts but keeps the sandbox; *Skip
> permissions* is the all-bypass switch.
>
> Like `agy`, `codex` is resolved against the **effective** `PATH` (the inherited
> environment plus the machine and user registry values), so
> installing it enables the button on the next detection poll, no restart
> required.

## Installing the Antigravity CLI

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
> environment plus the machine and user registry values — so an
> install lands on the next detection poll with **no restart at all**. A tray
> restart is only needed when `src/agents.py` itself changes (a newly
> *registered* agent, not a newly *installed* one). A bare `tray.bat` re-run is a no-op when a
> tray is already alive — use `tray.bat --restart` to stop the running tray and
> its tree (webapp, session-host, cloudflared, **any full-control Coding
> sessions**) and start a fresh one. **Detached (☁️) sessions survive** the
> restart — they are deliberately orphaned out of the tray's process tree so
> the `taskkill /T` teardown can't reach them.

## Installing the GitHub Copilot CLI

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
> Like `agy`, `copilot` is resolved against the **effective** `PATH` (inherited
> plus both registry values), so installing it enables the
> button on the next detection poll without restarting anything. Only a change
> to `src/agents.py` itself needs a restart.

## Installing Pi

The Coding tab can also launch the **Pi coding agent** (`pi`), driven by your
**Claude subscription** through the Claude Agent SDK *or* your **ChatGPT plan**
through pi's `openai-codex` provider — **no API credits** either way. The
tab's Pi button stays disabled until `pi` is on `PATH`. Install the CLI and the
SDK provider extension (needs Node.js):

```powershell
npm install -g --ignore-scripts @earendil-works/pi-coding-agent
pi install npm:claude-agent-sdk-pi
```

Verify with `pi --version`, `pi --list-models claude-agent-sdk`, and `pi --list-models openai-codex`. Launch defaults offer Claude 5 **Opus/Sonnet/Fable** and GPT-5.6 **Luna/Terra/Sol**, plus Pi's full `off` through `max` thinking ladder.

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
> [`docs/pi-coding-agent.md`](pi-coding-agent.md).
>
> Two different things are needed for the Pi button to work, and only one of
> them costs a restart. **Installing** the CLI is free: `pi` is resolved
> against the effective `PATH`, so a fresh install lands on the
> next detection poll. **Registering** the agent in `src/agents.py` is not:
> the `:8446` session-host imports that module at *its* start, so upgrading
> the launcher to a build that adds a new agent needs a session-host restart
> (`scripts/restart-session-host.ps1 -Confirm`, which ends every live PTY)
> before the button works — otherwise the launch fails with
> `unknown agent: pi`.

## Installing the Grok Build CLI

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
> Settings › Launch defaults carries a **Grok Build** sheet:
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
> against the effective `PATH` and appears on the next detection
> poll — but **registering** the agent (`src/agents.py`, imported by the
> `:8446` session-host at its own start) does, so upgrading to the build that
> first added Grok needed a session-host restart before the button worked,
> otherwise the launch failed with `unknown agent: grok`.
