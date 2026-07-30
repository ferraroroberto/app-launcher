# Pi as a Coding-tab agent on the Claude subscription (via the Agent SDK) — #273

**Status: implemented and live as an optional 5th Coding-tab agent (#273); most of the on-device checklist below is now confirmed through real usage, not just the bench.** This started as a feasibility spike for the [Pi coding agent](https://pi.dev) (`@earendil-works/pi-coding-agent`, `pi`) and the findings below proved it out, so Pi is wired into the registry/options/launch exactly like the other agents — README documents its model/effort/trust controls as a shipped feature (`README.md:147-190`), and `src/agents.py` registers it alongside Claude/Codex/Antigravity/Copilot/Grok. It answers the gate question — *can Pi join the Coding tab as a drop-in terminal agent, driven by the Claude subscription with **no API credits**?* — **yes**, via the `claude-agent-sdk` provider. It shipped **alongside** Copilot (not replacing it); see the closing checklist for exactly which on-device items are confirmed and which remain open.

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

### 4. Fullscreen / repaint: the static "leans inline" read was wrong — phone use caught it (#291)

The launcher's `fullscreen` flag marks a TUI whose scrollback ring is **replay-unsafe** and must instead skip replay and force a clean repaint on reconnect (issue #128). Static inspection of the pi packages at spike time found **no alternate-screen-buffer escapes** (`?1049h`/`?1047`/`?47h`) in the main TUI — only the *external-editor* feature uses the alternate buffer — and that led this spike to register Pi with `fullscreen=False`, reasoning it renders **inline like Claude Code**.

That static signal was **incomplete, and the on-device validation this doc originally deferred caught it**: `fullscreen` governs replay-safety, not alternate-screen use, and those are different properties. Opening a Pi session in the phone's in-page (full-control) terminal flooded the screen with endless, never-settling scroll (#291). An empirical ConPTY capture of a real response showed Pi is a **differential in-place repainter** — during an active reply it wraps ~4.4 redraw ops per output line (cursor-up, clear-line, synchronized-output framing) to repaint its bottom chrome — even though it never touches the alternate screen. Replaying that raw byte ring into a fresh xterm on reconnect cannot reconstruct the final frame; it replays the whole redraw history instead. Detached mode was unaffected (a live console receives the deltas in real time and is never rebuilt from a replayed ring). Fixed by flipping `fullscreen=True` in `src/agents.py`, putting Pi on the same skip-replay + forced-repaint path as Codex/Antigravity/Copilot/Grok.

## How it's wired (as implemented)

Pi is added the same way as the other terminal agents — registry row + flag builder + options block + icon — and touches **none** of the Apps, Jobs, Life OS, terminal-gate, Cloudflare, or Tailscale flows:

1. **Registry row** — `src/agents.py` `AGENTS["pi"]`: `command="pi"`, `quit_command="/quit"`, `fullscreen=True`, `resume_token="-r"` (native picker), `native_name_flag="--name"`. `fullscreen` was flipped from an assumed `False` to a phone-validated `True` by #291 (see "Fullscreen / repaint" below) — pi is a differential in-place repainter during an active response, not a plain inline emitter, so it takes the skip-replay + forced-repaint path like Codex/Antigravity/Copilot/Grok.
2. **Explicit provider/model + thinking + trust** — `build_pi_flags` (`src/webapp_config.py`) emits `--provider <p> --model <p>/<id> --thinking <effort> <--approve|--no-approve>`, all explicit because pi's settings.json defaults don't reliably reroute a launch. The provider switches on the chosen model (see point 3) so a launch can never fall back to the billing `anthropic` provider. Wired into the launch dispatch + resume path in `app/webapp/routers/apps.py`.
3. **Segmented model / effort / trust controls (#288)** — exposed in `/api/config` and the Coding **options** card's "Pi" block (`index.html`, `state.js`, `claude-options.js`) as segmented `<button>` rows mirroring the Claude/Codex blocks:
   - **Model** — `pi_model` over `PI_MODEL_SPECS` (default `claude-opus-4-8`): a deliberately small three-option set spanning two subscription providers — **Opus** (`claude-agent-sdk/claude-opus-4-8`) and **Sonnet** (`claude-agent-sdk/claude-sonnet-4-6`) on the Claude-subscription SDK path, and **GPT** (`openai-codex/gpt-5.5`) on the ChatGPT-plan path (verified no-API-credit). GPT is the one cross-provider option, so `build_pi_flags` switches `--provider`/`--model` on it. `models_available` is `{value,label}` so the buttons read "Opus/Sonnet/GPT".
   - **Effort** — `pi_effort` (`VALID_PI_EFFORTS` low/medium/high, default high) → `--thinking`, mirroring Claude's Effort.
   - **Project trust** — `pi_trust_mode` (`VALID_PI_TRUST_MODES` trust/ask, default trust) → `--approve`/`--no-approve`. This is project *trust* (whether pi loads project-local `.pi/` settings/extensions/skills), **not** a tool-permission gate: pi has no tool sandbox or per-action prompt (see pi's `security.md`). Default "trust" so an interactive phone launch never stalls on a startup trust prompt.

   Detached/Resume use the existing global toggles.
4. **Native spawn-time title (#503, superseded #555, landed via "name Board sessions in native pickers")** — Pi is one of the agents (with Claude Code and Copilot) whose documented `--name` flag receives a known Board issue title at spawn, syncing pi's own `-r` session picker without touching the live TUI (`README.md:317`). This is distinct from the launcher-side rename, which never forwards into any agent's CLI (issue #555 removed that path as unfixably racy).
5. **Icon** — `app/webapp/static/icons/pi.svg` (the SPA loads `/static/icons/<agent-id>.svg`) — confirmed present.
6. **Prereq (one-time, on the PC):** `pi install npm:claude-agent-sdk-pi`, logged into the Claude subscription (for Opus/Sonnet) and pi's `openai-codex` OAuth (for GPT), no `ANTHROPIC_API_KEY`. Pi's native `anthropic` OAuth is left **disconnected** (removed from `~/.pi/agent/auth.json`) so a launch can never slip onto the metered billing path — the SDK (Claude) and `openai-codex` (GPT) subscription paths are the only ones that remain. See README "Installing Pi".
7. **Copilot kept** — Pi shipped as the 5th agent, not a Copilot replacement; that swap (a one-line registry change) was never revisited and isn't blocked on anything left in this doc.

### The session-host must reload to see Pi

Adding an agent changes `src/agents.py`, which is imported by **both** the webapp (`:8445`) **and** the session-host (`:8446`). `tray.bat --restart` restarts only the webapp and deliberately **preserves `:8446`** (to keep open PTY sessions alive), so after a plain `--restart` the session-host still rejects `pi` with `unknown agent: pi` (`app/session_host/server.py`). Pi only becomes launchable after a **full restart that also cycles the session-host** — which ends every open Coding/PTY session. The pre-ship gate sidesteps this by spawning its own disposable session-host (issue #260).

## On-device validation status

The bench proved auth + CLI shape. Pi has since been live in the Coding tab for real phone use, and the items below are the same checklist this doc originally deferred, updated against what has actually surfaced since — ticked items name the evidence; unticked ones are still genuinely unconfirmed (no issue or observation has exercised them), not assumed fine by omission.

- [x] **Full-control PTY from the phone:** confirmed — #291 was found *by* running pi in the phone's in-page full-control terminal (that's how the scroll-flood was reproduced), and the TUI renders and accepts input correctly post-fix.
- [x] **Detached console mode:** confirmed fine — #291 states directly: "Detached mode is fine — when the session opens a real console window on the PC, Pi renders correctly."
- [ ] **Model switch over PTY:** `/model` / `Ctrl+L` usable from the phone keyboard — still unvalidated, no issue or observation on record.
- [ ] **Resume over PTY:** `-r` renders pi's session picker correctly through the session-host — still unvalidated.
- [x] **Stop + kill:** the unified graceful-then-force stop (issue #253) is a single generic code path keyed off each agent's `quit_command`/`fullscreen` registry fields, applied uniformly to every agent including Pi — not a Pi-specific mechanism, and no Pi-specific gap has surfaced.
- [x] **Reconnect/repaint:** resolved by #291 — `fullscreen=True` (see "Fullscreen / repaint" above); this doc's own wiring section previously described the pre-fix `fullscreen=False` and has been corrected in this pass.
- [ ] **Confirm interactive provider routing:** only the *headless* `-p --provider claude-agent-sdk` recipe (bench section above) is proven; whether an interactive phone launch actually lands on the SDK provider rather than silently falling back to the billing `anthropic` provider is still unconfirmed.
- [x] **Icon:** `pi.svg` present at `app/webapp/static/icons/pi.svg` and loaded by the SPA's `/static/icons/<agent-id>.svg` convention.

Three items remain genuinely open: model switch over PTY, resume over PTY, and interactive provider-routing confirmation. None have blocked Pi's adoption as a daily-use 5th agent, but none should be marked done without an actual observation the way #291 supplied one for reconnect/repaint.

## Recommendation

**Pi shipped as an optional 5th Coding-tab agent, driven by the `claude-agent-sdk` provider via explicit launch flags** — feasible, fits the existing registry/session-host architecture with no cross-tab blast radius, and gives a *working, no-API-credit* Claude path on this account where pi's native provider does not. It is documented in README as a live agent alongside Claude/Codex/Antigravity/Copilot/Grok. Copilot has **not** been replaced; that swap (a one-line registry change) is still available but was never revisited, and isn't gated on anything in this doc. The three open checklist items above are worth closing opportunistically the next time Pi is in daily use, rather than as a blocking follow-up.

Secondary note worth a line in any implementation issue: the same extension would let the `openai-codex` subscription drive pi too, and pi additionally supports custom providers via `~/.pi/agent/models.json` (OpenAI/Anthropic-compatible) pointed at the local LLM hub (`127.0.0.1:8000`) — both out of scope here but cheap future options.

## Related

- Issue #273 — this spike.
- Issue #291 — the phone-validated reconnect/repaint bug and fix (`fullscreen=True`).
- Issue #253 — the unified graceful-then-force stop, applied uniformly across all registered agents including Pi.
- Base reference: [`prateekmedia/claude-agent-sdk-pi`](https://github.com/prateekmedia/claude-agent-sdk-pi) (the pi extension that routes reasoning through the Claude Agent SDK on a Pro/Max subscription).
- Pi docs: <https://pi.dev/docs/latest/quickstart> (subscription login, model switching, session continue/resume).
- `src/agents.py` — the agent registry the implementation extends.
- `docs/voice-loop-spike.md` — companion de-risking spike (same doc shape).
