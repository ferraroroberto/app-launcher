# Pi as a Coding-tab agent (Claude subscription via the Agent SDK)

**Pi** (`@earendil-works/pi-coding-agent`, `pi`) is one of the Coding tab's
registered terminal agents, alongside Claude Code, Codex CLI, Antigravity
CLI, GitHub Copilot CLI and Grok Build. It is a registry-shaped interactive
CLI that loads this repo's `AGENTS.md`/`CLAUDE.md`, quits on `/quit`, and
resumes via `-r` (its own native session picker) — the same agent-registry +
session-host model Codex/Antigravity/Copilot use. Install steps: [Installing
Pi](installing-agent-clis.md#installing-pi).

## Provider routing: why the SDK extension, not the native provider

Pi ships two ways to reach Claude models, and only one avoids spending paid
API credits on this account:

| Provider | Call path | Billing |
| --- | --- | --- |
| `claude-agent-sdk` (the [`claude-agent-sdk-pi`](https://github.com/prateekmedia/claude-agent-sdk-pi) extension) | Claude Agent SDK / Claude Code | Claude Code **subscription quota** — no credits |
| `anthropic` (pi's native provider) | metered Messages API | billed as API "extra usage" |

Pi still executes its own tools (read/bash/edit/write) either way; the SDK
path only hands reasoning to Claude Code, with Claude Code's own tool
execution denied and mapped back to pi's tools. Because pi's
`defaultProvider`/`defaultModel` settings do not reliably reroute a launch
(they are consumed by the interactive TUI startup, not every launch path),
the launcher never trusts a user-side pi default — every launch passes
explicit `--provider`/`--model` flags so it can never fall back to the
billing `anthropic` provider. The GPT option (Luna/Terra/Sol) routes the same
way through pi's `openai-codex` provider on the ChatGPT-plan subscription.

## How it's wired

Pi touches **none** of the Apps, Jobs, Life OS, terminal-gate, Cloudflare, or
Tailscale flows — it is added the same way as the other terminal agents:
registry row + flag builder + options block + icon.

1. **Registry row** — `src/agents.py` `AGENTS["pi"]`: `command="pi"`,
   `quit_command="/quit"`, `fullscreen=True`, `resume_token="-r"` (native
   picker), `native_name_flag="--name"`, `per_launch_model=False`.
   `fullscreen=True` because Pi is a **differential in-place repainter**
   during an active reply (it wraps redraw ops — cursor-up, clear-line,
   synchronized-output framing — around its bottom chrome on every output
   line, even though it never touches the alternate screen buffer), so its
   scrollback ring is replay-unsafe: it takes the skip-replay +
   forced-repaint path like Codex/Antigravity/Copilot/Grok instead of a plain
   inline replay.
2. **Explicit provider/model + thinking + trust** — `build_pi_flags`
   (`src/launching/flags.py`) emits `--provider <p> --model <p>/<id>
   --thinking <effort> <--approve|--no-approve>`, all explicit for the
   routing reason above. The provider switches on the chosen model (point 3),
   so a launch can never land on the billing `anthropic` provider. Wired into
   the launch dispatch + resume path in `app/webapp/routers/apps.py`. Pi *is*
   launched with a `--model`, but that model comes from the persisted
   `pi_model` setting and no caller can vary it per launch — which is why its
   registry row carries `per_launch_model=False`, and why `POST
   /api/apps/{id}/launch` answers 400 to a `pi:<model>` in the request body
   instead of accepting it and dropping it.
3. **Model / effort / trust controls** — exposed in `/api/config` and the
   Coding options card:
   - **Model** — `pi_model` over the canonical `src/launching/model_catalog.py`
     registry (default `claude-opus-5`): Opus/Sonnet/Fable 5 use
     `claude-agent-sdk`; Luna/Terra/Sol use `openai-codex`. Astra remains
     visibly unavailable until Pi advertises its exact id. Every launch
     passes the provider and model explicitly.
   - **Effort** — per model, on the same `effective_effort` resolver as
     Claude and Codex: the model's override in `pi_model_efforts` ({model:
     level}, overrides only, default `{}`), else its catalog
     `default_effort`, else `high`. Every Pi model's catalog default is
     `high` (nothing is published for Pi's providers; Pi pins Opus 5, whose
     documented default is `high`). The level is one of Pi's
     `off|minimal|low|medium|high|xhigh|max` ladder and is sent as
     `--thinking`, on fresh launches and on resume. In Settings the Effort
     row follows the Model combo above it and offers "Model default (High)",
     which clears the override. A legacy global `pi_effort` value is read
     once on load: `high` is dropped, any other value is carried to every Pi
     model, so an existing config launches exactly as before; the next
     settings save rewrites the file without it.
   - **Project trust** — `pi_trust_mode` (`VALID_PI_TRUST_MODES` trust/ask,
     default trust) → `--approve`/`--no-approve`. This is project *trust*
     (whether pi loads project-local `.pi/` settings/extensions/skills),
     **not** a tool-permission gate: pi has no tool sandbox or per-action
     prompt (see pi's `security.md`). Default "trust" so an interactive phone
     launch never stalls on a startup trust prompt.

   Detached/Resume use the existing global toggles.
4. **Native spawn-time title** — Pi is one of the agents (with Claude Code
   and Copilot) whose documented `--name` flag receives a known Board issue
   title at spawn, syncing pi's own `-r` session picker without touching the
   live TUI. This is distinct from the launcher-side rename, which never
   forwards into any agent's CLI (forwarding it was unfixably racy).
5. **Icon** — the `b-pi` symbol in the brand sprite of
   `app/webapp/static/index.html` (the SPA references `#b-<agent-id>`).
6. **Prereq (one-time, on the PC):** `pi install npm:claude-agent-sdk-pi`,
   logged into the Claude subscription (for Opus/Sonnet) and pi's
   `openai-codex` OAuth (for GPT), no `ANTHROPIC_API_KEY`. Pi's native
   `anthropic` OAuth is left **disconnected** (removed from
   `~/.pi/agent/auth.json`) so a launch can never slip onto the metered
   billing path — the SDK (Claude) and `openai-codex` (GPT) subscription
   paths are the only ones that remain.

### The session-host must reload to see Pi

Adding an agent changes `src/agents.py`, which is imported by **both** the
webapp (`:8445`) **and** the session-host (`:8446`). `tray.bat --restart`
restarts only the webapp and deliberately **preserves `:8446`** (to keep open
PTY sessions alive), so after a plain `--restart` the session-host still
rejects `pi` with `unknown agent: pi` (`app/session_host/server.py`). Pi only
becomes launchable after a **full restart that also cycles the
session-host** — which ends every open Coding/PTY session. The pre-ship gate
sidesteps this by spawning its own disposable session-host.

## What is still unverified

Full-control PTY from the phone, detached console mode, stop/kill and
reconnect/repaint are confirmed through real use — the unified stop path and
`fullscreen=True` replay handling apply uniformly across every registered
agent, Pi included. Three items have not been exercised yet, by observation
or issue, so they stay open rather than assumed fine by omission:

- **Model switch over PTY** — `/model` / `Ctrl+L` usable from the phone
  keyboard.
- **Resume over PTY** — `-r` rendering pi's session picker correctly through
  the session-host.
- **Interactive provider routing** — that an interactive phone launch lands
  on the `claude-agent-sdk` provider rather than silently falling back to the
  billing `anthropic` provider (only the headless `-p --provider
  claude-agent-sdk` path is proven).

## Related

- Base reference: [`prateekmedia/claude-agent-sdk-pi`](https://github.com/prateekmedia/claude-agent-sdk-pi)
  (the pi extension that routes reasoning through the Claude Agent SDK on a
  Pro/Max subscription).
- Pi docs: <https://pi.dev/docs/latest/quickstart> (subscription login,
  model switching, session continue/resume).
- `src/agents.py` — the agent registry this extends.
- [Installing Pi](installing-agent-clis.md#installing-pi) — CLI + extension
  install steps.
