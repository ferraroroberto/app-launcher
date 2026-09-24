# The launcher-owned PTY terminal

The architecture, security model, and hard-won gotchas behind the launcher-owned ConPTY terminal — a full interactive terminal on the phone, and why each piece is shaped the way it is. Read this before extending the terminal feature.

## 1. What we set out to do

Replace the fire-and-forget "detached CMD window the launcher can't see" model with **launcher-owned ConPTY sessions**, and put a **full interactive terminal on the phone** on top — live output, scrollback, typing, `Ctrl+C`, `/quit`, image paste. Non-negotiable: it had to be at least as safe as the existing cloud surface, ideally safer.

## 2. The architecture, and why each piece exists

- **A separate `session-host` process owns the PTYs.** The obvious design is "the webapp owns the ConPTYs." It's wrong: every *Restart webapp* would kill every running Claude session. Pulling the PTYs into their own long-lived process (loopback-only, port `8446`, owned by the tray like `cloudflared`) means the streaming layer can restart without touching the work. **Lesson: separate the thing that restarts often from the thing that must not die.**
- **The webapp is the single auth choke point.** WebSockets *bypass HTTP middleware* — the bearer-token middleware never runs for a `ws://` upgrade. So every WS route re-applies the full gate (Tailscale check, bearer, passkey token) by hand. **Lesson: if you add an auth middleware, audit every protocol that skips it.**
- **The session-host fans output to N subscribers.** Making it multi-subscriber from day one (ring buffer + per-client queues) is what later made the PC mirror window a 10-line change instead of a rewrite.

## 3. Security: the model and the reasoning

The user's own framing was the key insight: *"the risk is the same risk I already have — anyone on my tailnet could already RDP in."* That's true, and it's why **Tailscale-only** is the foundation, not an afterthought. But "no worse than today" isn't the bar for a surface that runs `--dangerously-skip-permissions` — so we layered:

1. **Tailscale-only** — reject the `Cf-Ray` / `Cf-Connecting-IP` headers (public Cloudflare tunnel) and require the client IP in the `100.64.0.0/10` CGNAT range, loopback, or an explicit allowlist.
2. **Bearer token** — same as the rest of the app.
3. **WebAuthn platform passkey (Face ID)** — an enrolled-device whitelist. A passkey assertion mints a short-lived (12 h) terminal token; the WS and image endpoints demand it.
4. **Audit log** — every session start/stop, WS open/close, input, image, plus a full per-session transcript.

**Lesson: defense in depth means each layer assumes the others failed.** The passkey gate is worthless if the attacker is already on the tailnet *and* has the bearer token — but it's not *meant* to stop that; it's meant to stop the case where they have the token but not your phone.

**The deliberate exception:** loopback clients (the PC itself) skip the passkey — the iPhone's passkey isn't on the PC anyway, and loopback already implies you're at the machine. Scoped, documented, intentional. **Lesson: a bypass is fine if it's narrower than the thing it bypasses and you wrote down why.**

## 4. Gotchas, and what each one taught us

### A correct security boundary that *looked* like a bug

The phone showed a bare "Disconnected." The terminal was Tailscale-only and the phone was on the **public Cloudflare tunnel** — the gate worked *perfectly*. But the WS closed *before* `accept()`, so the browser only saw close code `1006` with no reason. **Two lessons:**

- **Accept the WebSocket first, then close with a code + reason.** Closing before the handshake completes gives the client nothing to display.
- **A correct rejection still needs a good error.** We added `/api/status` → `terminal.reachable/reason` and a pre-flight check so the UI explains *"open me over the Tailscale URL"* instead of failing mute. Security that the user experiences as a random bug erodes trust in the security.

### "It works when I run it, but not from the tray"

The tray's Tailscale-URL lookup failed only when launched by the tray. Root cause chain: (1) the venv `pythonw.exe` is a **redirector stub** — every launch is one idle stub + one real process; (2) the real cause was the `tailscale` CLI **not being on `PATH`** (it lives in `C:\Program Files\Tailscale\`); (3) a 4 s subprocess timeout was too short under tray-startup load. **Lessons: never assume a CLI is on `PATH`** (probe Program Files), **give startup-path subprocesses generous timeouts**, and **when a process behaves differently by launcher, suspect the environment, not the code.** We also made it write a debug log and surface the failure reason in the notification — diagnosing this blind cost hours.

### One PTY has exactly one size

When the phone and the PC mirror both attach to the same ConPTY, they **cannot** each have their own layout — a pseudo-console has a single `rows × cols`. The last client to call `resize()` wins, so they fight. There is no clever fix; you **pick an authority**. We made the phone the sole size authority (the WS proxy tags each client `role=pc|phone`; the session-host honours `resize` only from `phone`) and the PC window *mirrors* whatever size the phone set. **Lesson: when a resource is fundamentally single-valued, don't simulate sharing — assign ownership.**

### A repaint is a repaint someone keeps — and most of them are not resizes

Long Claude messages showed up two or three times in the phone's scrollback (issue #930). The duplication is in the raw PTY stream, not the renderer: Claude Code redraws its live region by stepping the cursor *up* over it, and when the frame it wants to draw is as tall as the viewport, cursor-up cannot reach the frame's top — so it falls back to a **full-viewport repaint** (`ESC[H`, then `N x ESC[2K ESC[1B`, then `ESC[H`). `ESC[2K` erases only the viewport, so the copy already in scrollback survives and the repaint *adds* one; as the tall repainted frame scrolls, that copy is committed to scrollback a second time.

The first round (#942) read this as a resize problem, and fixed a real one: the iOS keyboard sweep forwarded every intermediate row count (`44 -> 20 -> 6 -> 20 -> 44`, including 1- and 6-row samples that are never a real viewport). The phone now keeps xterm's local `fit()` immediate but settles the frame that reaches the PTY (~300 ms), floors it (`PTY_MIN_ROWS`/`PTY_MIN_COLS`, clamped again server-side), and `PtySession.resize()` is a no-op for an unchanged size. That holds — a keyboard cycle costs two SIGWINCHes, none below the floor.

It did not stop the duplication, because **most repaints are not resize-driven**. `scripts/probe_repaints.py --resize-log` prints the gap directly: in sampled sessions the repaint count runs an order of magnitude above the logged resizes, and one session repainted 29 times with zero resizes. The trigger is the live region's *height*, and the two things that drive it are the agent's own content (a long in-flight tool call, which keeps re-rendering for the spinner and elapsed timer) and how narrowly it wraps. Measured with the same workload through the real launch path: **0 repaints at 40x120, 1 at 40x51, 2 at 13x51** — and across archived sessions the rate runs ~12 repaints/MB at 40 rows, ~18 at 37, ~46 at 13 and ~131 at 8. The rate tracks roughly `1/rows`, which is why the phone (narrow, and shorter still with the keyboard up) sees what a desktop terminal rarely does.

**No filter can drop the copy itself.** The repaint is legitimate output and a faithful terminal must render it. The copy to drop would be the one *already in scrollback*, and xterm.js exposes no way to delete particular scrollback lines (`buffer` is read-only). Rewriting the byte stream instead would mean second-guessing a live TUI from a partial model of its screen, and #930's 2026-09-22 measurement found no byte-level filter that removed anything without breaking the screen.

**What ships is the blunt lever: clear scrollback at the repaint** (#930, decided 2026-09-24). `app/webapp/static/repaint-scrollback.js` watches an inline agent's output where it enters xterm (`terminal-connection.js`). Right after a preamble that erases every viewport row, it writes `ESC[3J`. At that point the viewport is already blank, so no byte is held back and the screen and cursor come out identical. The cost is the terminal's history from before that repaint, which is gone after every long turn. That's acceptable because reading the conversation back is Chat mode's job (#953/#982): it rebuilds the exchange from the transcript, keeps all of it, and measured fast on a 56 MB synthetic session (open ~135 ms desktop / ~424 ms iPhone projection, Load older ~85 / ~282 ms, medians). A partial erase is left alone, and full-screen agents (VT snapshot, #432) are not touched. The resize-driven repaints #942 settled are cleared the same way. One kind of duplicate remains: the relative cursor-up rewrite of a live region taller than the screen has no preamble to see, so it still duplicates (upstream, anthropics/claude-code#92678). **Lesson: when the duplicate is upstream and faithful, don't teach the terminal to lie. Choose what to give up, and give it up on purpose.**

### Detached ≠ untracked

The original "remote" launch orphaned a CMD window the launcher kept no handle to. Re-adding it as a *mode*, we kept a kill switch *without* holding a child handle: the console is spawned through a transient PowerShell `Start-Process -PassThru` that **re-parents it out of the session-host's process tree** — so a `tray.bat --restart`, which tears the tray subtree down with `taskkill /T`, can't cascade into it (issue #130) — and the launcher keeps **only the PID** that `-PassThru` printed. That PID is enough to **list and kill** it (`taskkill /PID … /T /F`); liveness is a bare PID probe, not a `Popen`. **Lesson: "detached" and "untracked" are different choices — you can have the window's independence (it even survives a launcher restart) and still keep a kill switch.**

### Self-signed certs and loopback

The PC mirror window (an Edge/Chrome `--app` window over `https://127.0.0.1`) tripped the cert error: the cert's SAN is the `.ts.net` hostname, not `127.0.0.1`, and the CA isn't trusted on the PC. Fix: `--ignore-certificate-errors --test-type`, **safe specifically because that window only ever points at our own loopback origin**. **Lesson: "ignore cert errors" is a scoped tool, not a sin — the question is always *which* origin.**

## 5. Practices that held up

- **Plan mode first, sharp questions before code.** The two `AskUserQuestion` rounds (terminal reachability scope, device-binding method, sizing authority) each prevented a rewrite. One good question beats a day of rework.
- **Phased execution with verification gates.** Backend (5 files) → `py_compile` + import smoke test → frontend (4 files) → `node --check` + boot check. Each phase provably green before the next.
- **Cheap gates, every time.** `py_compile`, `node --check`, an import-and-`create_app()` smoke test, `curl /healthz`. None take more than seconds; together they catch most "it doesn't even start" bugs.
- **Cache-busting is automatic.** Static asset URLs carry a content hash (`?v=<hash>`) stamped at serve time, so any edit busts the phone's cache with no manual step — see `docs/cache-hygiene-asset-hashing.md` (and do **not** reintroduce hand-bumped `?v=N` query strings). Mobile Safari caches aggressively, so "the user is testing stale code" used to be a costly confusion; the content hash removes it.
- **The reference doc lives next to the code.** `docs/` captures durable design rationale; updating it in the same change as the code keeps it honest.
- **Diagnose from the audit log.** The "Disconnected" bug was solved by reading the client IP out of the audit log — `188.x` = public tunnel. The logging we built for security paid for itself as debugging.

## 6. If you extend this

- **Per-client rendering** would need the session-host to broadcast size-change frames so a mirror can letterbox/scale instead of just matching. We chose not to — "phone drives, PC mirrors" was enough.
- **Detached sessions now survive a `tray.bat --restart`** (issue #130). The console is re-parented out of the tray's process tree and the session-host (`:8446`) is excluded from the restart's reclaim sweep, so the fresh tray re-adopts the still-running session-host and its in-memory list — the detached window keeps running and stays listed (`alive` is a PID probe). The gap left: a genuine *session-host process* restart still drops them from the list — the orphaned console keeps running but its PID is no longer tracked. Re-discovery by process scan would close that gap; it wasn't worth the complexity.
- **The terminal token TTL is 12 h.** If you shorten it, add a quiet re-auth path so the user isn't bounced mid-session.
- **Edit mode** gates per-row rename/remove behind the Jobs tab's Registered-jobs toggle to keep the lists icon-free. If you add more per-row actions, put them there too — resist icon inflation.
