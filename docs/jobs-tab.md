# Jobs tab — reference

The launcher's third surface (issue #47) — a remote-fireable definition + trigger + history layer for one-shot Python scripts and scheduled jobs. Each job is defined once, then any trigger (phone tap, Stream Deck button, schedule) funnels through one executor and produces a uniform run record.

## Why a third surface

The Apps tab launches long-running services (Streamlit, FastAPI siblings, tunnels). The Coding tab launches coding agents in project folders. Both have completely different lifecycles from one-shot scripts: the latter run, exit, and need a "did it work?" record. The Stream Deck can already fire them from the desk, but with no status feedback and no remote trigger. Jobs is the missing piece — same fire-and-forget contract, but reachable from the phone and tied into a uniform history.

## Architecture

```
   phone tap  --+
 Stream Deck  --+--> POST /api/jobs/<id>/run --+
   schedule   --+                              +--> launcher.py run-job <id>
 (Task Sched) ----------------------------------+         |
                                                          +-- resolve interpreter
                                                          +-- capture output + exit code
                                                          +-- write run record
                                                                     |
                                              webapp/jobs/<id>/<run>/  <-- history
```

The single executor is `app/cli/commands/run_job_cmd.py` (`launcher.py run-job <id>`). Task Scheduler calls it directly; the webapp's `POST /api/jobs/<id>/run` route pre-creates the run dir, returns the new `run_id` immediately, then spawns the executor detached so the request never blocks.

## Data model — `config/jobs.json`

Gitignored. Committed template at `config/jobs.sample.json`. Separate file from `apps.json` because the shape is materially different (schedule, run lifecycle).

```json
{
  "jobs": [
    {
      "id": "reporting-daily",
      "name": "Daily Reporting",
      "script_path": "E:\\automation\\content-management\\launch_reporting.bat",
      "args": "auto",
      "schedule": { "type": "daily", "at": "06:00" },
      "added_at": "2026-05-23T07:00:00"
    },
    {
      "id": "linkedin-scrape",
      "name": "LinkedIn Scrape",
      "script_path": "E:\\automation\\content-management\\engagement\\linkedin\\scrape_comments.py",
      "args": "",
      "schedule": { "type": "daily_times", "at": ["06:00", "12:00", "18:00"] },
      "added_at": "2026-05-23T07:00:00"
    }
  ]
}
```

### Job kinds (issue #70)

Dispatch goes through a small registry, `src/jobs_kinds/` — one module per kind, each contributing a `validate()` (save-time pre-flight) and a `build_argv()` (the actual invocation). `app/cli/commands/run_job_cmd.py::build_invocation` just resolves which kind a job is and delegates; there is no `if suffix == …` ladder anymore.

A job's `kind` field selects the module. **`kind` is optional** — when absent, the kind is inferred from `script_path`'s suffix (`.py` → `python`, `.bat` → `batch`), exactly the two cases that worked before this registry existed. Every pre-existing `jobs.json` row keeps dispatching identically with no migration. New kinds (`powershell`, `shell-wsl`, `inline-shell`, `http-check`) always require an explicit `kind` — suffix-inference never expands beyond `.py`/`.bat`.

| `kind` | `script_path` | `kind_config` | How it runs | cwd |
| --- | --- | --- | --- | --- |
| `python` (or omitted, `.py` suffix) | required, `.py` | — | `<venv>/python.exe <script> <args>`, `PYTHONPATH = <project root>` | project root (dir containing the resolved `.venv`) |
| `batch` (or omitted, `.bat` suffix) | required, `.bat` | — | `cmd.exe /c <script> <args>` | `script_path.parent` |
| `powershell` | required, `.ps1` | — | `powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File <script> <args>` (absolute pwsh-5.1 path) | `script_path.parent` |
| `shell-wsl` | required, `.sh` | — | `wsl bash <script> <args>` | `script_path.parent` |
| `inline-shell` | must be empty | `{script_body, ext}` | body written to `webapp/jobs/<id>/<run>/_inline<ext>`, then dispatched through whichever of the three shapes above matches `ext` (`.ps1`/`.bat`/`.sh`) | matches the delegated kind |
| `http-check` | must be empty | `{url, method?, expect_status?, timeout?}` | `python -m src.jobs_kinds.http_check_probe --url … --method … --expect-status … --timeout …` — a tiny built-in probe, no external script | project root |

`python`'s venv walk-up (`src.jobs_schtasks.resolve_venv_python`) starts from `script_path.parent` and falls back to `sys.executable` if no ancestor `.venv\Scripts\python.exe` is found — the PYTHONPATH bit fixes the "out-of-tree script imports project packages" gotcha (see global CLAUDE.md). `shell-wsl` is opt-in: pre-flight surfaces a warning (not a blocking error) when `wsl.exe` isn't resolvable on PATH.

`args` is split on whitespace and appended to every kind's argv tail except `http-check` (its probe takes fixed flags only). If you need an argument containing spaces, put it inside the script/wrapper rather than relying on shell quoting.

#### `inline-shell` — a body instead of a file

For a script small enough that a standalone file on disk is more ceremony than the job is worth. `kind_config.script_body` carries the literal contents; `kind_config.ext` (`.ps1` / `.bat` / `.sh`) picks which of the three native shapes runs it:

```json
{
  "id": "disk-cleanup-inline",
  "name": "Disk cleanup",
  "kind": "inline-shell",
  "kind_config": {
    "script_body": "Get-ChildItem -Path $env:TEMP -Recurse | Remove-Item -Force -ErrorAction SilentlyContinue\n",
    "ext": ".ps1"
  },
  "schedule": { "type": "weekly", "day": "SUN", "at": "03:00" }
}
```

At fire time the body is written to `webapp/jobs/<id>/<run_id>/_inline<ext>` — inside the run's own directory, so it's preserved alongside `run.json` / `output.log` for reproducibility (not a throwaway temp file elsewhere) — then dispatched exactly like a `powershell`/`batch`/`shell-wsl` job pointed at that file.

#### `http-check` — a synthetic kind, no script at all

Polls a URL and succeeds/fails on the response status — the "executor" is `src/jobs_kinds/http_check_probe.py`, a tiny script invoked via `python -m` (no user-authored script needed):

```json
{
  "id": "launcher-health-check",
  "name": "Launcher health check",
  "kind": "http-check",
  "kind_config": { "url": "https://127.0.0.1:8445/api/version", "expect_status": 200 },
  "schedule": { "type": "hourly", "every": 1 }
}
```

`method` defaults to `GET`, `expect_status` to `200`, `timeout` to `10` seconds. The probe's stdout (status/timing/body-tail) lands in `output.log` exactly like any other job's script output — it reuses the executor's normal `subprocess.Popen`/tee/resource-sampler/exit-code machinery, nothing kind-specific.

#### Adding a new kind

1. Add a module under `src/jobs_kinds/` implementing the `JobKind` protocol (`base.py`): `name`, `validate(job) -> List[Problem]`, `build_argv(job, tail, param_env, run_dir) -> (argv, cwd, env_overlay)`.
2. Register it in `src/jobs_kinds/__init__.py`'s `KINDS` dict.
3. If it needs settings, read them from `job.kind_config` (a free-form dict) — no change to the `Job` dataclass required.

That's the whole surface — the executor, pre-flight, and router never need to know a new kind exists.

### Schedule types

A deliberately bounded set — no raw cron expressions, no Quartz-style strings. Adding a new schedule shape is a code change, not a config change.

| `type` | Fields | Materialises as |
| --- | --- | --- |
| `none` | — | No Task Scheduler entry (manual / Stream Deck only) |
| `minutes` | `every: int (>0)` | one task with `/SC MINUTE /MO <every>` |
| `hourly` | `every: int (1..23)` | one task with `/SC HOURLY /MO <every>` |
| `daily` | `at: "HH:MM"` | one task with `/SC DAILY /ST <at>` |
| `daily_times` | `at: ["HH:MM", …]` | **N tasks**, one per HH:MM, suffixed `-1`, `-2`, … |
| `weekly` | `day: "MON"…"SUN"`, `at: "HH:MM"` | one task with `/SC WEEKLY /D <day> /ST <at>` |
| `once`   | `at: "YYYY-MM-DDTHH:MM"`          | one task with `/SC ONCE /SD <YYYY/MM/DD> /ST <HH:MM>` — self-cleaning, see below |

`daily_times` is the one schedule type that fans out into multiple Task Scheduler entries. It exists because "every 6 hours at 06:00 / 12:00 / 18:00 (skip midnight)" doesn't fit any single preset cleanly — `hourly /MO 6` would also fire at 00:00, and three separate jobs would clutter the Jobs tab. The fan-out is invisible to the user: one row in `jobs.json` → one row in the Jobs tab → three wake-ups per day under the hood.

### Visible console (issue #91)

A job can carry `"visible": true` (omitted / `false` is the default). It changes two things, both aimed at a job you want to *watch* run on the PC while still capturing output for remote run-history:

```json
{
  "id": "codebase-audit-fleet",
  "name": "Weekly Codebase Audit (fleet)",
  "script_path": "E:\\automation\\fleet-config\\audit-fleet.bat",
  "schedule": { "type": "weekly", "day": "THU", "at": "22:00" },
  "visible": true
}
```

- **Interpreter.** The scheduled task's `/TR` runs under `python.exe` (console subsystem) instead of the default silent `pythonw.exe`, so a console window appears when the task fires in the logged-on session. `src.jobs.task_run_command(job_id, visible=…)` picks the interpreter via `src.jobs._launcher_python(visible=…)`, which resolves the launcher venv's `python.exe`/`pythonw.exe` with a PATH fallback.
- **Output tee.** The executor spawns the child with `stdout=PIPE` and streams its combined output to **both** `output.log` (the remote run-history record, unchanged) and the launcher's own console (`sys.stdout.buffer`, guarded). A pythonw / detached fire has no console, so the console half is never started while the file half always works. A non-visible job keeps the original direct-to-file spawn — no pipe, no reader, byte-for-byte unchanged.
- **The console half is best-effort and lossy; the file half is the record (issue #694).** The thread reading the child's pipe writes to `output.log` and nothing else — console chunks go onto a bounded queue (`_CONSOLE_QUEUE_MAX_CHUNKS`, ~256 KB) drained by a separate daemon thread, and a full queue **drops** the chunk. That is deliberate: a console that stops *draining* — a Windows console put into mark/select mode by a click is the classic trigger — blocks `write()` indefinitely with no exception and no timeout. When that write sat on the reader thread it stopped the child's pipe being drained, the pipe filled, and the whole process tree deadlocked (a real 5-hour `running` hang on 2026-07-30, `output.log` frozen mid-character at exactly 6 × 4096 bytes). At EOF the reader waits `_CONSOLE_DRAIN_TIMEOUT_SECONDS` for the console writer and then moves on regardless. Dropping is never silent — a run that drops emits a `console tee fell behind — dropped N chunk(s)` warning, from a throwaway thread because the root logger's `StreamHandler` writes to `stderr`, i.e. that same wedged console. So: watch the console for live progress, read `output.log` for what actually happened.

`visible` round-trips through `POST`/`PUT` like `confirm` and is omitted from the stored row when false. It only affects **scheduled** fires meaningfully — a webapp/Stream-Deck fire spawns the executor detached with no console regardless, so the tee's console half no-ops there (the log half still works).

### Elevated (admin) jobs (issue #350, #352)

A job can carry `"elevated": true` (omitted / `false` is the default) for a script whose target needs admin rights to do its work — e.g. restarting an app whose manifest requires elevation (`Start-Process`/`subprocess.Popen` from a non-elevated context fails silently: "the operation was canceled by the user", the signature of a blocked UAC prompt with nothing present to click "Yes").

```json
{
  "id": "hwinfo-restart",
  "name": "HWiNFO restart",
  "script_path": "E:\\automation\\automation\\system\\hwinfo_restart.py",
  "schedule": { "type": "hourly", "every": 8 },
  "elevated": true
}
```

**The launcher never *creates* a Task Scheduler entry for an elevated job.** `/RL HIGHEST` — Task Scheduler's own silent elevation, needed so the scheduled fire runs elevated with no interactive UAC prompt — can only be set by an already-elevated *calling* process (empirically verified: an admin account running non-elevated gets `ERROR: Access is denied` on the `/Create` call; being in the Administrators group is not enough, the token itself must be elevated). The launcher's webapp always runs non-elevated, so `sync_schtasks()` never issues a `/Create` the moment `job.elevated` is true, on `POST /api/jobs`, `PUT /api/jobs/<id>` (any field edit), `pause`, or `resume`. It still *deletes* any stale `\AppLauncher\<id>*` entry first (issue #409) — otherwise a job that used to be non-elevated leaves its old un-elevated scheduled task behind, still firing on its old schedule indefinitely. An elevated job's real Task Scheduler entry is treated as **externally-managed**: everything else (registry row, run history, stats, the computed `next_run` sortable field) keeps working normally, but the entry itself must be registered and updated by hand, from an elevated shell:

```
schtasks /Create /F /TN "\AppLauncher\hwinfo-restart" /TR '"E:\automation\app-launcher\.venv\Scripts\pythonw.exe" "E:\automation\app-launcher\launcher.py" run-job hwinfo-restart' /SC HOURLY /MO 8 /RL HIGHEST
```

(Single-quote the `/TR` value in PowerShell — double-quoted strings there don't pass embedded `"` through literally.) The Jobs tab marks an elevated job with a `🔒 external schedule` pill next to its schedule chip so it's visually obvious which jobs the app isn't managing. The row remains tappable for run history, and edit mode still offers the side-effect-free dry-run check. Run-now and pause/resume are omitted and their API endpoints return `409`: the non-elevated launcher cannot honor those actions safely against an externally managed `/RL HIGHEST` task. `elevated` round-trips through `POST`/`PUT` like `visible` and is omitted from the stored row when false. There's no dedicated UI checkbox yet (same as `visible`) — set it directly in `config/jobs.json` or via the API.

### Cooldown (issue #68)

A job can declare a per-job `cooldown_seconds`: a debounce window that prevents rapid manual fires (phone double-tap, Stream Deck button mash) from spawning overlapping runs of the same script.

```json
{
  "id": "reporting-daily",
  "name": "Daily Reporting",
  "script_path": "E:\\automation\\content-management\\launch_reporting.bat",
  "args": "auto",
  "schedule": { "type": "daily", "at": "06:00" },
  "cooldown_seconds": 120
}
```

- **Range.** `null` (or omitted) and `0` both mean "no cooldown". Otherwise the value must be an `int` in `[1, 86400]` — the upper bound (one day) catches obvious typos like millisecond values; bools are rejected explicitly because `bool` is a subclass of `int` in Python.
- **Anchor.** The window is measured from the **most recent non-skipped run's** `started_at`. Skipped records are deliberately ignored: anchoring on them would turn the fixed cooldown into a sliding debounce, where every rejected mash-fire pushed the next allowed fire further away.
- **Manual fires inside the window** (phone tap, Stream Deck, any `POST /api/jobs/<id>/run`) are rejected at the route with `HTTP 429`. Response:
  - `Retry-After: <seconds>` header (HTTP-standard, for unsophisticated clients).
  - JSON body: `{"detail": {"detail": "cooldown", "retry_after_seconds": <int>, "cooldown_seconds": <int>}}`.
  - No run dir is created — a rejected manual fire leaves zero on-disk footprint.
  - The UI surfaces a toast: *"⏭ Skipped — cooled down for N more s."*
- **Scheduled fires inside the window** (Task Scheduler firing the executor directly) cannot be intercepted at the route, so the executor itself performs the same admission check. It writes a `skipped` run record (no spawn, no `output.log`) and exits 0. The record carries `status="skipped"`, `note="cooldown"`, `cooldown_seconds`, `cooldown_remaining_seconds`, and `cooldown_anchor_run_id` for audit clarity. Skipped records do **not** contribute to p50/p95/success-rate stats and do **not** count toward the failure-streak notification gate.

### `once` schedule + pause/resume (issue #68)

#### `once`

A `once` schedule fires exactly one time at the named instant, then deletes itself. The `at` is ISO-style `YYYY-MM-DDTHH:MM` (no seconds, no timezone) — the format that `<input type="datetime-local">` emits, so the dialog round-trips without conversion.

```json
{ "schedule": { "type": "once", "at": "2026-06-01T14:30" } }
```

- **schtasks fan-out:** one task with `/SC ONCE /SD <YYYY/MM/DD> /ST <HH:MM>`. The slash date form is the locale-independent input schtasks accepts everywhere; the dashed / dotted forms are locale-dependent and silently no-op outside en-US.
- **Self-cleaning:** when a `once` job fires via Task Scheduler (`trigger="scheduled"`), the executor's finalisation removes the schtasks entry and flips the registry's `schedule` to `type: "none"` so the row stops advertising a past-tense "once" instant. **Manual fires of a `once` job leave the schedule intact** so a deferred future fire is still possible.

#### Pause / resume

Any schedule can be paused. Pause is a **state marker, not a new schedule shape** — the live `schedule` flips to `none` (so the schtasks resync layer deletes the entries) and the original is parked under `paused_schedule`. Resume restores it byte-for-byte.

```json
{
  "schedule":        { "type": "none" },
  "paused_schedule": { "type": "daily", "at": "06:00" }
}
```

- **Endpoints:** `POST /api/jobs/<id>/pause` and `POST /api/jobs/<id>/resume`. Pause on a manual-only job returns `400 cannot pause a job whose schedule is 'none'` (no parked payload would survive a load → save cycle anyway). Pause is idempotent: pausing an already-paused job is a no-op so accidentally pressing ⏸ twice doesn't lose the parked payload.
- **`schedule_chip`** for a paused job reads "paused — was <original chip>" so the user can see at a glance both that the schedule isn't ticking and what it will restore to.
- **UI:** a `⏸` button on every row whose live or parked schedule isn't `none`; pressing toggles pause/resume. The button's icon and label switch with the state.

### DAG chaining (issue #68)

A job can declare downstream consequences:

```json
{
  "id": "scrape",
  "name": "Scrape",
  "script_path": "...",
  "on_success": ["transform"],
  "on_failure": ["alert-failure"]
}
```

After the executor finalises a run, it loads the current registry (so a user edit between spawn and finalise takes effect on the next chain hop without a webapp restart), picks `on_success` if `status == "success"` or `on_failure` if `status == "failed"`, and dispatches each downstream via the same mutex-aware admission path the route uses. The downstream run carries `trigger="chain:<upstream_id>"` and `chained_from=<upstream_id>` on its `run.json`.

- **Cycle guard.** Both `add_job` and `update_job` run a DFS over the union of `on_success ∪ on_failure` and reject any change that introduces a cycle. The error message names the cycle path (e.g. `chain cycle detected: a → b → a`). Self-chains are rejected with a separate, clearer error.
- **Reference guard.** Every entry must point at an existing job id. A typo'd downstream is rejected at save time, so the dialog catches it instead of letting the chain silently no-op forever.
- **Cascade strip on delete.** Deleting a job strips its id from every other job's `on_success` / `on_failure` so the registry stays referentially clean (no dangling ids).
- **Interaction with mutex groups.** Chain fires go through `dispatch_chain_run`, which runs the same `mutex_collision` check as the route. A chained downstream that hits a busy mutex group lands in the queue with `status="queued"` and waits for drain like any other queued entry.
- **Interaction with cooldown.** Chain fires are intentionally exempt from cooldown — they're an explicit downstream consequence, not a user click. (Mirrors the executor's `scheduled`-only cooldown check from the other side.)

### Mutex groups (issue #68)

A job can declare a `mutex_group` — a free-form lowercase identifier (alnum + `_`/`-`, must start with a letter, ≤32 chars). Two jobs sharing a `mutex_group` are not allowed to have overlapping in-flight runs: when one is already `running` or `pending`, a fresh fire of any other member is **queued** rather than rejected, and the head run's finalisation pops the next queued entry and spawns it detached.

```json
{
  "id": "linkedin-scrape",
  "name": "LinkedIn Scrape",
  "script_path": "...",
  "mutex_group": "chrome-profile"
}
```

- **Admission points (issue #696).** Every fire is gated before it runs, on whichever side of the process boundary it arrives: manual / webhook / API fires at `_admit_and_spawn` (`app/webapp/routers/jobs_run.py`), chain fires at `dispatch_chain_run` (`src/jobs_queue.py`), and **schtasks fires in the executor itself** (`_finalize_mutex_queue`, `app/cli/commands/run_job_cmd.py`) — Task Scheduler launches `run-job` directly and never touches the webapp, so without that third gate the weekly fleet chain, the main reason the feature exists, ran unserialized. The executor gate is scoped to `trigger == "scheduled"` **with no `--run-id`**: every already-admitted path pre-creates its run dir and passes `--run-id`, so the discriminator is what stops a drained entry (which replays its original `trigger="scheduled"`) being pushed straight back onto the queue it was just released from. Mirrors the `scheduled`-only cooldown gate next to it, and runs *after* it — a cooled-down fire is a no-op and shouldn't occupy a queue slot.
- **Queue file.** `webapp/jobs/_queue.json` — one JSON document, keyed by group → FIFO list of `{job_id, run_id, trigger, params}`. Mutations go through `os.replace` so the file is always a complete document. Empty groups are pruned out so the file stays tidy.
- **`status: "queued"`.** Queued runs land in history with `status="queued"`, `mutex_group`, and `mutex_blocked_by` (the id of the job currently holding the group). Queued runs do **not** contribute to p50/p95/success-rate stats and do **not** count toward the failure-streak gate. The route returns `{run_id, job_id, status: "queued", mutex_group, mutex_blocked_by}` so the UI can render a queue toast.
- **Drain triggers.** The mutex queue drains in two places: (1) the executor's finalisation block, after the head's `run.json` flips to `success`/`failed`; (2) the kill endpoint, after a stuck head is signalled (otherwise killing the head would wedge the queue with no finaliser to drain it).
- **Double-spawn guard.** Just before spawning the drained head, `drain_mutex_queue` re-reads the head's `run.json` and refuses to spawn if its `status` is not `queued`. A concurrent finaliser racing us advances the queue forward (the head is popped) but does not double-fire the executor.
- **Cross-host coordination.** Single-host only. Multi-host coordination is explicitly out of scope (see the umbrella issue).

### Parameters (issue #67)

A job can declare typed inputs collected at run-time. With no `params`, a tap on ▶ fires immediately (today's behaviour). With one or more `params`, ▶ opens a small dialog so the user supplies values; the executor composes them into argv (and env) safely.

```json
{
  "id": "linkedin-scrape",
  "name": "LinkedIn Scrape",
  "script_path": "E:\\automation\\content-management\\engagement\\linkedin\\scrape_comments.py",
  "args": "",
  "schedule": { "type": "none" },
  "params": [
    { "name": "since", "kind": "date", "flag": "--since" },
    { "name": "tier", "kind": "enum",
      "options": ["smb", "mid", "enterprise"],
      "default": "smb", "flag": "--tier" },
    { "name": "verbose", "kind": "bool", "flag": "--verbose",
      "default": false }
  ]
}
```

Submitting the dialog with `since = 2026-06-01`, `tier = mid`, `verbose = true` runs:

```
python scrape_comments.py --since 2026-06-01 --tier mid --verbose
```

#### Param schema

| Field      | Type                                                   | Notes                                                                                 |
|------------|--------------------------------------------------------|---------------------------------------------------------------------------------------|
| `name`     | snake_case string, unique within the job               | identifier used to key user input and to label the dialog field                       |
| `kind`     | one of `string` \| `int` \| `enum` \| `bool` \| `date` | bounded; anything else fails validation                                               |
| `default`  | kind-typed, optional                                   | pre-fills the dialog; presence makes the param non-required unless `required: true`   |
| `required` | bool, optional                                         | defaults to `false` when `default` is set, else `true`                                |
| `options`  | non-empty list of strings, required iff `kind: enum`   | renders as a `<select>`                                                               |
| `flag`     | string (`--…`), optional                               | when set, emits `<flag> <value>` (or just `<flag>` for truthy bool); absent → positional |
| `env`      | UPPER_SNAKE_CASE string, optional                      | when set, value lands in the executor's env overlay instead of argv (mutually exclusive with `flag`) |

Bool params require either `flag` or `env` — they have no useful positional encoding.

#### Composition rules

- Params iterate in declaration order; positional + flag args interleave in that order, so the list controls argv layout.
- `kind: bool` with `flag` emits just `<flag>` when truthy and is omitted when falsy.
- `env`-mapped params contribute to the env overlay, never argv.
- The legacy free-form `args` field is composed **after** the param-driven argv as a whitespace-split tail. Existing jobs continue to work unchanged.

#### Re-run from history

A run record persists the typed payload as `params: {name: value}`. Each row in the runs list grows a small ↻ button that opens the run dialog **pre-filled** with that record's values. If the job's schema has changed since the run (a param was removed or renamed), the dialog drops the unknown keys and surfaces a yellow note before letting the user submit.

## Webhook-target jobs (issue #73)

Phone tap, Stream Deck, and schedule are all *pull* triggers — something the
user owns decides to fire the job. A webhook is the *push* direction: an
external service (GitHub, Stripe, IFTTT/Zapier/a custom script) POSTs
straight into `POST /api/jobs/<id>/hook` and the job fires, with the payload
turned into the same typed `params` a manual run would supply.

### Security boundary — no bearer, ever

`/hook` sits **outside** the bearer-token gate entirely (`app/webapp/middleware.py`'s
`_is_webhook_hook_path`) — an external service can't be handed the SPA's
all-powerful bearer, and baking it into a third-party integration's URL store
would be the same blast radius as leaking the token outright. Instead the
route trusts **only** the job's own provider-specific signature:

- **`github`** — `X-Hub-Signature-256: sha256=<hex>`, an HMAC-SHA256 of the
  raw request body, `hmac.compare_digest`-checked.
- **`stripe`** — `Stripe-Signature: t=<epoch>,v1=<hex>`, HMAC-SHA256 of
  `f"{t}.{body}"`; `t` must be within 300 s (Stripe's own SDK default) of
  now, rejecting a replayed-but-otherwise-valid signature.
- **`generic`** — `X-Webhook-Token: <secret>`, a plain constant-time
  compare. The catch-all for IFTTT, Zapier, or any custom script.

A request that fails signature verification (or names an unknown/missing
provider secret) gets a `401` and **writes no run record at all** — nothing
touches disk until the signature is verified. This means the `/hook` URL
itself is safe to hand to the external service in plaintext.

### Job shape

```json
{
  "id": "gh-push-deploy",
  "name": "Deploy on GitHub push",
  "script_path": "E:\\automation\\automation\\deploy\\deploy.py",
  "params": [
    { "name": "repo", "kind": "string", "flag": "--repo" },
    { "name": "branch", "kind": "string", "flag": "--branch" }
  ],
  "webhook": {
    "provider": "github",
    "secret": "$secret:gh_deploy",
    "mapping": {
      "repo": "$.repository.full_name",
      "branch": "$.ref"
    },
    "events": ["push"]
  }
}
```

| Field | Type | Notes |
| --- | --- | --- |
| `provider` | `"github"` \| `"stripe"` \| `"generic"` | Selects the verification scheme above |
| `secret` | string | A literal value, or `$secret:<key>` resolved against `webapp_config.json`'s `secrets` block at fire time (see below) |
| `mapping` | `{param_name: jsonpath}` | Keys **must match a name in this job's `params`** — resolution reuses the exact same `compose_argv` composition a manual run goes through, so there is no separate argv-building logic for a webhook fire |
| `events` | list of strings, GitHub only | `X-GitHub-Event` allowlist; empty (or omitted) accepts every event. A filtered-out event returns `204` with no run record |

### Payload → params mapping

`mapping` values are a small JSONPath-lite dot-path: `$.repository.full_name`,
with optional list indices (`$.commits[0].id`). A path that doesn't resolve
against a *particular* event's shape is silently omitted from the mapped
params — not fatal — so one job's mapping can list fields from several event
types without erroring on whichever one didn't fire. A mapping key that
doesn't match any declared `Param` name **does** fail (`400`, no run record)
— the same "unknown param" rejection a manual run's bad payload gets.

Two worked examples:

- **GitHub `push`** — `{"repo": "$.repository.full_name", "branch": "$.ref"}`
  against a standard push payload.
- **Stripe `payment_intent.succeeded`** — `{"intent_id": "$.data.object.id"}`
  pulls the PaymentIntent id out of the event envelope.

### Secrets — the shared `secrets` block

`config/webapp_config.json` carries a `secrets: {key: value}` dict (gitignored,
alongside the existing `pushover_*` secrets). `$secret:<key>` in a job's
`webhook.secret` — and in any `Job.env` value, see "Per-job secrets & env"
below — resolves against it at fire time (`src.jobs_secrets`). A literal string
works too — `jobs.json` is already gitignored — but `$secret:` keeps the
secret in one place instead of duplicated across every job that shares it, and
lets it rotate without touching `jobs.json`. This block shipped with issue #73
under the legacy key `webhook_secrets`, which still loads; issue #72
generalised it and renamed it on save.

### Run record

A verified webhook fire dispatches through the same admission path (cooldown
+ mutex-group check) as a manual run, then writes the run exactly like any
other: `trigger: "webhook"`, `trigger_source: "webhook:<provider>"`, and the
mapped `params`. The raw payload is additionally persisted to
`_webhook.json` in the run's own directory — provider, the `X-GitHub-Event` /
`content-type` headers (never the signature or secret), and the parsed JSON
body — so the run is fully reproducible. `GET /api/jobs/<id>/runs/<run_id>`
surfaces it as `webhook_payload`; the Jobs-tab UI renders it in a collapsed
"🪝 Webhook payload" `<details>` under the output pane for the selected run.

### UI

A job with `webhook` configured shows a `🪝 <provider>` chip next to its
schedule chip. The job editor's **Webhook** section has a provider picker
(none/GitHub/Stripe/generic), a secret field, a GitHub-only event-allowlist
field, and a mapping editor (param-name / JSONPath row pairs, `+ Add mapping`)
— structurally identical to the Parameters editor just above it. Switching
the provider back to "None" and saving clears the job's webhook.

## Task Scheduler — `\AppLauncher\` namespace

All Jobs-tab schtasks entries live under the `\AppLauncher\` Task Scheduler folder. The naming rule:

- Single-task schedules → `\AppLauncher\<job_id>`
- `daily_times` → `\AppLauncher\<job_id>-1`, `-2`, … (one per HH:MM)

Sync is idempotent: on every create/edit, the launcher deletes every existing `\AppLauncher\<job_id>*` task first, then re-creates from the current schedule. Edits never leave stale entries behind. Delete-via-API removes both the registry row and every matching schtasks entry.

The `/TR` (task run) command stored in Task Scheduler is quoted so paths containing spaces survive Task Scheduler's own tokenisation:

```
"E:\automation\app-launcher\.venv\Scripts\pythonw.exe" "E:\automation\app-launcher\launcher.py" run-job <job_id>
```

An `elevated: true` job (see "Elevated (admin) jobs") is never *created/recreated* by the launcher — its Task Scheduler entry (created by hand with `/RL HIGHEST`) is externally-managed. A stale non-elevated entry from a prior schedule is still deleted, though.

Scheduled runs use `pythonw.exe` (silent — no console window appears on schedule fire). The repo's own `.venv` is preferred; a missing `.venv` falls back to `pythonw.exe` on PATH. A job with `"visible": true` (see "Visible console") instead runs under `python.exe` so a window appears on fire.

To inspect what's actually scheduled:

```powershell
schtasks /Query /TN "\AppLauncher\reporting-daily" /FO LIST /V
schtasks /Query /FO CSV /NH | findstr "AppLauncher"
```

## Run history — `webapp/jobs/<job_id>/<run_id>/`

Every run produces a directory with two canonical files, an artifact directory,
and one extra file for a webhook-triggered run:

| File | Content |
| --- | --- |
| `run.json` | One run's full metadata (schema below) |
| `output.log` | Combined stdout+stderr, raw bytes |
| `artifacts/` | Files the child deliberately preserves through `JOB_ARTIFACT_DIR` |
| `_webhook.json` | The triggering webhook's payload + a safe header subset (webhook-triggered runs only — see "Webhook-target jobs" below) |

`run_id` is a sortable timestamp (`YYYYmmddTHHMMSS`); collisions within the same second append `-2`, `-3`, … The executor exports the absolute artifact-directory path as `JOB_ARTIFACT_DIR`; a script copies or writes any CSV, JSON report, image, or other result it wants preserved there. The run detail API lists immediate files as `{name, size, mtime}`, and the guarded download route resolves the requested filename before serving it. Parent traversal and nested paths return HTTP 400.

Unpinned history is pruned to the most recent **20 runs per job** by the executor at the end of each run. A run with `pinned: true` is excluded from that quota and survives until explicitly unpinned; its artifacts follow the run directory, so there is no separate artifact-retention policy.

`status` transitions: `pending` (webapp pre-create) → `running` (executor takes over) → `success` | `failed`. The runs list remains on the Jobs tab's lightweight 4 s refresh. Selecting a live run opens `/api/jobs/<id>/runs/<rid>/stream`: the server sends one current-tail snapshot, incremental output chunks, then a final status frame and closes. Finalized output is fetched once over JSON and never streamed.

### `run.json` schema

| Field | Type | Written by | Purpose |
| --- | --- | --- | --- |
| `run_id` | str | webapp + executor | Sortable timestamp; matches the dir name |
| `job_id` | str | both | FK to `config/jobs.json` |
| `name` | str | both | Job name at the time of the run (denormalised on purpose — survives renames) |
| `trigger` | `"manual"` \| `"scheduled"` \| `"webhook"` \| `"chain:<upstream_id>"` | both | Where the run was fired from |
| `trigger_source` | `"api"` \| `"schtasks"` \| `"webhook:<provider>"` | webapp / executor | Provenance (issues #73, #72): `"api"` on every `POST /run` fire, `"schtasks"` on a Task Scheduler fire, `"webhook:github"`-style on a webhook fire. Absent on pre-#72 records and direct CLI fires |
| `trigger_ip` | str | webapp | API fires only — `request.client.host` of the caller |
| `trigger_ua` | str | webapp | API fires only — the caller's `User-Agent` |
| `trigger_token_id` / `trigger_token_label` | str | webapp | API fires authenticated by a minted scoped token (see "Scoped API tokens") — the token's id + label, never the secret. Absent for the legacy `auth_token` and loopback callers |
| `script_path` | str | both | Resolved at spawn time |
| `args` | str | both | Whitespace-split into argv |
| `params` | object | webapp + executor | Typed-parameter payload (issue #67); only written when non-empty |
| `started_at` | ISO 8601 | both | `pending` write or `running` re-write |
| `status` | `"pending"` \| `"running"` \| `"success"` \| `"failed"` | both | Final value lands at executor exit |
| `finished_at` | ISO 8601 | executor | Only on final write |
| `exit_code` | int | executor | `-9` is reserved for `/kill` (`SIGKILL` analogue) |
| `pid` | int | executor | The child PID, persisted at spawn so the kill endpoint works even if the executor itself crashes between spawn and `wait()` |
| `pid_create_time` | float (epoch) | executor | Captured immediately after spawn (issue #591) — lets the reap check (below) tell "still this process" apart from a since-recycled pid; absent on pre-#591 records |
| `duration_seconds` | float | executor | Wall-clock seconds the child ran for; rounded to 3 d.p. |
| `peak_rss_bytes` | int | executor | Peak resident-set size summed across the process tree (parent + recursive children) — sampled at ~1 Hz |
| `cpu_seconds` | float | executor | Accumulated user + system CPU across the tree — sum of per-PID maxima |
| `killed` | bool | kill endpoint | `True` only when finalised via `/kill` |
| `reaped` | bool | reap check (issue #591) | `True` only when finalised automatically because the recorded pid was provably dead and the executor never finalised it — distinct from `killed`, which means a human tapped Kill |
| `watchdog` | bool | executor (issue #695) | `True` only when the executor's last-resort watchdog tore the run down — distinct again from `killed` (a human) and `reaped` (nobody; the executor had already died) |
| `watchdog_reason` | `"no_output"` \| `"max_runtime"` | executor (issue #695) | Which signal breached. Only written alongside `watchdog: true` |
| `note` | str | executor | Human one-liner for a non-obvious finalisation — `"cooldown"` on a skipped fire, an invocation error, or `"watchdog: no output for 68min"` |
| `pinned` | bool | run update endpoint | Keep-forever flag; omitted/false for normal retention |

Plain files remain the canonical store — same pattern as session transcripts and audit logs. A future LLM/human can `cat` a run record without any tooling. `webapp/jobs/_index.sqlite` is only a derived mirror: `runs` stores queryable metadata and `output_fts` maps FTS5 rowids to run rowids for cross-run grep. `src/jobs_index.py` rebuilds it from every `run.json` / `output.log` pair when the file is missing, corrupt, or carries an older `PRAGMA user_version`; deleting it and reloading the Jobs tab is the supported repair path. Canonical writes update the mirror after the atomic JSON swap, and finalization captures the completed output plus artifact presence.

## Authoring safety — pre-flight on save (issue #69)

Adding a job used to be a leap of faith: the first scheduled fire was when you found out the path was wrong or the venv didn't walk up. `src/jobs_preflight.py::preflight(job)` front-loads those checks at save time so the dialog can surface problems *before* the schedule starts ticking. It is a **pure function** (no subprocess, no globals) — both so it is trivially unit-testable and so a request handler never shells out to `schtasks.exe`.

Two severities:

- **error** — the job cannot run as configured; the save is **blocked** with HTTP 400.
- **warning** — the job will run, but probably not the way the author expects; it **saves once acknowledged**.

Checks performed:

1. **`script_path` exists** → *error* if the file is missing (the single most common authoring mistake).
2. **`.py` venv walk-up** → *warning* when no ancestor `.venv\Scripts\python.exe` is found; the executor will fall back to `sys.executable`. Mirrors the executor's own `resolve_venv_python` (now living in `src/jobs.py`) so the check matches runtime behaviour exactly.
3. **`.bat` embedded `.venv` reference** → *warning* when the wrapper names a `.venv` interpreter / `activate` path that doesn't resolve (best-effort text scan).

**Not a check** — `args` splitting: the executor uses plain `str.split()` (whitespace only, no shell quoting), so any non-empty string splits cleanly under that contract; there is nothing to validate at save time. Jobs that need arguments containing spaces should embed them in the `.bat` / `.py` wrapper instead — see the `build_invocation` docstring in `app/cli/commands/run_job_cmd.py`.

### Two-phase flow (errors block, warnings confirm)

`POST /api/jobs` and `PUT /api/jobs/<id>` both run pre-flight on the effective job:

- **Error present** → `400 {"detail": {"reason": "preflight", "problems": [...]}}`. Nothing is persisted.
- **Warnings only, not acknowledged** → `200 {"saved": false, "warnings": [...]}`. Nothing is persisted; the dialog stays open showing the warnings with a **Save anyway** button.
- **Warnings acknowledged** (`"acknowledge_warnings": true` in the body) **or no problems** → the row is saved and the response is `{"job": ..., "saved": true, "warnings": [...]}` (the warnings are echoed back so the UI can still note them).

Each `Problem` is `{level, field, message}` — `field` (currently always `script_path`) lets the dialog place the message next to the offending input.

**Deferred** (issue #69, not implemented): the schtasks `/TR` round-trip check and the schtasks id-collision query. Both would require shelling out to `schtasks.exe` from the request path; the `/TR` string carries only launcher-internal paths (never user input), so the value is low and the cost — forcing schtasks mocking into every create test — is high.

## Dry-run (issue #69)

Once a job is saved, dry-run lets you verify it without committing to a full-effect fire. `POST /api/jobs/<id>/run` accepts an optional `dry_run` field with two modes:

- **`"check"`** (mode 2 — the 🧪 row button, edit-mode only): resolves the full invocation (`script_path` exists, venv walk-up, param composition) **without spawning the child**. Writes a synthetic record with `status: dry_run_success` (or `dry_run_failed` carrying the resolution error in `note`) and no `exit_code`. This is the "would this even start?" check; it deliberately bypasses the executor funnel because nothing is ever spawned.
- **`"execute"`** (mode 1 — the **Dry-run** checkbox in the run-now dialog): spawns the child through the real executor but with `JOB_DRY_RUN=1` in its environment. Scripts that opt in (`if os.environ.get("JOB_DRY_RUN"): …`) suppress their side effects. The run record is stamped `dry_run: true` so history shows the distinction.

Both modes **bypass cooldown and the mutex queue** — a dry run is an explicit verification action, so pressing 🧪 should never be answered with "cooled down" or "queued". Dry-run records (`dry_run_success` / `dry_run_failed`, and any record stamped `dry_run`) are **excluded from the cooldown anchor** so a verification never resets a job's cooldown window. The history list marks dry runs with a `🧪 dry` chip.

## Confirm-on-fire (issue #69)

A job can carry an optional `confirm: true` flag (the **⚠️ Require confirmation before running** checkbox in the editor). When set, a manual fire must be explicit:

- `POST /api/jobs/<id>/run` returns `403 {"detail": "confirmation required"}` unless the request carries `?confirmed=1`. This keeps the gate honest against a direct curl or a stray Stream Deck press — a Stream Deck button targeting a `confirm` job has to bake `?confirmed=1` in deliberately.
- The UI's run-now path (`▶` and the run-now dialog's Run button) shows a confirm prompt and then sends `?confirmed=1`.
- A dry-run **`"check"`** is **exempt** (it has no side effects); a dry-run **`"execute"`** is **not** (it spawns the child), so it is gated like any other real fire.

The flag round-trips through `POST` / `PUT` and is omitted from the stored row when false (like the other optional fields).

## API surface

| Route | Auth | Purpose |
| --- | --- | --- |
| `GET /api/jobs` | bearer-token | List jobs, decorated with `schedule_chip`, `target_kind`, `next_run`, `next_run_epoch` / `next_run_iso` (computed), `last_run`, `running`, `run_count`, `pinned_count` |
| `GET /api/jobs/agenda?days=7` | bearer-token | Upcoming fires over the next `days` (1..14), grouped client-side: `{occurrences, frequent, days, generated_epoch}` (issue #230) |
| `POST /api/jobs` | bearer-token | Create — body `{name, script_path, args?, schedule?}` |
| `PUT /api/jobs/<id>` | bearer-token | Edit (re-syncs schtasks) |
| `DELETE /api/jobs/<id>` | bearer-token | Remove + delete schtasks entries |
| `POST /api/jobs/<id>/run` | bearer-token | Trigger now (returns `run_id`, spawns executor detached) |
| `POST /api/jobs/<id>/hook` | **provider signature only — never the bearer** | Trigger from an external service; see "Webhook-target jobs" below |
| `GET /api/jobs/<id>/runs` | bearer-token | Newest-first run history |
| `GET /api/jobs/runs/search?q=<text>&job=<id?>&status=<s?>&since=<iso?>` | bearer-token | FTS5 cross-run output search, newest first, with a concise snippet |
| `GET /api/jobs/<id>/runs/<run_id>` | bearer-token | One run's metadata + output tail (last 64 KB) + artifact list + `webhook_payload` |
| `PUT /api/jobs/<id>/runs/<run_id>` | bearer-token | Set `{pinned: true|false}` |
| `GET /api/jobs/<id>/runs/<run_id>/artifacts/<filename>` | bearer-token | Download one strictly path-jailed artifact |
| `WS /api/jobs/<id>/runs/<run_id>/stream` | bearer-token | Live-only snapshot → delta chunks → final status; token travels in the WS query string |
| `POST /api/jobs/<id>/runs/<run_id>/kill` | bearer-token | Terminate a stuck run's process tree, finalise `run.json` (`status: failed`, `exit_code: -9`, `killed: true`) |

## Operational signal (issue #66)

The row carries five lightweight signals on top of the schedule chip and last-run line, recomputed on every `/api/jobs` poll:

- **Duration chip** — `p50 4.2s · p95 11s` over completed runs of this job. Hidden when there are no completed runs yet.
- **Sparkline** — `●●●○●●●` over the last 7 runs, oldest-left. Green = success, red = failed, amber = running/pending, grey = unknown.
- **Success rate / 30 d** — appears in the meta line when there has been at least one completed run in the last 30 days (`72% / 30d`).
- **⚠️ stuck marker** — the latest run is in `running` status and has been running for more than `max(p95 × 3, 300 s)`. The marker is *surface only* — auto-kill is intentionally out of scope; a human still chooses to act.
- **⚠ not firing pill** — the schedule isn't producing runs at all: a missing/disabled Task Scheduler entry, or an elapsed slot with no run record. See [Missed-fire coverage](#missed-fire-coverage-issue-697); the pill renders only for a confirmed `problem`, never for `unknown`.
- **CPU / peak RSS** — surfaced on the selected run's output label inside the expanded panel (`Output · <rid> · success · 47 s CPU · peak 1.3 GB`).
- **Tap-to-copy log (issue #97)** — tapping the selected run's output pane copies the whole log to the clipboard (toast `📋 Copied log`), so an error trace is one tap away from pasting into a report / chat. A manual text selection inside the pane is left alone (auto-copy is suppressed while a selection exists), and the empty placeholder is a no-op.

## List order + countdown (issue #229)

As the registry grows, the question that matters at a glance is *"what fires next?"* — which name order can't answer (it interleaves cadences). So:

- **Computed next-fire timestamp.** `src.jobs.next_fire(schedule, *, now)` derives the next wall-clock fire purely from the bounded schedule shape — `daily_times` picks the earliest upcoming slot, `weekly` rolls to the next matching weekday, `once` returns its instant only if still future, and `none` (which includes a *paused* job, whose active schedule is parked as `none`) returns `None`. It is exposed as `next_run_epoch` (int seconds) + `next_run_iso` on `/api/jobs`. This is deliberately **separate from** the schtasks `next_run` string, which is a locale-formatted, lexically-sorted best-effort value — fine to display, useless to sort by.
- **Default Next-run order.** The client (`app/webapp/static/jobs.js` `sortedJobs`) sorts ascending by `next_run_epoch`; jobs with no next fire (manual-only / paused) sink to the bottom, tie-broken by name. A header toggle (`#jobsSortBtn`) flips to classic A–Z; the choice persists in `localStorage` (`launcher.jobsSort`, default `next`).
- **Countdown chip.** Each scheduled row shows a relative `⏱ in 3h` chip next to its cadence chip, recomputed in place on every poll. No chip for jobs with no next fire. Replaces the old `next: <schtasks string>` text in the meta line so "next" has exactly one home.

## Schedule agenda (issue #230)

A foldable **🗓️ Schedule** panel sits above Registered jobs (collapsed by default, same `<details>` chrome as #226). It answers *"what's planned over the next few days?"* without a desktop-style 2D calendar grid — the deliberate mobile-native substitute is a **day-grouped agenda list**.

- **Occurrence expansion.** `src.jobs.upcoming_fires(schedule, *, start, end, cap=200)` enumerates every fire in a window by walking `next_fire` forward (each call returns a fire strictly after the cursor), so it reuses the #229 logic rather than re-deriving cadence math. Dense `minutes` / `hourly` cadences (`FREQUENT_SCHEDULE_TYPES`) are **not** enumerated — they'd flood the window — and return `[]` here.
- **Endpoint.** `GET /api/jobs/agenda?days=7` (clamp 1..14) expands each non-paused job into `{occurrences, frequent, days, generated_epoch}`: `occurrences` is a flat, time-sorted list of `{job_id, name, fire_epoch, fire_iso, cadence}`; `frequent` summarises the minutes/hourly jobs as one `{job_id, name, cadence}` row each. No schtasks, no per-job decoration.
- **Panel.** `app/webapp/static/jobs.js` fetches lazily when the panel opens (re-fetched on each open; nothing polls it) and groups `occurrences` by calendar day under `Today` / `Tomorrow` / `Wed 18 Jun` headers, each row `HH:MM · name · cadence`. Frequent jobs render as a muted footer; an empty window shows "No scheduled runs in the next 7 days." Tapping a row calls `revealJob` — it expands that job in the Registered-jobs list below and scrolls it into view (the agenda is a read-only lens, not a second control surface).

### `run_stats` shape

`src/jobs.py::run_stats(job_id)` is the single helper feeding all of the above:

```python
{
  "p50": 4.2,                            # seconds, completed runs only
  "p95": 11.7,
  "success_rate_30d": 0.72,              # None when zero completed in 30 d
  "completed_count": 18,
  "last7": [{"status": "success", "run_id": "20260524T080000"}, ...]
}
```

Process-local 30 s TTL cache per job id; invalidated explicitly when a run finalises (`invalidate_stats_cache(job_id)`).

### Stuck-run kill

```
POST /api/jobs/<id>/runs/<rid>/kill
```

- 404 when job or run is unknown.
- 409 when the run's status is not `running` or `pending`.
- Loads the persisted `pid` from `run.json` and uses `psutil` to:
  1. `terminate()` the parent + every recursive child,
  2. `wait_procs` with a 5 s grace,
  3. `kill()` whatever survived.
- Finalises `run.json` to `status: failed`, `exit_code: -9`, `killed: true`, `finished_at: now`, with `duration_seconds` derived from `started_at`.

If the executor has already exited (orphan pid), the route still finalises the record — the UI is the authoritative "is this run done?" surface, and a stale `running` row that nothing is actually executing is the bug the kill button fixes.

### Executor watchdog — the running-forever backstop (issue #695)

The kill button above is manual, and the reap below only fires on a *provably dead* pid. Neither covers the case that actually happened: on 2026-07-30 `cleanup-fleet-all-weekly` froze at 14:06 on the visible-job tee's console-backpressure deadlock (#694) and sat `running` in the Jobs UI for **five hours** until someone noticed and tapped Kill. The inner safety net that should have caught it — `claude_progress.py`'s own 45-minute stall watchdog, several layers downstream inside the orchestrator that job spawns — had itself deadlocked, because it emitted its "killing the stalled run" message *before* calling the kill, and that emit blocked on the same jammed pipe chain (`fleet-config#514`).

The lesson generalises: **every inner safety layer shares fate with the thing it guards.** The executor is the one layer whose health depends on nothing the child does — it needs only to keep a thread ticking and be able to call `kill_process_tree` — so that is where the backstop belongs, and it assumes nothing about *why* a run is stuck.

`app/cli/commands/run_job_cmd.py::_RunWatchdog` is a daemon thread started alongside the resource sampler in `_spawn_and_wait`, watching two signals:

- **No output growth** — `output.log` hasn't gained a byte for longer than `no_output_seconds`. Size, not mtime: Windows doesn't refresh a file's directory-entry timestamp while a handle is open, so mtime reads as frozen for a perfectly healthy run. A `stat` that fails is treated as *unknown* and skips the tick — never as "no growth".
- **Total runtime** — wall-clock since spawn exceeds `max_runtime_seconds`.

Either breach calls `src.diagnostics.kill_process_tree` on the child's whole tree. That also unwedges the main thread for free: the child's pipe closes, the tee loop hits EOF, `proc.wait()` returns, and the executor's ordinary finalisation runs — so the watchdog never finalises a run itself, it only records *why* it fired.

**Per-job configuration.** Both `max_runtime_seconds` and `no_output_seconds` are optional `Job` fields (`config/jobs.json`, settable through `POST`/`PUT /api/jobs`), and both are **tri-state**: absent means "use the default", a positive int is that many seconds, and an explicit `0` **disables that signal** for the job. Zero is deliberately *not* folded into "unset" the way `cooldown_seconds` folds it — for a watchdog, "off" and "use the default" are opposite instructions.

**Defaults.**

- `no_output_seconds` → 1 hour (`_WATCHDOG_DEFAULT_NO_OUTPUT_SECONDS`). Generous on purpose: a job that prints nothing until it finishes is common and must not be killed for being quiet.
- `max_runtime_seconds` → `src.jobs_stats.derived_runtime_ceiling_seconds(job_id)`, the same `max(p95 × 3, 300 s)` heuristic behind the ⚠️ stuck badge (`stuck_threshold_seconds`, now the single definition both read). **But only once the job has at least `WATCHDOG_MIN_COMPLETED_RUNS` (5) completed runs on record** — below that it returns `None` and the run gets *no* runtime ceiling. A false ⚠️ costs a glance; a false auto-kill of a healthy first-ever run is not recoverable, so thin history is reported as unknown rather than collapsed onto the 300 s floor. Give such a job a ceiling by setting `max_runtime_seconds` explicitly.

**Discipline inside the thread.** Both ceilings are resolved on the *main* thread before it starts (`_resolve_watchdog_limits`), so the loop itself touches nothing but `Path.stat`, `Popen.poll`, and a bounded `Event.wait` — no config load, no run-history read, no notifier. It does not log inline either: the root logger writes to `stderr`, which for a `visible` job is the same console a wedge may already have jammed, so the kill happens **first** and the breadcrumb goes out afterwards on a throwaway daemon thread (`_log_off_thread`). That ordering is `fleet-config#514`'s lesson applied directly. A child that exited on its own a moment before a breach is caught by a `poll()` check and is never reported as watchdog-killed.

**Finalisation.** `status: "failed"`, plus `watchdog: true`, `watchdog_reason`, and a human `note` — its own state, not folded into a normal failure or a manual kill. The Jobs UI run row renders one "who ended this" chip: ⏳ `watchdog <reason>`, or 🛑 `killed` for the manual path (`endedChip` in `app/webapp/static/jobs.js`); a run that ended by itself gets no chip. A watchdog kill is otherwise an ordinary failed run — it feeds the `on_failure` chain, the failure-streak gate, and the notification config exactly like any other failure. There is no auto-retry.

### Stranded-run reap (issue #591)

If the **executor itself** dies before it reaches its own finalise (interrupt, reboot, OOM, a parent shell killing its process tree) — not just the child it's tracking — nothing ever writes a terminal status, and the record stays `running` forever even after the recorded pid is long gone. `src/jobs_reap.py` automates the same reconciliation the kill route already does by hand for that exact case:

- **Where it runs.** Opportunistically on read, not a background sweep — every `GET /api/jobs` poll (`app/webapp/routers/jobs.py::_decorate_job`) and every `Board` "stuck" sweep (`src.board.jobs_attention`) call `reap_stranded_runs(job)` before deciding what to show. `src.jobs_queue.mutex_collision` calls the drain-less `finalize_dead_runs(job)` for the same reason mid-admission-check (draining there could spawn a queued sibling before the rest of that same collision sweep re-checks it).
- **What counts as "provably dead".** Only a recorded `pid` that `psutil` confirms is gone (or has been recycled by Windows — a `pid_create_time` persisted alongside `pid` at spawn, and compared against the live process's actual `create_time()` within 1 s, rules out reuse). No `pid` yet, or a pid that's alive and unverifiable against a create-time hint, is left alone — a genuinely long-running job is never reconciled out from under itself.
- **What gets swept.** Every non-terminal record in the job's history, not just the latest — an older `running` record superseded by a newer run is invisible to every behavioural consumer (mutex/streak/run-button all read only the latest), but still a lie if a user opens that specific historical run's detail view.
- **What a reap does.** Writes `status: "failed"`, `finished_at`, `duration_seconds` (mirroring the kill route) plus `reaped: true` — distinct from `killed: true`, since this was never killed, just lost track of — invalidates the stats cache, and drains the job's mutex group once if anything was reaped.
- **What it doesn't replace.** The ⚠️ stuck marker (a live-but-slow run) is untouched — reap only fires on a confirmed-dead pid. Nor does it replace the [executor watchdog](#executor-watchdog--the-running-forever-backstop-issue-695) above: reap handles "the executor is gone and the record lied", the watchdog handles "the executor is still here, still waiting, and the child will never finish".

### `next_run` cache

The original v1 issued one `schtasks /Query` per job per `/api/jobs` poll — N+1 fork+exec on Windows. The decoration layer now reads `next_run` out of a single process-local snapshot:

- One bulk `schtasks /Query /FO LIST /V` populates `{task_name: {next_run, enabled}}` for every entry under `\AppLauncher\` — `next_run` backs the countdown, `enabled` backs the [missed-fire coverage](#missed-fire-coverage-issue-697) structural check, so both read one query.
- The snapshot is cached for **30 s** (`_NEXT_RUN_TTL_SECONDS` in `src/jobs_schtasks.py`).
- A *failed* query caches as `None`, distinct from `{}` ("the query worked and there are no `\AppLauncher\` tasks") — without that distinction one failed query would flag every scheduled job as missing its entry.
- `sync_schtasks` and `delete_schtasks` call `invalidate_next_run_cache()` at the end so user edits show up on the next poll without waiting out the TTL. That also drops the derived coverage scan, which reads this same snapshot.

Net effect: `GET /api/jobs` performs at most one `schtasks` invocation per cache window regardless of job count.

## Failure notifications

Set the Pushover keys in `config/webapp_config.json` and flip `notify_on_failure: true` — the executor will fire a single push per failed run (master switch defaults off, so the feature ships dormant).

```json
{
  "pushover_api_token": "azGDORePK8gMaC0QOYAMyEEuzJnyUi",
  "pushover_user_key":  "uQiRzpo4DXghDmr9QzzfQu27cmVRsG",
  "notify_on_failure":     true,
  "notify_failure_streak": 3,
  "notify_failure_summary": false
}
```

| Key | Default | Effect |
| --- | --- | --- |
| `pushover_api_token` / `pushover_user_key` | `""` | Both must be set for any push to fire; otherwise the notifier short-circuits as a no-op |
| `notify_on_failure` | `false` | Master switch — even with creds present, nothing is sent until this flips on |
| `notify_failure_streak` | `0` | When > 0, also fires a separate "🔁 N consecutive failures" push when the streak ticks to exactly this count. Useful when individual-failure pushes are muted via Pushover quiet hours |
| `notify_failure_summary` | `false` | When `true`, pipe the last ~500 chars of `output.log` through the local LLM hub (`http://127.0.0.1:8000`, `claude-haiku-4-5`) and prepend the model's one-line root-cause summary to the push body. Hub down → silently falls back to raw tail |

The push body always includes: optional LLM summary, the raw output tail (last 500 chars), then a footer `— job=<id> run=<rid> exit=<code>`. Pushover caps individual messages at ~1024 chars; longer bodies are truncated server-side, so the tail is what the executor budgets toward.

The notifier path is wrapped in a single `try`/`except` — credentials misconfigured, Pushover 5xx, hub unreachable: none of those can block the executor's normal exit. Errors land in the launcher log at `WARNING`.

### Per-job Telegram alerts (issue #597)

`notify_on_failure` above is global — every job, one shared Pushover channel. A job can additionally carry an `alert_on_failure: true` flag (the **🔔 Alert to Telegram if failed** toggle in the editor, sitting just above **Require confirmation before running**) that fires a Telegram message on *that job's* failed runs only, independent of the global switch — opt-in per job so a shared Telegram chat isn't spammed by every job's failures.

The channel is the fleet's vendored `src/notify/` Telegram primitive (byte-identical to `whatsapp-radar` and `home-automation`'s copies, sourced from `project-scaffolding`) — one Bot API `sendMessage` HTTPS POST via stdlib `urllib`. Set both credentials in `config/webapp_config.json`:

```json
{
  "telegram_bot_token": "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
  "telegram_chat_id":   "987654321"
}
```

| Key | Default | Effect |
| --- | --- | --- |
| `telegram_bot_token` / `telegram_chat_id` | `""` | Both must be set for any alert to fire; otherwise the notifier short-circuits as a no-op |
| `Job.alert_on_failure` | `false` | Per-job opt-in — set from the editor's toggle, or `"alert_on_failure": true` in `config/jobs.json` |

The message is short and job-scoped: `❌ <job name> failed` as the title, `<timestamp> — run=<rid> exit=<code>` as the body — no LLM summary, no streak logic (those are Pushover-specific enrichments on the global channel). Same swallow-on-error contract as Pushover: a misconfigured token or a Telegram-side failure is logged at `WARNING` and never blocks the executor's normal exit. A job with the toggle on shows a 🔔 bell icon next to its name in the Jobs tab list.

## Missed-fire coverage (issue #697)

Failure alerting covers a run that **failed**; the ⚠️ stuck marker covers one that never **ended**. `src/jobs_coverage.py` covers the third case — a run that never **started**. A job whose Task Scheduler entry is missing, disabled, or was never created simply doesn't fire, and the absence is otherwise invisible: the row keeps showing its old stats and nothing alerts. This is not hypothetical — `config-map` and `sota-watch` shipped launchers plus "runs weekly unattended" docs with *no registered task at all* for weeks, found only by a manual coherence review.

Two halves, both answered from data the tab already reads:

- **Structural** — every non-paused, scheduled job must have a matching, **enabled** `\AppLauncher\<id>` entry (every fan-out slot for `daily_times`). This is what catches a deleted entry immediately, without waiting for the missed slot itself. It reads the *same* 30 s cached bulk `schtasks /Query` the `next_run` column already pays for (see [`next_run` cache](#next_run-cache)) — one batched query per cycle, never an N+1 shell-out storm.
- **Behavioural** — the schedule is expanded across the last `COVERAGE_WINDOW_DAYS` (3) via `upcoming_fires`, and each elapsed slot is matched against the on-disk run records.

The never-flag rules are the load-bearing half — a coverage check that cries wolf gets muted, and then it protects nothing:

| Rule | Why |
| --- | --- |
| Paused jobs and `schedule: none` jobs report `exempt` | There is no schedule to miss |
| `minutes` / `hourly` skip the behavioural half | Too dense to enumerate (same `FREQUENT_SCHEDULE_TYPES` the agenda summarises). The structural half still covers them, and that is what detects a deleted entry |
| A slot counts as missed only once it is 15 min past (`MISSED_FIRE_GRACE_SECONDS`) | Task Scheduler starts late; a machine waking from sleep starts very late |
| **Any** run record near the slot counts as a fire | Including `skipped` — a cooldown no-op *did* fire, it just declined to do work — and a manual run that happened to cover the slot |
| The window never reaches past `added_at`, nor past the oldest retained run when history is at its 20-run cap | A pruned record is not evidence of a missed fire |
| A failed `schtasks` query reports `unknown`, never "missing" | One bad query must not flag every job. An unestablished fact gets its own state and is never folded into the passing *or* the failing one |

**Surfaces.** `GET /api/jobs` decorates each row with a `coverage` object — `{state, detail, problems, missing_tasks, disabled_tasks, missed_count, missed_fires}`, where `state` is `ok` / `problem` / `unknown` / `exempt`. Only `problem` renders: a red **⚠ not firing** pill on the row's chip line, titled with the detail.

**Alerting** reuses the exact channels the failure path uses — global Pushover gated by `notify_on_failure`, per-job Telegram gated by `Job.alert_on_failure` — because a coverage problem is a job problem, not a new notification surface. A background tick in the webapp's lifespan re-scans on an interval (the whole point being that it does *not* depend on the Jobs tab being open); it is skipped entirely on a disposable instance (the e2e / verify-before-ship autoboot webapp, identified by `LAUNCHER_SESSION_HOST_PORT`) so a throwaway instance never pushes a real alert. Pings are de-duplicated through `webapp/jobs/coverage-alerts.json`: a job re-alerts only when its problem *signature* changes or 24 h have passed, and a job whose coverage recovers is dropped from the state so its next break alerts immediately.

| Key | Default | Effect |
| --- | --- | --- |
| `jobs_coverage_interval_minutes` | `60` | Minutes between background coverage scans. `0` disables the tick entirely — the `/api/jobs` badge still computes lazily on poll. On by default because it pushes nothing on its own: alerts still route through the two opt-in gates above |

## Per-job secrets & env (issue #72)

A job can declare an `env: {NAME: value}` overlay in `config/jobs.json` (or via the create/edit API — the field round-trips through both routes). The executor merges it into the child's environment at fire time, after `os.environ` and before the typed-param env (so a per-run param can override a static value). Values are either literals or `$secret:<key>` references resolved against `webapp_config.json`'s `secrets` block — the same mechanism `webhook.secret` uses:

```json
{
  "id": "linkedin-sync",
  "name": "LinkedIn sync",
  "script_path": "E:\\automation\\social\\sync.py",
  "env": {"LINKEDIN_API_KEY": "$secret:linkedin_api"}
}
```

`jobs.json` and every API response carry only the opaque reference — the real value lives in the gitignored `secrets` block and appears nowhere else. An unresolvable reference finalises the run as `failed` with `note: "secret '<key>' not found"` (and the 🧪 dry-run "check" mode catches it without firing anything). Env names are UPPER_SNAKE_CASE, same shape as a `Param`'s `env` mapping.

## Scoped API tokens (issue #72)

The Settings tab's **API tokens** panel mints bearer tokens whose scope is a specific job (or `"*"` via the API). A job-scoped token can call exactly one thing — `POST /api/jobs/<id>/run` for its allowed jobs — and is rejected with a precise 403 everywhere else, so the URL baked into a Stream Deck button no longer unlocks the whole SPA if the deck config leaks. Records live in `webapp_config.json`'s `api_tokens` list as `{id, label, salt, hash, scope, created_at, last_used_at}`: only a salted SHA-256 hash is stored, the raw token is shown exactly once at mint time (with a copy button and, when the tunnel URL is known, a ready-to-paste run URL). Revoking a token deletes its record; the legacy `auth_token` keeps working unchanged with implicit `"*"` scope, and rotating a deck token never touches the phone's login. Endpoints: `GET /api/tokens`, `POST /api/tokens {label, jobs: [ids]}`, `DELETE /api/tokens/<id>` — all reachable only with full-scope auth.

## Security boundary

Jobs sit on the **Apps tab side** of the launcher's security model — not the interactive-terminal side:

- `POST /api/jobs/<id>/run` is bearer-token gated and reachable over the Cloudflare tunnel. That is the whole point — a Stream Deck button hits the same HTTPS endpoint the phone uses. Both the legacy `auth_token` and a minted job-scoped token (see "Scoped API tokens" above) pass the gate; the scoped kind passes *only* here.
- `POST /api/jobs/<id>/hook` (issue #73) is the one exception — it is exempt from the bearer gate entirely and authenticates itself via the job's own provider signature instead. See "Webhook-target jobs" above.
- The per-run WebSocket is an output-only tail, not an interactive terminal. It carries the same bearer-token boundary as the Jobs HTTP APIs and remains Cloudflare-reachable; the Tailscale-only + passkey terminal gate does not apply.
- The `id` is checked against the registry on every call — the launcher cannot be coerced into running an arbitrary script path. Mutating `config/jobs.json` is the only way to register a new target.

## Stream Deck recipe

A Stream Deck "Website / System" action calls the run endpoint directly — no plugin needed:

```
URL:    https://launcher.<your-domain>/api/jobs/reporting-daily/run?token=<job-scoped-token>
Method: POST
```

Mint the token in **Settings → API tokens**, scoped to exactly this job (issue #72) — the mint result shows the ready-to-paste run URL when the tunnel is up. Stream Deck stores button URLs in plaintext, so a scoped token caps what a leaked deck export can do: fire this one job, nothing else. Revoke + re-mint to rotate it without touching the SPA's own `auth_token` (which also still works in the URL if you accept the wider blast radius). Use a tunnel URL (Cloudflare named tunnel or `<host>.<tailnet>.ts.net:8445`) — not loopback. The Stream Deck shows ✓ / ✗ based on the HTTP status; the SPA shows the run in history on the next poll, with a 🎛 chip carrying the token's label.

## Why not …

- **A DB as the canonical run store.** Files stay simpler and cold-reader friendly. SQLite is used only as a rebuildable metadata/FTS mirror so cross-run search and aggregate reads do not repeatedly walk the filesystem.
- **A custom scheduler daemon / APScheduler.** Windows already has a scheduler; running a second one inside the launcher process couples job firing to the launcher's lifecycle. With Task Scheduler the schedules survive a tray restart, a reboot, and a launcher uninstall (until the user cleans up `\AppLauncher\` themselves).
- **A live PTY per job.** One-shot scripts do not need input or terminal emulation. The lightweight output-only WebSocket tails `output.log`; interactive-terminal infrastructure remains reserved for Coding.
- **Raw cron expressions.** The five presets cover real use without inviting the standard "did I get the day-of-week field right?" pitfall.

## Verification

The pre-ship gate (`pwsh -File scripts/verify-before-ship.ps1`) runs the unit suite (`tests/test_jobs.py`, `tests/test_webapp_api_jobs.py`) plus the e2e Jobs-tab smoke check in `tests/e2e/test_smoke.py::test_tabs_switch`. All schtasks calls are mocked at the runner-callable seam (`src.jobs._run_schtasks`) so the unit suite never invokes real Task Scheduler.

Live verification after restart of `:8445`:

```powershell
# Confirm \AppLauncher\ tasks materialised correctly
schtasks /Query /FO CSV /NH | findstr "AppLauncher"

# Trigger a run from the CLI (same path the webapp uses)
curl -k -X POST "https://127.0.0.1:8445/api/jobs/reporting-daily/run"

# Inspect the run record
type webapp\jobs\reporting-daily\<latest>\run.json
type webapp\jobs\reporting-daily\<latest>\output.log
```
