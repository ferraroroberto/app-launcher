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
      "script_path": "E:\\automation\\reporting\\launch_reporting.bat",
      "args": "auto",
      "schedule": { "type": "daily", "at": "06:00" },
      "added_at": "2026-05-23T07:00:00"
    },
    {
      "id": "linkedin-scrape",
      "name": "LinkedIn Scrape",
      "script_path": "E:\\automation\\reporting\\engagement\\linkedin\\scrape_comments.py",
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

`daily_times` is the one schedule type that fans out into multiple Task Scheduler entries. It exists because "every 6 hours at 06:00 / 12:00 / 18:00 (skip midnight)" doesn't fit any single preset cleanly — `hourly /MO 6` would also fire at 00:00, and three separate jobs would clutter the Jobs tab. The fan-out is invisible to the user: one row in `jobs.json` → one row in the Jobs tab → three wake-ups per day under the hood.

## Task Scheduler — `\AppLauncher\` namespace

All Jobs-tab schtasks entries live under the `\AppLauncher\` Task Scheduler folder. The naming rule:

- Single-task schedules → `\AppLauncher\<job_id>`
- `daily_times` → `\AppLauncher\<job_id>-1`, `-2`, … (one per HH:MM)

Sync is idempotent: on every create/edit, the launcher deletes every existing `\AppLauncher\<job_id>*` task first, then re-creates from the current schedule. Edits never leave stale entries behind. Delete-via-API removes both the registry row and every matching schtasks entry.

The `/TR` (task run) command stored in Task Scheduler is quoted so paths containing spaces survive Task Scheduler's own tokenisation:

```
"E:\automation\app-launcher\.venv\Scripts\pythonw.exe" "E:\automation\app-launcher\launcher.py" run-job <job_id>
```

Scheduled runs use `pythonw.exe` (silent — no console window appears on schedule fire). The repo's own `.venv` is preferred; a missing `.venv` falls back to `pythonw.exe` on PATH.

To inspect what's actually scheduled:

```powershell
schtasks /Query /TN "\AppLauncher\reporting-daily" /FO LIST /V
schtasks /Query /FO CSV /NH | findstr "AppLauncher"
```

## Run history — `webapp/jobs/<job_id>/<run_id>/`

Every run produces a directory with two files:

| File | Content |
| --- | --- |
| `run.json` | `{run_id, job_id, name, trigger, script_path, args, started_at, status, finished_at, exit_code}` |
| `output.log` | Combined stdout+stderr, raw bytes |

`run_id` is a sortable timestamp (`YYYYmmddTHHMMSS`); collisions within the same second append `-2`, `-3`, … Pruned to the most recent **20 runs per job** by the executor at the end of each run, so the directory never grows unbounded.

`status` transitions: `pending` (webapp pre-create) → `running` (executor takes over) → `success` | `failed`. The UI shows the live status by polling `/api/jobs` every 4 s while the tab is visible.

Plain files were a deliberate choice over a DB — same pattern as session transcripts and audit logs. A future LLM/human can `cat` a run record without any tooling.

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
- **Parameterised-run prompts.** `args` is fixed per job. If a job needs to behave differently across triggers, register two jobs.

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
