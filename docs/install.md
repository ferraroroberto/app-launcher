# Install, update and migrate app-launcher

app-launcher installs, updates and migrates the way any app in the toolbox does: clone it and run one `setup.bat`. An update is a pull plus a restart. A migration, onto a fresh clone of the same PC or of another one, is one `setup.bat` call. None of them resets or overwrites what a PC has configured.

## Install

You need Windows 10 or 11, `git`, and Python 3.11 or newer (the `py` launcher, or `python` on `PATH`). Optional: [Tailscale](https://tailscale.com/) for the phone-trusted HTTPS certificate; a `fleet-config` checkout for the orphan-proof `tray.bat --restart` (a plain `tray.bat` start works without it); the GitHub CLI (`gh`) for the Board; Node.js, which bundles the page scripts; and the CLI of each coding agent you want ([installing-agent-clis.md](installing-agent-clis.md)).

```powershell
git clone https://github.com/ferraroroberto/app-launcher.git
cd app-launcher
.\setup.bat
.\tray.bat
```

`setup.bat` runs `src/installer.py`, with this app's inputs declared in the `[install]` table of `.fleet.toml`. It:

1. creates `.venv` when it is missing;
2. installs `requirements.txt`, and later reinstalls only when that file changed (its hash is kept inside `.venv`);
3. creates each ignored `config/*.json` from its tracked `config/*.sample.json` when the file is missing;
4. ends with two lists: the required items, each `ok`, `missing` or `unknown`, and the optional features, each `available`, `off` or `unknown` with what it unlocks.

Setup has succeeded when it ends with `Setup complete` and exits 0; an optional feature that is `off` never changes that. The optional features, each needed only for what it unlocks:

- **Sign-in password**, for remote sign-in: `.venv\Scripts\python.exe -m src.password set`.
- **HTTPS certificate**, for phone access: `.venv\Scripts\python.exe scripts\gen_tailscale_cert.py` (see the README's HTTPS section).
- **Shared tray helper**, for `tray.bat --restart` and for a migration: it comes with `fleet-config`.
- **Claude Code CLI, GitHub CLI, Node.js:** install the ones you use.
- **Start at log on:** `.\setup.bat -Startup`, or turn it on in Settings.

`config/apps.json` and `config/jobs.json` start as the sample rows, which point at placeholder paths: replace or delete them. The project folders in `config/webapp_config.json` can stay empty, which means "the folder beside this checkout" ([config.md](config.md)).

`.\setup.bat -WhatIf` lists every change it would make and makes none.

### Execution policy

Nothing here needs a signed script or overrides the PC's PowerShell execution policy, `AllSigned` included. `setup.bat`, `tray.bat` and `scripts/webapp.bat` are batch files. `tray.bat` loads the shared tray helper as text, and the operator-only session-host restart starts through `src/launching/ps_script.py` (`.venv\Scripts\python.exe -m src.launching.ps_script scripts\restart-session-host.ps1 -Confirm`), which falls back to that ScriptBlock launch under `AllSigned` or `Restricted`. PowerShell jobs in the Jobs tab start the same way.

### Uninstall

1. Turn off **Start at log on** in Settings, or delete `AppLauncher.bat` from your Startup folder (`shell:startup`).
2. Remove any job schedules you made in the Jobs tab: they are Task Scheduler tasks under the `\AppLauncher\` folder.
3. If you turned on the remote power guard in Settings, turn it off there first ([remote-power-guard.md](remote-power-guard.md)).
4. Quit the tray from its menu (this ends every live terminal), then delete the `app-launcher` folder. Keep a copy of the files listed under [Migrate](#migrate) first if you may want them back.
5. Optional: delete its runtime data under `C:\sqlite\app-launcher` (or under `FLEET_DATA_ROOT`) and the power guard log under `%LOCALAPPDATA%\AppLauncher`.

## Update

```powershell
git pull --ff-only
.\tray.bat --restart
```

Every file the app writes while it runs (configs, state, logs, `tmp/`, certificates, `.venv`) is gitignored, and no tracked file is rewritten at runtime, so `git status` stays clean and the pull always fast-forwards. The ports in `tray.bat`, `scripts/webapp.bat` and `scripts\restart-session-host.ps1` come from this PC's ignored config through `scripts/webapp_port.py`, so moving a port never means editing a tracked file. A new setting works at its default with no further step.

Three cases need more than those two lines:

- **The pull changed `requirements.txt`:** run `.\setup.bat` before the restart.
- **No shared tray helper on this PC:** `--restart` refuses. Quit from the tray menu, then run `.\tray.bat`. Quit also ends every live terminal.
- **The pull changed the session-host:** the restart above keeps the old one, and your terminals, running. The operator restart is in the README's restart matrix.

Running `setup.bat` on a configured PC is safe, and is how a new setting gets written into your file:

- Settings files (`config/config.json`, `config/webapp_config.json`): an existing value is never changed. A key the sample has and the file lacks is added, after a timestamped backup beside it (`config/<name>.json.bak-<YYYYmmdd-HHMMSS>`), and the added key names are printed. A file that does not parse is left alone and reported, and the run exits 1.
- Registries (`config/apps.json`, `config/jobs.json`, `config/channel_profiles.json`) are your own lists: created when missing, never rewritten or reformatted.
- The log-on entry is added only with `-Startup` and only when absent.
- A second run reports `unchanged` and writes nothing.

## Migrate

What moves is declared in `[install.migrate]` of `.fleet.toml`. All of it is gitignored, and each path is the same in the new folder. The installer does the whole move; nothing here or in `setup.bat` deletes the old copy.

| Carried over | What it holds |
| --- | --- |
| `config\config.json`, `config\webapp_config.json` | Settings, the access token, API tokens, notification keys |
| `config\app_password.json` | The sign-in password hash |
| `config\webauthn_devices.json` | Enrolled passkeys |
| `config\apps.json`, `config\jobs.json`, `config\channel_profiles.json` | Your registries |
| `config\process_auto_restart.json` | Night auto-restart state |
| `webapp\certificates\` | The HTTPS certificate and key |
| `webapp\cloudflared.yml` | Named tunnel settings |
| `webapp\terminal-themes.json` | Your terminal colours |
| `webapp\jobs\` | Job run history and queue |
| `webapp\sessions\`, `webapp\terminal_audit.log` | Terminal transcripts and the audit trail |
| `webapp\outbox\` | Files agents sent to a session's Chat |
| `webapp\chief-pointer.json`, `webapp\chief-answered.json`, `webapp\chief-journal-look.json` | The Board's chief conversation, answers and journal marks |
| `webapp\jobs-dismissed.json`, `webapp\ci-dismissed.json`, `webapp\ci-failed-seen.json`, `webapp\machine-critical-open.json` | Which alerts were already seen or dismissed |
| `webapp\owner-check-marks.json`, `webapp\life-os-skill-hashes.json`, `webapp\progress-totals.json` | Owner-check marks, Life OS fingerprints, GitHub totals |
| `webapp\lid-keep-awake.json` | The power plan values to restore when the lid keep-awake switch goes off |
| `webapp\spawned-apps.json` | The apps the launcher started |

Left behind, because the new copy makes its own: `.venv` (a venv cannot be moved), `webapp\js-bundle\`, `webapp\briefs\`, `webapp\last_tunnel_url.txt`, `webapp\restart-baseline.json`, `webapp\bench\` and `webapp\health\` (local reports), the other `*.log` files, `config\*.lock` and `config\*.bak-*`.

Stays where it is, because it lives outside the app folder: the usage, timing and job-index databases and the other runtime data under `C:\sqlite\app-launcher` (or under `FLEET_DATA_ROOT`); the Cloudflare credentials in `%USERPROFILE%\.cloudflared`; the remote power guard task, whose script is stored inside the task.

### Before either way

- **Run it from a plain terminal**, one you opened yourself, never a session the launcher hosts: the run stops app-launcher and every session it hosts, Telegram channel sessions and running jobs included, so pick a quiet moment. The stop refuses a run it recognises as started from the app, but do not rely on that: a launcher started through another spelling of its folder (a junction, another drive letter) is not recognised.
- The new copy is started with `tray.bat --restart`, so the PC needs the shared tray helper from `fleet-config`.

### Reinstall in the same folder (the usual case)

From a terminal whose current folder is outside the app:

```powershell
cd <PARENT FOLDER>
.\app-launcher\setup.bat -InPlace -WhatIf    # read the backup name and every step; nothing changes
.\app-launcher\setup.bat -InPlace
```

It stops the app, keeps the replaced copy as `app-launcher.bak-YYYYMMDD-N`, clones `origin` into the same path, copies the files above, installs, starts and passes only when the new clone serves its own commit. The path is unchanged, so the log-on entry, the job schedules and your shortcuts need no edit.

The new clone holds only what `origin` has. Unpushed commits, local branches, stashes and linked worktrees stay with the backup, so push or finish them first.

### Beside it (another folder, or an old copy that is not a git clone)

Do not run a plain `setup.bat` in the new folder first: it creates configs from the samples, and a file that is already there is never overwritten by the old one.

```powershell
git clone https://github.com/ferraroroberto/app-launcher.git <NEW FOLDER>
cd <NEW FOLDER>
.\setup.bat -MigrateFrom <OLD FOLDER> -StopOld -WhatIf
.\setup.bat -MigrateFrom <OLD FOLDER> -StopOld
```

Only you can do these steps:

1. **The log-on entry.** Delete the old `AppLauncher.bat` from your Startup folder (`shell:startup`), then run `.\setup.bat -Startup`.
2. **Job schedules.** Their Task Scheduler tasks still run the old folder's Python. From the new folder:

   ```powershell
   .\.venv\Scripts\python.exe -c "from src.jobs import sync_schtasks; from src.jobs.config import load_jobs; [sync_schtasks(j) for j in load_jobs().jobs]"
   ```

   Two kinds of job need an elevated PowerShell instead: an `elevated` job ([jobs-tab.md](jobs-tab.md), with the new paths) and a logged-out (`session_less`) job, whose sheet on the Jobs tab shows the script to paste. Then confirm that no task still names the old folder (no output is the good answer):

   ```powershell
   Get-ScheduledTask -TaskPath '\AppLauncher\' |
     Where-Object { "$($_.Actions.Execute) $($_.Actions.Arguments)" -like "*<OLD FOLDER>\*" } |
     Select-Object TaskName
   ```

3. **Repoint what still names the old folder.** The run rewrites old-folder paths in the copied JSON configs and lists what it could not rewrite (`webapp\cloudflared.yml`, for one). Check anything outside the app yourself: shortcuts, Stream Deck actions, other tools' settings.
4. **Check it.** Open the launcher on the phone and the PC, sign in, look over the Apps and Jobs lists, launch one coding session and run one job.
5. **Fall back** if verify fails (the report names the step): stop the new copy and start the old one, whose files are untouched (re-register the schedules from it, as in step 2). After a reinstall in the same folder, rename the new folder away and the backup to the original name first.
6. **Retire the old copy or the backup** once you are sure the new one works, by deleting it yourself. An agent working through this playbook asks you before it deletes anything.

### Coming from the old Flask launcher

Your apps list and Claude flags carry over by hand: copy `automation\launcher\apps_config.json` to `config\apps.json`, and the old `config.json`'s `claude_*` values into `config\webapp_config.json` under the matching keys.
