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

### `script_path` — `.py` or `.bat`

The two cases dispatch differently:

| Suffix | How it runs | cwd | Notes |
| --- | --- | --- | --- |
| `.py` | `<venv>/python.exe <script> <args>`, with `PYTHONPATH = <project root>` | project root (= dir containing the resolved `.venv`) | The executor walks up from `script_path.parent` looking for `.venv\Scripts\python.exe`; falls back to `sys.executable`. The PYTHONPATH bit fixes the "out-of-tree script imports project packages" gotcha — see global CLAUDE.md. |
| `.bat` | `cmd.exe /c <script> <args>` | `script_path.parent` | The bat does its own venv dance; the executor doesn't intervene. |

`args` is split on whitespace. If you need an argument containing spaces, put it inside the `.bat` / `.py` wrapper rather than relying on shell quoting.

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
  "script_path": "E:\\automation\\claude-config\\audit-fleet.bat",
  "schedule": { "type": "weekly", "day": "THU", "at": "22:00" },
  "visible": true
}
```

- **Interpreter.** The scheduled task's `/TR` runs under `python.exe` (console subsystem) instead of the default silent `pythonw.exe`, so a console window appears when the task fires in the logged-on session. `src.jobs.task_run_command(job_id, visible=…)` picks the interpreter; `src.jobs._python_path()` resolves the launcher venv's `python.exe` with a PATH fallback that mirrors `_pythonw_path()`.
- **Output tee.** The executor spawns the child with `stdout=PIPE` and streams its combined output to **both** `output.log` (the remote run-history record, unchanged) and the launcher's own console (`sys.stdout.buffer`, guarded). A pythonw / detached fire has no console, so the console half is silently dropped while the file half always works. A non-visible job keeps the original direct-to-file spawn — no pipe, no reader, byte-for-byte unchanged.

`visible` round-trips through `POST`/`PUT` like `confirm` and is omitted from the stored row when false. It only affects **scheduled** fires meaningfully — a webapp/Stream-Deck fire spawns the executor detached with no console regardless, so the tee's console half no-ops there (the log half still works).

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

## Task Scheduler — `\AppLauncher\` namespace

All Jobs-tab schtasks entries live under the `\AppLauncher\` Task Scheduler folder. The naming rule:

- Single-task schedules → `\AppLauncher\<job_id>`
- `daily_times` → `\AppLauncher\<job_id>-1`, `-2`, … (one per HH:MM)

Sync is idempotent: on every create/edit, the launcher deletes every existing `\AppLauncher\<job_id>*` task first, then re-creates from the current schedule. Edits never leave stale entries behind. Delete-via-API removes both the registry row and every matching schtasks entry.

The `/TR` (task run) command stored in Task Scheduler is quoted so paths containing spaces survive Task Scheduler's own tokenisation:

```
"E:\automation\app-launcher\.venv\Scripts\pythonw.exe" "E:\automation\app-launcher\launcher.py" run-job <job_id>
```

Scheduled runs use `pythonw.exe` (silent — no console window appears on schedule fire). The repo's own `.venv` is preferred; a missing `.venv` falls back to `pythonw.exe` on PATH. A job with `"visible": true` (see "Visible console") instead runs under `python.exe` so a window appears on fire.

To inspect what's actually scheduled:

```powershell
schtasks /Query /TN "\AppLauncher\reporting-daily" /FO LIST /V
schtasks /Query /FO CSV /NH | findstr "AppLauncher"
```

## Run history — `webapp/jobs/<job_id>/<run_id>/`

Every run produces a directory with two files:

| File | Content |
| --- | --- |
| `run.json` | One run's full metadata (schema below) |
| `output.log` | Combined stdout+stderr, raw bytes |

`run_id` is a sortable timestamp (`YYYYmmddTHHMMSS`); collisions within the same second append `-2`, `-3`, … Pruned to the most recent **20 runs per job** by the executor at the end of each run, so the directory never grows unbounded.

`status` transitions: `pending` (webapp pre-create) → `running` (executor takes over) → `success` | `failed`. The UI shows the live status by polling `/api/jobs` every 4 s while the tab is visible.

### `run.json` schema

| Field | Type | Written by | Purpose |
| --- | --- | --- | --- |
| `run_id` | str | webapp + executor | Sortable timestamp; matches the dir name |
| `job_id` | str | both | FK to `config/jobs.json` |
| `name` | str | both | Job name at the time of the run (denormalised on purpose — survives renames) |
| `trigger` | `"manual"` \| `"scheduled"` | both | Where the run was fired from |
| `script_path` | str | both | Resolved at spawn time |
| `args` | str | both | Whitespace-split into argv |
| `params` | object | webapp + executor | Typed-parameter payload (issue #67); only written when non-empty |
| `started_at` | ISO 8601 | both | `pending` write or `running` re-write |
| `status` | `"pending"` \| `"running"` \| `"success"` \| `"failed"` | both | Final value lands at executor exit |
| `finished_at` | ISO 8601 | executor | Only on final write |
| `exit_code` | int | executor | `-9` is reserved for `/kill` (`SIGKILL` analogue) |
| `pid` | int | executor | The child PID, persisted at spawn so the kill endpoint works even if the executor itself crashes between spawn and `wait()` |
| `duration_seconds` | float | executor | Wall-clock seconds the child ran for; rounded to 3 d.p. |
| `peak_rss_bytes` | int | executor | Peak resident-set size summed across the process tree (parent + recursive children) — sampled at ~1 Hz |
| `cpu_seconds` | float | executor | Accumulated user + system CPU across the tree — sum of per-PID maxima |
| `killed` | bool | kill endpoint | `True` only when finalised via `/kill` |

Plain files were a deliberate choice over a DB — same pattern as session transcripts and audit logs. A future LLM/human can `cat` a run record without any tooling.

## Authoring safety — pre-flight on save (issue #69)

Adding a job used to be a leap of faith: the first scheduled fire was when you found out the path was wrong or the venv didn't walk up. `src/jobs_preflight.py::preflight(job)` front-loads those checks at save time so the dialog can surface problems *before* the schedule starts ticking. It is a **pure function** (no subprocess, no globals) — both so it is trivially unit-testable and so a request handler never shells out to `schtasks.exe`.

Two severities:

- **error** — the job cannot run as configured; the save is **blocked** with HTTP 400.
- **warning** — the job will run, but probably not the way the author expects; it **saves once acknowledged**.

Checks performed:

1. **`script_path` exists** → *error* if the file is missing (the single most common authoring mistake).
2. **`.py` venv walk-up** → *warning* when no ancestor `.venv\Scripts\python.exe` is found; the executor will fall back to `sys.executable`. Mirrors the executor's own `resolve_venv_python` (now living in `src/jobs.py`) so the check matches runtime behaviour exactly.
3. **`.bat` embedded `.venv` reference** → *warning* when the wrapper names a `.venv` interpreter / `activate` path that doesn't resolve (best-effort text scan).
4. **`args` lex** → *error* when `shlex.split(args, posix=False)` raises (e.g. an unbalanced quote), rather than letting the executor mangle a value silently.

### Two-phase flow (errors block, warnings confirm)

`POST /api/jobs` and `PUT /api/jobs/<id>` both run pre-flight on the effective job:

- **Error present** → `400 {"detail": {"reason": "preflight", "problems": [...]}}`. Nothing is persisted.
- **Warnings only, not acknowledged** → `200 {"saved": false, "warnings": [...]}`. Nothing is persisted; the dialog stays open showing the warnings with a **Save anyway** button.
- **Warnings acknowledged** (`"acknowledge_warnings": true` in the body) **or no problems** → the row is saved and the response is `{"job": ..., "saved": true, "warnings": [...]}` (the warnings are echoed back so the UI can still note them).

Each `Problem` is `{level, field, message}` — `field` (`script_path` / `args`) lets the dialog place the message next to the offending input.

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
| `GET /api/jobs` | bearer-token | List jobs, decorated with `schedule_chip`, `target_kind`, `next_run`, `last_run`, `running` |
| `POST /api/jobs` | bearer-token | Create — body `{name, script_path, args?, schedule?}` |
| `PUT /api/jobs/<id>` | bearer-token | Edit (re-syncs schtasks) |
| `DELETE /api/jobs/<id>` | bearer-token | Remove + delete schtasks entries |
| `POST /api/jobs/<id>/run` | bearer-token | Trigger now (returns `run_id`, spawns executor detached) |
| `GET /api/jobs/<id>/runs` | bearer-token | Newest-first run history |
| `GET /api/jobs/<id>/runs/<run_id>` | bearer-token | One run's metadata + output tail (last 64 KB) |
| `POST /api/jobs/<id>/runs/<run_id>/kill` | bearer-token | Terminate a stuck run's process tree, finalise `run.json` (`status: failed`, `exit_code: -9`, `killed: true`) |

## Operational signal (issue #66)

The row carries five lightweight signals on top of the schedule chip and last-run line, recomputed on every `/api/jobs` poll:

- **Duration chip** — `p50 4.2s · p95 11s` over completed runs of this job. Hidden when there are no completed runs yet.
- **Sparkline** — `●●●○●●●` over the last 7 runs, oldest-left. Green = success, red = failed, amber = running/pending, grey = unknown.
- **Success rate / 30 d** — appears in the meta line when there has been at least one completed run in the last 30 days (`72% / 30d`).
- **⚠️ stuck marker** — the latest run is in `running` status and has been running for more than `max(p95 × 3, 300 s)`. The marker is *surface only* — auto-kill is intentionally out of scope; a human still chooses to act.
- **CPU / peak RSS** — surfaced on the selected run's output label inside the expanded panel (`Output · <rid> · success · 47 s CPU · peak 1.3 GB`).

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

### `next_run` cache

The original v1 issued one `schtasks /Query` per job per `/api/jobs` poll — N+1 fork+exec on Windows. The decoration layer now reads `next_run` out of a single process-local snapshot:

- One bulk `schtasks /Query /FO LIST /V` populates `{task_name: next_run_iso_or_none}` for every entry under `\AppLauncher\`.
- The snapshot is cached for **30 s** (`_NEXT_RUN_TTL_SECONDS` in `src/jobs.py`).
- `sync_schtasks` and `delete_schtasks` call `invalidate_next_run_cache()` at the end so user edits show up on the next poll without waiting out the TTL.

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

## Security boundary

Jobs sit on the **Apps tab side** of the launcher's security model — not the interactive-terminal side:

- `POST /api/jobs/<id>/run` is bearer-token gated and reachable over the Cloudflare tunnel. That is the whole point — a Stream Deck button hits the same HTTPS endpoint the phone uses.
- There is **no** interactive stream to drive, so the Tailscale-only + passkey gate that the live terminal requires does not apply.
- The `id` is checked against the registry on every call — the launcher cannot be coerced into running an arbitrary script path. Mutating `config/jobs.json` is the only way to register a new target.

## Stream Deck recipe

A Stream Deck "Website / System" action calls the run endpoint directly — no plugin needed:

```
URL:    https://launcher.<your-domain>/api/jobs/reporting-daily/run?token=<your-bearer-token>
Method: POST
```

The token bakes into the URL the same way the tray menu's "Copy Cloudflare URL" item does it for the SPA. Use a tunnel URL (Cloudflare named tunnel or `<host>.<tailnet>.ts.net:8445`) — not loopback. The Stream Deck shows ✓ / ✗ based on the HTTP status; the SPA shows the run in history on the next poll.

## Why not …

- **A DB for run history.** Files are simpler and consistent with the audit log / session transcripts. Twenty rows per job × dozens of jobs is no scaling concern.
- **A custom scheduler daemon / APScheduler.** Windows already has a scheduler; running a second one inside the launcher process couples job firing to the launcher's lifecycle. With Task Scheduler the schedules survive a tray restart, a reboot, and a launcher uninstall (until the user cleans up `\AppLauncher\` themselves).
- **A live PTY per job.** One-shot scripts don't need a live terminal — captured output + tail is enough. The interactive-terminal infrastructure (session-host, WebSocket proxy, passkey gate, audit log) is reserved for the Coding tab where it earns its complexity.
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
