# Install and update

app-launcher moves to another Windows PC the way you copy an app: clone it and run one install. On a PC that already runs it, an update is `git pull` plus a restart, and nothing else. An install or an update never resets or overwrites what that PC has configured.

## What you need

- Windows 10 or 11 and Python 3 (the `py` launcher or `python` on `PATH`).
- `git`.
- Optional: [Tailscale](https://tailscale.com/) for the phone-trusted HTTPS certificate, and a `fleet-config` checkout for the orphan-proof `tray.bat --restart` (a plain `tray.bat` start works without it).

## Fresh PC

```powershell
git clone https://github.com/ferraroroberto/app-launcher.git
cd app-launcher
.\setup.bat
.\tray.bat
```

`setup.bat` runs `scripts/install.py`, which:

1. creates `.venv` when it is missing;
2. installs `requirements.txt`, and later reinstalls only when that file changed (its hash is recorded inside `.venv`);
3. creates each ignored `config/*.json` from its tracked `config/*.sample.json` when the file is missing;
4. ends with a checklist that marks each item `ok`, `missing` or `unknown`: the venv, the requirements, each config, the sign-in password, the HTTPS certificate, the shared tray helper and the log-on autostart.

The checklist names what still needs you. The usual first-run items:

- **Sign-in password:** `.venv\Scripts\python.exe -m src.password set`.
- **HTTPS certificate:** `.venv\Scripts\python.exe scripts\gen_tailscale_cert.py` (see the README's HTTPS section).
- **Start at log on:** run `.\setup.bat -Startup`, or turn it on in Settings.

Each coding agent's CLI is installed separately: [installing-agent-clis.md](installing-agent-clis.md).

## Already-configured PC

Update with:

```powershell
git pull --ff-only
.\tray.bat --restart
```

No other step is needed. Every file the app writes while it runs (configs, state, logs, `tmp/`, certificates, `.venv`) is gitignored, and no tracked file is rewritten at runtime, so `git status` stays clean and the pull always fast-forwards. The ports in `tray.bat`, `scripts/webapp.bat` and `scripts\restart-session-host.ps1` come from this PC's ignored config through `scripts/webapp_port.py`, so moving a port never means editing a tracked file.

Running `setup.bat` on a configured PC is safe and is how you pick up a new setting:

- Settings files (`config/config.json`, `config/webapp_config.json`): an existing value is never changed. A key the sample has and the file lacks is added, after a timestamped backup beside it (`config/<name>.json.bak-<YYYYmmdd-HHMMSS>`), and the added key names are printed. A file that does not parse is left alone and reported, and the run exits 1.
- Registries (`config/apps.json`, `config/jobs.json`, `config/channel_profiles.json`) are your own lists: created when missing, never rewritten or reformatted.
- The log-on autostart entry is added only with `-Startup` and only when absent. An existing one is reported `kept`.
- A second run reports `unchanged` and writes nothing.

`.\setup.bat -WhatIf` lists every change it would make and makes none.

## Execution policy

Nothing here needs a signed script or overrides the PC's PowerShell execution policy, `AllSigned` included. `setup.bat`, `tray.bat` and `scripts/webapp.bat` are batch files. `tray.bat` loads the shared tray helper as text, and the operator-only session-host restart starts through `src/launching/ps_script.py` (`.venv\Scripts\python.exe -m src.launching.ps_script scripts\restart-session-host.ps1 -Confirm`), which falls back to that ScriptBlock launch under `AllSigned` or `Restricted`. PowerShell jobs in the Jobs tab start the same way.

## Coming from the old Flask launcher

Your apps list and Claude flags carry over: copy `automation\launcher\apps_config.json` to `config\apps.json`, and the old `config.json`'s `claude_*` values into `config\webapp_config.json` under the matching keys.

## Uninstall

1. Turn off **Start at log on** in Settings, or delete `AppLauncher.bat` from your Startup folder (`shell:startup`).
2. Remove any job schedules you made in the Jobs tab: they are Task Scheduler tasks under the `\AppLauncher\` folder.
3. If you turned on the remote power guard in Settings, turn it off there first ([remote-power-guard.md](remote-power-guard.md)).
4. Quit the tray from its menu, then delete the `app-launcher` folder.
5. Optional: delete its runtime data (usage and job index databases) under the fleet data root, `C:\sqlite\app-launcher` unless `FLEET_DATA_ROOT` points elsewhere.
