# Pi as a Coding-tab agent on the Claude subscription (via the Agent SDK) — #273

**Status: implemented as an optional 5th Coding-tab agent; phone validation owed.** This started as a feasibility spike for the [Pi coding agent](https://pi.dev) (`@earendil-works/pi-coding-agent`, `pi`) and the findings below proved it out, so Pi is now wired into the registry/options/launch exactly like the other agents. It answers the gate question — *can Pi join the Coding tab as a drop-in terminal agent, driven by the Claude subscription with **no API credits**?* — **yes**, via the `claude-agent-sdk` provider. It is added **alongside** Copilot (not replacing it) pending the on-device checklist at the end.

**Headline result (2026-06-21, this machine):** **Yes, feasible — with one decisive routing caveat.** Pi 0.79.9 is a registry-shaped interactive terminal CLI that loads this repo's `AGENTS.md`/`CLAUDE.md`, quits on `/quit`, and resumes via `-r` — so it slots into the existing agent registry + session-host model the same way Codex/Antigravity/Copilot do. The Claude-subscription path **works, but only through the SDK extension**: pi's *native* `anthropic` provider bills metered API "extra usage" credits (verified: it fails `400 You're out of extra usage` on this account), whereas the [`claude-agent-sdk-pi`](https://github.com/prateekmedia/claude-agent-sdk-pi) extension routes through the Claude Agent SDK / Claude Code **subscription quota** and returns a clean completion with **no `ANTHROPIC_API_KEY` set**. For Claude on this machine the SDK extension isn't *a* no-credit path — it's the *only* working one.

## The question this de-risks

The Coding tab treats terminal agents as a registry-driven set (`src/agents.py`): one tap from the phone spawns the agent in a project dir inside the session-host's ConPTY (full-control) or a detached console. Issue #273 asks whether Pi belongs in that set, ideally replacing the Copilot button for a trial, **without** spending API credits and **without** touching the Apps/Jobs/Life OS/terminal-gate/Cloudflare/Tailscale flows. The user's steer narrowed it: drive Pi with the Claude **subscription** via the **Agent SDK** (the `prateekmedia/claude-agent-sdk-pi` extension), not API keys.

## What was verified on the bench (autonomous, this machine)

All of the following was checked directly on 2026-06-21 with `pi 0.79.9` and `claude-agent-sdk-pi@^1.0.22`.

### 1. Pi is a registry-shaped interactive CLI

- **On PATH:** `pi` resolves at `C:\Users\rober\AppData\Roaming\npm\pi` — so `shutil.which("pi")` (the launcher's `is_installed` check) succeeds.
- **Loads repo context like Claude Code:** `pi --help` documents `--no-context-files` as "Disable **AGENTS.md and CLAUDE.md** discovery and loading" — i.e. by default pi discovers and loads both, exactly the behaviour the Coding tab relies on for project-aware agents. This repo already ships an `AGENTS.md` pointer + `CLAUDE.md`.
- **Quit command is `/quit`.** Confirmed in the installed package (the changelog string notes `/exit` was *removed* in favour of `/quit`; `Ctrl+C`/`Ctrl+D` also exit). Matches the launcher's per-agent `quit_command` graceful-stop contract.
- **Resume = `-r` (native session picker).** `pi --help`: `--resume, -r  Select a session to resume` — a picker the agent renders itself over the PTY, which is exactly the launcher's Resume contract (issue #151; like Claude/Copilot `--resume`). `-c`/`--continue` reopens the most recent session (the Antigravity-style fallback). Sessions are saved automatically under `~/.pi/agent/sessions/`.
- **Model switching in-session:** `/model` or `Ctrl+L`; `Ctrl+P` cycles a scoped `--models` set; `Shift+Tab` cycles thinking level. So the "switch models in one session" requirement is a native pi feature, no launcher work needed.
- **Headless mode exists:** `-p`/`--print` (+ `--mode json|rpc`) — used below to prove the auth path without driving the TUI.

### 2. The no-API-credit path: SDK extension works, native provider bills

Install + provider lineup:

```
pi install npm:claude-agent-sdk-pi     # adds the extension; pi list shows it
pi --list-models claude-agent-sdk      # provider registers the full current lineup:
                                        #   claude-opus-4-8, claude-sonnet-4-6,
                                        #   claude-haiku-4-5, claude-fable-5, ...
```

The extension's model lineup is **current** (Opus 4.8 / Sonnet 4.6 / Haiku 4.5 / Fable 5), despite its README mentioning only 4.5-era ids.

The decisive comparison, both run headless with **no `ANTHROPIC_API_KEY` in the environment**:

| Provider | Command | Result |
| --- | --- | --- |
| `claude-agent-sdk` (extension) | `pi -p --provider claude-agent-sdk --model claude-agent-sdk/claude-haiku-4-5 "…"` | ✅ clean completion (subscription quota, no credits) |
| `anthropic` (native) | `pi -p --provider anthropic --model anthropic/claude-haiku-4-5 "…"` | ❌ `400 invalid_request_error: "You're out of extra usage. Add more at claude.ai/settings/usage"` |

Both providers were logged in via subscription OAuth (`~/.pi/agent/auth.json` holds `anthropic` + `openai-codex` OAuth tokens, not API keys). The difference is the *call path*: pi's native `anthropic` provider hits the metered **Messages API** (billed as API "extra usage", which is exhausted on this account), while the extension hands reasoning to the **Claude Agent SDK** — Claude Code under the hood — which draws on the **Claude Code subscription quota**. Pi still executes its own tools (read/bash/edit/write); the SDK only does reasoning, with Claude Code's tool execution denied and mapped back to pi's tools.

**Implication:** the no-credit Claude path is real and verified, but it is *specifically* the SDK extension. A naive "add pi to the registry" without forcing the SDK provider would launch pi on its current default (`anthropic`) and fail on this account.

### 3. Routing caveat: settings default did *not* reroute headless `-p`

Setting `~/.pi/agent/settings.json` `defaultProvider: "claude-agent-sdk"` (+ matching `defaultModel`) did **not** make a bare `pi -p "…"` use the SDK — it still hit the native `anthropic` billing path (`400 out of extra usage`). Only **explicit `--provider claude-agent-sdk --model claude-agent-sdk/<id>` flags** reliably routed to the subscription path in headless mode.

This may be a headless-vs-interactive quirk: `defaultProvider`/`defaultModel` in `settings.json` are most likely consumed by the *interactive* TUI startup, which `-p` bypasses. The launcher spawns pi **interactively** (`cmd /c pi …`), so the settings default *might* suffice there — but that is unverified and is the kind of thing that fails silently into paid credits. **Safe recommendation:** the launcher should pass explicit provider/model flags rather than trust the user's pi default. That slots cleanly into the existing per-agent flag-builder pattern (Codex/Copilot already have dedicated flag blocks in `app/webapp/routers/apps.py`).

### 4. Fullscreen / repaint: leans inline (like Claude), needs phone confirm

The launcher's `fullscreen` flag marks an **alternate-screen differential TUI** (Codex's ratatui) that must skip scrollback replay and force a clean repaint on reconnect (issue #128). Static inspection of the pi packages found **no alternate-screen-buffer escapes** (`?1049h`/`?1047`/`?47h`) in the main TUI — only the *external-editor* feature uses the alternate buffer. That leans toward pi rendering **inline like Claude Code** (`fullscreen=False`), not like Codex. This is a static signal, not a device measurement — the reconnect/repaint behaviour over the phone PTY is on the validation list below.

## How it's wired (as implemented)

Pi is added the same way as the other terminal agents — registry row + flag builder + options block + icon — and touches **none** of the Apps, Jobs, Life OS, terminal-gate, Cloudflare, or Tailscale flows:

1. **Registry row** — `src/agents.py` `AGENTS["pi"]`: `command="pi"`, `quit_command="/quit"`, `fullscreen=False` (pi's TUI is inline like Claude), `resume_token="-r"` (native picker).
2. **Forced SDK provider** — `build_pi_flags` (`src/webapp_config.py`) always emits `--provider claude-agent-sdk --model claude-agent-sdk/<pi_model>` so the launch can never fall back to the billing `anthropic` provider. The settings.json default did not reliably reroute, so the provider/model are explicit. Wired into the launch dispatch + resume path in `app/webapp/routers/apps.py`.
3. **Model config knob** — `pi_model` (`VALID_PI_MODELS`, default `claude-opus-4-8`), exposed in `/api/config` and the Coding **options** card's new "Pi" block (`index.html`, `state.js`, `claude-options.js`) — a model `<select>` over the `claude-agent-sdk` lineup, like Copilot but with no empty "Default" (pi always launches with an explicit model). Detached/Resume use the existing global toggles.
4. **Icon** — `app/webapp/static/icons/pi.svg` (the SPA loads `/static/icons/<agent-id>.svg`).
5. **Prereq (one-time, on the PC):** `pi install npm:claude-agent-sdk-pi`, logged into the Claude subscription, no `ANTHROPIC_API_KEY`. See README "Installing Pi".
6. **Copilot kept** — Pi is the 5th agent; flip to replacing Copilot (a one-line registry swap) only after the on-device checklist holds.

### The session-host must reload to see Pi

Adding an agent changes `src/agents.py`, which is imported by **both** the webapp (`:8445`) **and** the session-host (`:8446`). `tray.bat --restart` restarts only the webapp and deliberately **preserves `:8446`** (to keep open PTY sessions alive), so after a plain `--restart` the session-host still rejects `pi` with `unknown agent: pi` (`app/session_host/server.py`). Pi only becomes launchable after a **full restart that also cycles the session-host** — which ends every open Coding/PTY session. The pre-ship gate sidesteps this by spawning its own disposable session-host (issue #260).

## What still needs the phone (interactive validation — owed)

The bench proved auth + CLI shape; these need a real run through the launcher over the tunnel (ideally via a throwaway branch-only registry row + the SDK flag block, reverted before any docs-only PR):

- [ ] **Full-control PTY from the phone:** launch pi in a project dir, confirm the TUI renders and accepts input.
- [ ] **Detached console mode:** same via the detached path.
- [ ] **Model switch over PTY:** `/model` / `Ctrl+L` usable from the phone keyboard.
- [ ] **Resume over PTY:** `-r` renders pi's session picker correctly through the session-host.
- [ ] **Stop + kill:** graceful `/quit` stop, and the unified hard-kill (issue #253).
- [ ] **Reconnect/repaint:** confirm `fullscreen=False` (inline) is correct, or flip it if pi leaves stale frames on reconnect (issue #128 behaviour).
- [ ] **Confirm interactive provider routing:** verify the launched pi is actually on `claude-agent-sdk` (not silently on the billing `anthropic` provider) — the explicit-flag recipe above is the guaranteed answer.
- [ ] **Icon:** `pi.svg` present and rendering on the tile + running-session chip.

## Recommendation

**Adopt Pi as an optional 5th Coding-tab agent for a trial, driven by the `claude-agent-sdk` provider via explicit launch flags.** It is feasible, fits the existing registry/session-host architecture with no cross-tab blast radius, and — crucially — gives a *working, no-API-credit* Claude path on this account where pi's native provider does not. Do **not** replace Copilot until the phone validation checklist is green; the swap is trivial once it is. File a follow-up implementation issue covering the registry row, the SDK-provider flag block (+ model config knob), and the `pi.svg` icon; keep this spike docs-only.

Secondary note worth a line in any implementation issue: the same extension would let the `openai-codex` subscription drive pi too, and pi additionally supports custom providers via `~/.pi/agent/models.json` (OpenAI/Anthropic-compatible) pointed at the local LLM hub (`127.0.0.1:8000`) — both out of scope here but cheap future options.

## Related

- Issue #273 — this spike.
- Base reference: [`prateekmedia/claude-agent-sdk-pi`](https://github.com/prateekmedia/claude-agent-sdk-pi) (the pi extension that routes reasoning through the Claude Agent SDK on a Pro/Max subscription).
- Pi docs: <https://pi.dev/docs/latest/quickstart> (subscription login, model switching, session continue/resume).
- `src/agents.py` — the agent registry the implementation extends.
- `docs/voice-loop-spike.md` — companion de-risking spike (same doc shape).
