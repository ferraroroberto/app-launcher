# Install, update and move app-launcher

app-launcher moves to another Windows PC the way you copy an app: clone it and run one `setup.bat`. An update is a pull plus a restart. Neither resets or overwrites what a PC has configured.

## What you need

- Windows 10 or 11, `git`, and Python 3.11 or newer (the `py` launcher, or `python` on `PATH`).
- Optional: [Tailscale](https://tailscale.com/) for the phone-trusted HTTPS certificate; a `fleet-config` checkout for the orphan-proof `tray.bat --restart` (a plain `tray.bat` start works without it); the GitHub CLI (`gh`) for the Board; Node.js, which bundles the page scripts; and the CLI of each coding agent you want ([installing-agent-clis.md](installing-agent-clis.md)).

## Fresh PC

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
4. ends with a checklist that marks each item `ok`, `missing` or `unknown`.

The checklist names what still needs you:

- **Sign-in password:** `.venv\Scripts\python.exe -m src.password set`.
- **HTTPS certificate:** `.venv\Scripts\python.exe scripts\gen_tailscale_cert.py` (see the README's HTTPS section).
- **Shared tray helper:** comes with `fleet-config`; only `tray.bat --restart` needs it.
- **Claude Code CLI, GitHub CLI, Node.js:** install the ones you use.
- **Start at log on:** `.\setup.bat -Startup`, or turn it on in Settings.

`config/apps.json` and `config/jobs.json` start as the sample rows, which point at placeholder paths: replace or delete them. The project folders in `config/webapp_config.json` can stay empty, which means "the folder beside this checkout" ([config.md](config.md)).

`.\setup.bat -WhatIf` lists every change it would make and makes none.

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

## Migrate from an existing install

For a PC that already runs an older copy: one that may not be a git clone, and may live at another path. The old copy keeps running while you prepare the new one, and nothing here deletes it.

### What carries over

All of it is gitignored, and each path is the same in the new folder.

| Copy | What it holds |
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

Leave behind, because the new copy makes its own: `.venv` (a venv cannot be moved), `webapp\js-bundle\`, `webapp\briefs\`, `webapp\last_tunnel_url.txt`, `webapp\restart-baseline.json`, `webapp\bench\` and `webapp\health\` (local reports), the other `*.log` files, `config\*.lock` and `config\*.bak-*`.

Stays where it is, because it lives outside the app folder: the usage, timing and job-index databases and the other runtime data under `C:\sqlite\app-launcher` (or under `FLEET_DATA_ROOT`); the Cloudflare credentials in `%USERPROFILE%\.cloudflared`; the remote power guard task, whose script is stored inside the task.

### Steps

1. **Clone fresh, beside the old copy.** Never clone over it.

   ```powershell
   git clone https://github.com/ferraroroberto/app-launcher.git C:\path\to\new\app-launcher
   ```

2. **Copy this PC's own files across.** Set the two folders, then run the block. A file the old copy does not have is skipped.

   ```powershell
   $old = 'C:\path\to\old\app-launcher'
   $new = 'C:\path\to\new\app-launcher'
   $carry = @(
     'config\config.json', 'config\webapp_config.json', 'config\app_password.json',
     'config\webauthn_devices.json', 'config\apps.json', 'config\jobs.json',
     'config\channel_profiles.json', 'config\process_auto_restart.json',
     'webapp\certificates', 'webapp\cloudflared.yml', 'webapp\terminal-themes.json',
     'webapp\jobs', 'webapp\sessions', 'webapp\terminal_audit.log', 'webapp\outbox',
     'webapp\chief-pointer.json', 'webapp\chief-answered.json', 'webapp\chief-journal-look.json',
     'webapp\jobs-dismissed.json', 'webapp\ci-dismissed.json', 'webapp\ci-failed-seen.json',
     'webapp\machine-critical-open.json', 'webapp\owner-check-marks.json',
     'webapp\life-os-skill-hashes.json', 'webapp\progress-totals.json',
     'webapp\lid-keep-awake.json', 'webapp\spawned-apps.json'
   )
   foreach ($item in $carry) {
     $source = Join-Path $old $item
     if (Test-Path -LiteralPath $source) {
       $parent = Split-Path (Join-Path $new $item)
       New-Item -ItemType Directory -Force $parent | Out-Null
       Copy-Item -LiteralPath $source -Destination $parent -Recurse -Force
     }
   }
   ```

3. **Top them up.** In the new folder run `.\setup.bat -WhatIf`, read what it would change, then `.\setup.bat`. Settings files gain only the keys they lack.

4. **Stop the old copy.** This ends every live terminal and Telegram channel session, so pick a quiet moment.
   - Right-click the old tray icon and choose **Quit**. That stops the webapp, the tunnel and the session-host.
   - Check that nothing still listens (use your own ports if you changed them): `Get-NetTCPConnection -State Listen -LocalPort 8445,8446 -ErrorAction SilentlyContinue`. If a line comes back, stop that process with `Stop-Process -Id <OwningProcess>`.
   - Remove the old log-on entry: delete `AppLauncher.bat` from your Startup folder (`shell:startup`).

5. **Copy once more.** The old copy kept writing until you stopped it: run the block from step 2 again, then `.\setup.bat` again.

6. **Repoint paths into the old folder.** Your own configs may name files inside it, for example a job's `script_path`. This lists the places without printing any value:

   ```powershell
   Set-Location $new
   @(Get-ChildItem config\*.json; Get-Item webapp\cloudflared.yml -ErrorAction Ignore) |
     Select-String -SimpleMatch -Pattern $old, $old.Replace('\', '\\'), $old.Replace('\', '/') |
     Select-Object Path, LineNumber
   ```

7. **Start the new one.** `.\tray.bat`, then `.\setup.bat -Startup` if it should start at log on. A checklist line `[unknown] Start at log on - probe exited 2` means the entry still starts another folder: delete it (step 4) and run `.\setup.bat -Startup` again.

8. **Re-register the job schedules.** Their Task Scheduler tasks still run the old folder's Python. From the new folder:

   ```powershell
   .\.venv\Scripts\python.exe -c "from src.jobs import sync_schtasks; from src.jobs.config import load_jobs; [sync_schtasks(j) for j in load_jobs().jobs]"
   ```

   Two kinds of job need an elevated PowerShell instead: an `elevated` job ([jobs-tab.md](jobs-tab.md), with the new paths) and a logged-out (`session_less`) job, whose sheet on the Jobs tab shows the script to paste. Then confirm that no task still names the old folder (no output is the good answer):

   ```powershell
   Get-ScheduledTask -TaskPath '\AppLauncher\' |
     Where-Object { "$($_.Actions.Execute) $($_.Actions.Arguments)" -like "*$old\*" } |
     Select-Object TaskName
   ```

9. **Verify it.** Open the launcher on the phone and the PC. Check that the build line shows the commit `git rev-parse --short HEAD` prints, sign in, look over the Apps and Jobs lists, launch one coding session and run one job. Check anything else you pointed at the old folder yourself: shortcuts, Stream Deck actions, other tools' settings. If something is wrong, quit the new copy and start the old one: its files are untouched (re-register the schedules from it, as in step 8).

10. **Retire the old folder only after you confirm.** Rename it first (for example `app-launcher.retired`), use the new copy for a while, then delete it yourself once you are sure. An agent following this playbook asks you before it deletes anything.

### Coming from the old Flask launcher

Your apps list and Claude flags carry over: copy `automation\launcher\apps_config.json` to `config\apps.json`, and the old `config.json`'s `claude_*` values into `config\webapp_config.json` under the matching keys.

## Execution policy

Nothing here needs a signed script or overrides the PC's PowerShell execution policy, `AllSigned` included. `setup.bat`, `tray.bat` and `scripts/webapp.bat` are batch files. `tray.bat` loads the shared tray helper as text, and the operator-only session-host restart starts through `src/launching/ps_script.py` (`.venv\Scripts\python.exe -m src.launching.ps_script scripts\restart-session-host.ps1 -Confirm`), which falls back to that ScriptBlock launch under `AllSigned` or `Restricted`. PowerShell jobs in the Jobs tab start the same way.

## Uninstall

1. Turn off **Start at log on** in Settings, or delete `AppLauncher.bat` from your Startup folder (`shell:startup`).
2. Remove any job schedules you made in the Jobs tab: they are Task Scheduler tasks under the `\AppLauncher\` folder.
3. If you turned on the remote power guard in Settings, turn it off there first ([remote-power-guard.md](remote-power-guard.md)).
4. Quit the tray from its menu (this ends every live terminal), then delete the `app-launcher` folder.
5. Optional: delete its runtime data under `C:\sqlite\app-launcher` (or under `FLEET_DATA_ROOT`) and the power guard log under `%LOCALAPPDATA%\AppLauncher`.
