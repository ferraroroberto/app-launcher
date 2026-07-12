# Board tab — reference

The launcher's fifth surface (issue #164, shipped in four steps: **#300** read-only render, **#301** drill-down + reply + one-tap issue start, **#302** dispatch bar, **#399** split into five single-purpose columns). It is a **read-only fleet kanban** that answers one question — *"what needs me now, across everything"* — over five **computed** columns, each holding one kind of card. A card moves because reality changed; there is deliberately no drag-and-drop. It renders three independently-degrading live sources (the session-host's session list, fleet-config's sessions-state file, and today's job runs) plus a cached GitHub view (`gh`-fetched issues / PRs).

On the phone the five columns are a swipeable one-column-per-screen carousel with a count strip on top; desktop shows all five side by side. The **Your turn** count is the number that matters — its strip button highlights when nonzero.

## The five columns and their data sources

Column assembly is pure logic in `src/board.py::build_board()`. Each column holds one kind of card:

| Column | What populates it |
| --- | --- |
| **Backlog** | Open GitHub issues across every repo of the configured owner (`github_owner`, default `ferraroroberto`). |
| **Claude's turn** | Live session cards whose status is **not** `needs-you` — i.e. `working`, `unknown`, or `idle`. (An idle session is still Claude holding a workspace, so it is shown here — dimmed client-side, not hidden.) |
| **Your turn** | Session cards whose status **is** `needs-you`, and nothing else — a terminal-only column. |
| **Other** | Open PRs, then today's failed-or-stuck job runs — everything else that needs attention but isn't a terminal. |
| **Done** | Today's closed issues, since local midnight. |

**Backlog** cards render thin (repo · #N · title · age) and link out to GitHub. A Backlog card whose repo is present in the projects folder additionally carries **▶ Start / ⚡ YOLO** (see "One-tap issue start" below).

**Done is issues only** (#399): a merged PR that closed an issue is already reflected by that issue showing closed, so there is no PR/issue pairing step any more — `src/github_client.py::search_done_today()` is just the closed-issues search.

The GitHub queries (`src/github_client.py`) are `gh search issues --state open` (limit 100, sorted by updated) for Backlog, `gh search prs --state open` (limit 50) for Other, and `gh search issues --state closed --closed >= <today>` for Done, each with a 20 s per-call timeout.

## Data endpoints

All Board routes live in `app/webapp/routers/board.py`.

| Route | Auth | Purpose |
| --- | --- | --- |
| `GET /api/board` | bearer-token | The five columns — the **5 s poll target**. Cheap only: runs the live session list, one state-file read, and one jobs-runs walk concurrently, plus a pure in-memory read of the GitHub cache. **No `gh` subprocess ever runs on this path.** |
| `POST /api/board/github/refresh` | bearer-token | Runs the three `gh` searches (open issues, open PRs, closed issues) and replaces the cache. The **only** place `gh` is invoked. |
| `GET /api/board/sessions/{sid}/exchange` | Tailscale + passkey | The last user↔assistant exchange from a session's transcript (drill-down drawer). |
| `POST /api/board/dispatch` | Tailscale + passkey | Spawn a new session and type an `/issue-*` goal into it (dispatch bar). |
| `POST /api/board/issues/start` | Tailscale + passkey | One-tap `/issue-start` / `/issue-yolo <N>` on a Backlog card. |

The `GET /api/board` response is `{ generated_at, columns, github: {fetched_at, error}, sessions_state: {available, stale, updated_at}, rate_limits: {available, stale, updated_at, five_hour, seven_day} }`. Each session card carries its raw session fields plus `project`, `status`, and `age_seconds`.

## The session-state file join

Launcher-owned session **presence and agent identity** come from the session-host list. That is the authoritative process-liveness source: when a PTY/detached process leaves the host list, its live Board card leaves on the next 5 s poll, regardless of whether an agent shutdown hook fired. Semantic status comes from a **sessions-state file** written by [`fleet-config`](https://github.com/ferraroroberto/fleet-config)'s agent hooks/extensions (fleet-config#91) — path `sessions_state_file`, default `~/.claude/hooks/state/sessions-state.json`. The Board only ever reads it.

**The join is agent-aware and exact-id-first.** A state writer may carry `agent` plus the launcher's own id as `launcher_session_id`; that exact id + agent pair wins. Rows predating #455 have neither field and are Claude Code rows by definition. They retain the normalized-cwd fallback (forward-slash separators, lowercase, trailing slash stripped), but only for a live Claude session. A Claude row can therefore never classify Codex, Pi, Antigravity, or Copilot.

Join mechanics (`_claim_walk` / `_match_state_row`):

- Live sessions are walked **newest-first** by `started_at`.
- Each session first claims an unmatched state row with the same `launcher_session_id` and `agent`.
- Without exact identity, the session can claim only an agent-compatible row whose normalized `cwd` is **equal-or-under** the session's project dir. A row carrying a different launcher id is never allowed to fall back by cwd.
- Two legacy candidate rows in one directory → the **most recently updated** wins.
- Two live sessions in the same dir → the fresher one claims the row; the older renders `unknown`.
- A fresh unmatched row with **no** live session becomes a state-only external card only when its declared transcript file exists and was written within the last 15 minutes. Hook status is semantic evidence, not process-liveness evidence: a missing cloud/bridge transcript or a quiet `working`/`needs-you`/`idle` row is suppressed rather than trusted for the state file's 24 h retention window. The first suppression per state id leaves an info-level breadcrumb with the distinct reason (missing path, unavailable file, or quiet transcript) without repeating on every poll.

**States** (`_KNOWN_STATUSES`): `working`, `needs-you`, `idle`. Anything else — including a missing row — renders `unknown`.

Agent capability matrix:

| Agent | Presence | Semantic state in this release |
|---|---|---|
| Claude Code | Session host | Native `UserPromptSubmit`, `Stop`, `Notification`, and `SessionEnd` rows from fleet-config |
| Codex | Session host | `unknown`; native semantic adapter tracked in fleet-config#349 |
| Pi | Session host | `unknown`; native semantic adapter tracked in fleet-config#349 |
| Antigravity / Copilot | Session host | `unknown`; no adapter in this release |

`unknown` is an explicit capability limit, not silence interpreted as certainty. Native Codex/Pi status publication is independently shippable cross-agent work in [fleet-config#349](https://github.com/ferraroroberto/fleet-config/issues/349).

**Degradation is total and silent.** `read_sessions_state()` returns `{available, stale, updated_at, rows}` and never raises: an absent, unreadable, or corrupt file yields `available: False` with empty rows, and every session card falls back to `unknown` while the GitHub and jobs columns render regardless. `stale: True` when the newest row is older than `STATE_STALE_AFTER` (24 h) — i.e. the hooks have stopped writing.

## Shared session title, cross-tab (#396)

The state row also carries `name` / `name_source` — Claude Code's own live per-conversation title, copied in by fleet-config's `session_state` hook from `~/.claude/sessions/<pid>.json` (fleet-config#302). `merge_sessions()` copies those onto every card as `shared_name` / `shared_name_source` via the **same** exact-id/agent-aware `_claim_walk` described above, and `attach_shared_names()` runs the identical walk for `GET /api/claude-code/sessions` (the Coding tab's Running-sessions list) — so a live session resolves to the same state row, and therefore the same title, on both tabs. `name_source: "derived"` marks the generic `<project>-N` fallback (no real title assigned yet); anything else is a genuine title.

The frontend precedence lives in one place — `sessions.js`'s `sessionTitle()` — which `board.js` imports rather than re-deriving a title: a genuine `shared_name` wins outright, the OSC-parsed `live_title` is kept as a same-poll-cycle-faster supplement (it updates sub-second inside an open terminal, ahead of the next state-file poll), then `prompt_title`, then a *derived* `shared_name`, then the launch name. See [Naming sessions from the conversation](#interactive-terminal-from-the-phone) in the README for the full precedence history (#266, extended #396).

## The Claude usage badges (#326)

The strip above the columns can show two small dot+label badges — 5h and 7d Claude account usage % — sourced from a **rate-limits cache** a [`fleet-config`](https://github.com/ferraroroberto/fleet-config) statusline writer maintains ([fleet-config#259](https://github.com/ferraroroberto/fleet-config/issues/259)) — path `rate_limits_file`, default `~/.claude/hooks/state/rate-limits.json`. As of this writing that writer doesn't exist yet, so the badges render hidden until it lands; the Board only ever reads the file.

**Schema** the writer is expected to produce: `{ "five_hour": {"used_percentage": N, "resets_at": epoch}, "seven_day": {...}, "captured_at": iso8601 }`. Either window, or any sub-field within a present window, may be `null`/absent — `src/board.py::read_rate_limits()` treats each independently and never raises on a missing/corrupt file (`available: False`, both windows `None`).

**Freshness** is driven by `captured_at` against `RATE_LIMITS_STALE_AFTER` (10 minutes) — far shorter than the sessions-state file's 24 h, since a usage percentage is only useful near-real-time. A stale reading **dims** rather than disappears (`.board-usage-badge.stale`), unlike the sessions-state banner's all-or-nothing text.

**Color thresholds match the terminal statusline exactly** — `>=80%` danger, `>=60%` warn, else the "good" tint — so the Board badge and a terminal's own statusline never disagree about what counts as "getting close." The reset countdown (`"resets in Xh Ym"`) is computed client-side from each window's `resets_at` epoch on every `renderBoard()` call, so it stays live between the 5 s polls with no extra timer.

## The transcript-activity overlay (#305, #309)

The fleet-config hooks flip status only on a few events (`UserPromptSubmit` → working, `Stop` → needs-you, `Notification` → needs-you/idle). Two resume paths fire **no hook at all**: answering an AskUserQuestion / permission prompt resumes the turn without a prompt submission, and a prompt typed into an already-running session is queued and delivered mid-turn. In both cases a `needs-you` stamp sticks while the agent is visibly working — the bug #305 fixes. Ground truth is the transcript JSONL, which is appended continuously during a turn and quiet on stop.

**The rule** (`_transcript_overlay()`): only when the hook status is `needs-you` or `idle` (it never overrides `working`/`unknown`), if real transcript activity is newer than the row's stamp by more than `_RESUME_EPSILON` (10 s), override the status to `working` and re-anchor the card's age to that activity timestamp. When Claude genuinely stops, the Stop hook re-stamps the row **after** the final transcript write, so `needs-you` wins immediately — no delayed alert. The 10 s epsilon absorbs the Stop hook and the final message write landing a couple of seconds apart in either order.

**Two-stage probe (the #309 refinement):**

1. **Pre-filter — raw mtime `os.stat`.** If the transcript's mtime is not more than the epsilon past the stamp, nothing was written after it — return unchanged and skip the read. This is the cheap gate.
2. **Confirmation — bounded tail read** (`_last_activity()`, only when the mtime clears the epsilon). Reads the **last 8 KB** of the transcript, splits into lines, walks in reverse, and returns the timestamp of the newest line whose `type` is `"assistant"` or `"user"` **and** which carries a dict `message` payload.

**Why mtime alone is too blunt (the #309 bug):** Claude Code appends **non-message metadata lines** to the transcript *after* Stop stamps the row — `system`, `ai-title`, `mode`, `permission-mode`, `pr-link`, `file-history-snapshot`, and more. Some land seconds-to-minutes post-Stop (a `pr-link` when a PR is detected, a title refresh), pushing mtime past the epsilon with no real resume — which made a finished, waiting session wrongly overshoot to `working`. `_last_activity` ignores every one of those types (only `assistant`/`user` lines with a dict `message` count), and skips torn/unparseable lines. No conversation event in the tail → the hook status is kept.

Net cost per 5 s poll: one `os.stat` per session row, plus one ≤8 KB tail read **only** for rows in a waiting status whose mtime moved.

## The dispatch bar — injection-safe spawn-then-type (#302)

Pinned above the columns, the **dispatch bar** speaks a new goal into existence: a goal box, a repo `<select>` (the same live claude-code listing the Coding tab launches from), an **add / build / yolo** mode pill, an **opus** toggle (off = Sonnet, on = Opus — the same per-launch toggle as the Life OS tab), a 🎤, and a ➤. Endpoint: `POST /api/board/dispatch`, body `{repo, goal, mode, opus, rows, cols, desktop}`.

The modes map to `/issue-*` commands:

| Mode | Command |
| --- | --- |
| `add` | `/issue-add` |
| `build` | `/issue-add now` |
| `yolo` | `/issue-yolo` |

**Why spawn-then-type.** The free-text goal must never touch the unquoted `cmd /c {exe} {flags}` string the session-host spawns with — that would be cmd-metacharacter injection. So dispatch spawns the session with **only the shared Claude flags and no positional prompt**, then delivers the goal over the PTY input path. (Contrast the sibling `/api/board/issues/start`, which *can* use a positional prompt because `/issue-<mode> <N>` is built server-side from an int-validated number — injection-safe by construction.)

**The readiness wait** (`_await_dispatch_ready()`): poll the session-host every 0.25 s, up to a 15 s cap. Ready = the session reports `output_chars > 0` (the session-host's first-paint signal); then wait a 2 s settle so the agent's TUI has its input box up. A session-host predating #302 exposes no `output_chars` key — the wait degrades to a fixed 5 s legacy grace with a warning log rather than refusing. If the session ever reports **not alive** during startup, the request raises 504 — typing into a dead PTY is the one forbidden outcome.

**The type** is two separate PTY writes:

1. `\x1b[200~` + `/issue-<mode> <goal>` + `\x1b[201~` — bracketed-paste framing keeps the goal one atomic paste (no per-keystroke TUI interpretation) and routes it through the first-prompt title capture (#266).
2. `\r` — the submitting carriage return as its **own second write**, so the paste-end marker cannot swallow it (the #64/#166 CR-as-own-write rule).

**Failure kills, never strands.** Any error past the spawn triggers a `stop(kill)` on the half-spawned session before re-raising, so a readiness timeout cannot leave an orphan the user never asked for.

**PTY-only.** Dispatch always spawns a full-control (`pty`) session — a detached console has no input path, and handing free text to its command line is exactly the injection this design avoids.

The bar is static markup the 5 s poll never re-renders, so a re-render cannot wipe a goal being typed. After a send the goal **stays in the bar** for rapid multi-dispatch (✕ clears it), and the new card lands in *Claude's turn* on the next poll. Voice dictation — the compose bar's exact streamed-partials pipeline, extracted to a shared `voice.js` — mounts on the dispatch goal box (and on every drawer reply box), so "speak a goal" is one mic tap; the transcript always lands in the box for review, never straight into a dispatch.

## The drill-down drawer and its PTY-write path (#301)

Tapping a live session card opens an **inline drill-down drawer** on the card (`board.js::buildDrawer`). It shows the last user↔assistant exchange plus a reply box, and while any drawer is open the **5 s poll pauses** (`fetchBoard()` self-gates on `state.boardExpanded`) so a re-render can never wipe a half-typed reply.

**Reading the exchange.** `GET /api/board/sessions/{sid}/exchange` resolves the live session's state row via the same cwd-join, reads `transcript_path` off that row (the transcript UUID lives only in the hook row), and calls `board.last_exchange()`. That reads the **last 256 KB** of the transcript, walks in reverse, and pulls the newest assistant `text` reply (skipping `thinking` / `tool_use` blocks; joining sibling lines of the same `message.id`) and the nearest preceding plain-string `user` prompt (list-shaped user content is tool results and is skipped, as are harness wrappers like `<command-`, `<system-reminder`). Display caps: assistant 6000 chars, user 1500. No row or no transcript → `{available: False}`, never an error.

**Replying to the live PTY.** The reply box is offered only for `alive && kind === 'pty'` cards (a detached console or state-only card has no reachable stdin). The reply proxies through `POST /api/claude-code/sessions/{sid}/input`, which wraps the text in bracketed paste when it contains a newline, then — on submit — sends `\r` as a **separate** write (the #166 rule again), forwarding to the session-host's `/sessions/{sid}/input`. On success the drawer closes (`boardExpanded = null`, poll resumes) and the card flips to *Claude's turn* within a cycle — the prompt-submit hook plus the transcript overlay both catch it. A **⚡** in the drawer opens the full-control terminal.

**One-tap issue start.** A Backlog card whose repo is in the projects folder carries **▶ Start / ⚡ YOLO** → `POST /api/board/issues/start`, body `{repo, number, mode, rows, cols}`. Injection-safe by construction: `prompt = "/issue-<mode> <number>"` with mode allowlisted and number int-validated. It resolves the repo to a project dir (404 if not present locally) and spawns a streamed PTY session — the `/issue-*` skills inherit worktree isolation for free, claiming the repo and building in a sibling worktree when the primary checkout is busy. On a desktop client it opens the PC-mirror window; elsewhere it opens the in-page terminal.

**`?board=<sid>` deep-link** (`board.js::openBoardCard`): activates the Board tab, fetches the board, finds the column holding that `session_id`, opens its drawer, and scrolls the carousel to it — the landing page a `notify_on_idle` Slack ping links to. If the session is already gone, it toasts and leaves the board browsable rather than pausing the poll forever on a non-existent card.

## Refresh / cache contract

GitHub data is fetched **server-side via `gh`** into a lock-guarded module-level cache in `src/github_client.py`. `snapshot()` is the pure in-memory read the 5 s poll hits for free; `refresh(owner)` runs the three `gh` subprocesses and replaces the cache. On failure the previous data is kept and only `error` is set — a flaky `gh` degrades to a badge, not an empty board.

The cache is refreshed **only** by:

- the manual **↻** button, or
- **tab activation** when the cache is stale — never fetched, or `fetched_at` older than `GH_STALE_MS` (2 minutes).

It is **never** refreshed on the 5 s poll (which only reads the snapshot), and an **errored** cache is never auto-retried — that would hammer a broken `gh`, so ↻ stays manual. This mirrors the Coding tab's ⎇ git-status on-demand contract exactly.

## Security boundary

The Board splits along the launcher's usual line. The **read-only board** (`GET /api/board`, the GitHub refresh) is bearer-token gated and reachable over the Cloudflare tunnel — it exposes only issue / PR / session metadata. The **terminal-grade surfaces** — the drill-down exchange (transcript text), the drawer reply (`/input`), the dispatch bar, and one-tap issue start — are **Tailscale-only + passkey-gated**: refused over the public tunnel entirely, and on the tailnet they additionally require a valid WebAuthn terminal token, exactly like the live terminal. The client obtains the token via the shared `ensureTerminalToken()` path.

## Verification

The pre-ship gate (`pwsh -File scripts/verify-before-ship.ps1`) covers the Board via `tests/test_board.py` (column assembly, the cwd-join, the transcript overlay, the `gh` cache), `tests/test_board_dispatch.py` (the spawn-then-type contract), `tests/test_board_drilldown.py` (the exchange read + reply path), and `tests/e2e/test_board_tab.py` (the read-only kanban, drill-down + reply + one-tap start, and dispatch bar in a real browser). All `gh` and session-host calls are mocked at their client seams so the unit suite never shells out.
