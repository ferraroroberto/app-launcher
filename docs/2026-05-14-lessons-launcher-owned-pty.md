# Lessons from building the phone terminal

**Date:** 2026-05-14
**Issue:** #1 — *Rearchitect Claude Code sessions around a launcher-owned PTY*
**Companion:** [`2026-05-14-launcher-owned-pty.md`](2026-05-14-launcher-owned-pty.md) (the what; this is the why)

A didactic retrospective — the reasoning, the dead ends, and the
practices that held up. Read this before extending the terminal feature.

---

## 1. What we set out to do

Replace the fire-and-forget "detached CMD window the launcher can't see"
model with **launcher-owned ConPTY sessions**, and put a **full
interactive terminal on the phone** on top — live output, scrollback,
typing, `Ctrl+C`, `/quit`, image paste. Non-negotiable: it had to be at
least as safe as the existing cloud surface, ideally safer.

## 2. The architecture, and why each piece exists

- **A separate `session-host` process owns the PTYs.** The obvious design
  is "the webapp owns the ConPTYs." It's wrong: every *Restart webapp*
  would kill every running Claude session. Pulling the PTYs into their
  own long-lived process (loopback-only, port `8446`, owned by the tray
  like `cloudflared`) means the streaming layer can restart without
  touching the work. **Lesson: separate the thing that restarts often
  from the thing that must not die.**
- **The webapp is the single auth choke point.** WebSockets *bypass HTTP
  middleware* — the bearer-token middleware never runs for a `ws://`
  upgrade. So every WS route re-applies the full gate (Tailscale check,
  bearer, passkey token) by hand. **Lesson: if you add an auth
  middleware, audit every protocol that skips it.**
- **The session-host fans output to N subscribers.** Making it
  multi-subscriber from day one (ring buffer + per-client queues) is what
  later made the PC mirror window a 10-line change instead of a rewrite.

## 3. Security: the model and the reasoning

The user's own framing was the key insight: *"the risk is the same risk
I already have — anyone on my tailnet could already RDP in."* That's
true, and it's why **Tailscale-only** is the foundation, not an
afterthought. But "no worse than today" isn't the bar for a surface that
runs `--dangerously-skip-permissions` — so we layered:

1. **Tailscale-only** — reject the `Cf-Ray` / `Cf-Connecting-IP` headers
   (public Cloudflare tunnel) and require the client IP in the
   `100.64.0.0/10` CGNAT range, loopback, or an explicit allowlist.
2. **Bearer token** — same as the rest of the app.
3. **WebAuthn platform passkey (Face ID)** — an enrolled-device
   whitelist. A passkey assertion mints a short-lived (12 h) terminal
   token; the WS and image endpoints demand it.
4. **Audit log** — every session start/stop, WS open/close, input, image,
   plus a full per-session transcript.

**Lesson: defense in depth means each layer assumes the others failed.**
The passkey gate is worthless if the attacker is already on the tailnet
*and* has the bearer token — but it's not *meant* to stop that; it's
meant to stop the case where they have the token but not your phone.

**The deliberate exception:** loopback clients (the PC itself) skip the
passkey — the iPhone's passkey isn't on the PC anyway, and loopback
already implies you're at the machine. Scoped, documented, intentional.
**Lesson: a bypass is fine if it's narrower than the thing it bypasses
and you wrote down why.**

## 4. Gotchas, and what each one taught us

### A correct security boundary that *looked* like a bug
The phone showed a bare "Disconnected." The terminal was Tailscale-only
and the phone was on the **public Cloudflare tunnel** — the gate worked
*perfectly*. But the WS closed *before* `accept()`, so the browser only
saw close code `1006` with no reason. **Two lessons:**
- **Accept the WebSocket first, then close with a code + reason.**
  Closing before the handshake completes gives the client nothing to
  display.
- **A correct rejection still needs a good error.** We added
  `/api/status` → `terminal.reachable/reason` and a pre-flight check so
  the UI explains *"open me over the Tailscale URL"* instead of failing
  mute. Security that the user experiences as a random bug erodes trust
  in the security.

### "It works when I run it, but not from the tray"
The tray's Tailscale-URL lookup failed only when launched by the tray.
Root cause chain: (1) the venv `pythonw.exe` is a **redirector stub** —
every launch is one idle stub + one real process; (2) the real cause was
the `tailscale` CLI **not being on `PATH`** (it lives in
`C:\Program Files\Tailscale\`); (3) a 4 s subprocess timeout was too
short under tray-startup load. **Lessons: never assume a CLI is on
`PATH`** (probe Program Files), **give startup-path subprocesses generous
timeouts**, and **when a process behaves differently by launcher,
suspect the environment, not the code.** We also made it write a debug
log and surface the failure reason in the notification — diagnosing this
blind cost hours.

### One PTY has exactly one size
When the phone and the PC mirror both attach to the same ConPTY, they
**cannot** each have their own layout — a pseudo-console has a single
`rows × cols`. The last client to call `resize()` wins, so they fight.
There is no clever fix; you **pick an authority**. We made the phone the
sole size authority (the WS proxy tags each client `role=pc|phone`; the
session-host honours `resize` only from `phone`) and the PC window
*mirrors* whatever size the phone set. **Lesson: when a resource is
fundamentally single-valued, don't simulate sharing — assign ownership.**

### Detached ≠ untracked
The original "remote" launch orphaned a CMD window the launcher kept no
handle to. Re-adding it as a *mode*, we kept the handle: spawn in a
`CREATE_NEW_CONSOLE` so it stays visible and outlives the host, but hold
the `Popen` so it can be **listed and killed**. **Lesson: "detached" and
"untracked" are different choices — you can have the window's
independence and still keep a kill switch.**

### Self-signed certs and loopback
The PC mirror window (an Edge/Chrome `--app` window over
`https://127.0.0.1`) tripped the cert error: the cert's SAN is the
`.ts.net` hostname, not `127.0.0.1`, and the CA isn't trusted on the PC.
Fix: `--ignore-certificate-errors --test-type`, **safe specifically
because that window only ever points at our own loopback origin**.
**Lesson: "ignore cert errors" is a scoped tool, not a sin — the
question is always *which* origin.**

## 5. Practices that held up

- **Plan mode first, sharp questions before code.** The two
  `AskUserQuestion` rounds (terminal reachability scope, device-binding
  method, sizing authority) each prevented a rewrite. One good question
  beats a day of rework.
- **Phased execution with verification gates.** Backend (5 files) →
  `py_compile` + import smoke test → frontend (4 files) → `node --check`
  + boot check. Each phase provably green before the next.
- **Cheap gates, every time.** `py_compile`, `node --check`, an
  import-and-`create_app()` smoke test, `curl /healthz`. None take more
  than seconds; together they catch most "it doesn't even start" bugs.
- **Cache-busting discipline.** Every static change bumps `?v=N` on the
  CSS and JS. Mobile Safari caches aggressively; "the user is testing
  stale code" is a costly confusion (it bit us once already this repo).
- **The changelog doc lives next to the code.** `docs/` captures *done*
  work; updating it in the same change as the code keeps it honest.
- **Diagnose from the audit log.** The "Disconnected" bug was solved by
  reading the client IP out of the audit log — `188.x` = public tunnel.
  The logging we built for security paid for itself as debugging.

## 6. If you extend this

- **Per-client rendering** would need the session-host to broadcast
  size-change frames so a mirror can letterbox/scale instead of just
  matching. We chose not to — "phone drives, PC mirrors" was enough.
- **Remote (detached) sessions don't survive in the list across a
  session-host restart** — the `Popen` handle is lost even though the
  window keeps running. Re-discovery by process scan would fix it; it
  wasn't worth the complexity.
- **The terminal token TTL is 12 h.** If you shorten it, add a quiet
  re-auth path so the user isn't bounced mid-session.
- **Edit mode** gates per-row rename/remove behind a Settings toggle to
  keep the lists icon-free. If you add more per-row actions, put them
  there too — resist icon inflation.
