# Hide shut down over Remote Desktop

**Settings › Hide shut down over Remote Desktop** removes Shut down, Restart and Sleep from the Start menu while you are connected to the PC over Windows Remote Desktop. At the PC's own keyboard and screen they stay. It exists because one mis-click in a remote session can power off an unattended host and stop everything running on it until someone is back at the machine.

## How it works

Windows has no setting that hides power options for remote sessions only. The policy that hides them, `NoClose` under `HKCU\Software\Microsoft\Windows\CurrentVersion\Policies\Explorer`, applies to your user in every session. So the guard sets it again each time your session starts or changes hands:

- A scheduled task, `\AppLauncher-Guard\remote-power-guard`, fires on two events from the `Microsoft-Windows-TerminalServices-LocalSessionManager/Operational` log: **21** (logon) and **25** (reconnect, from the console or over Remote Desktop).
- The task runs a short PowerShell script in your own session. It writes `NoClose=1` when the session is remote and `NoClose=0` at the console. If the value changed, it restarts Explorer and the Start menu host in that session only. Windows relaunches Explorer by itself.
- Each run appends one line to `%LOCALAPPDATA%\AppLauncher\remote-power-guard.log` (remote or not, the old and new value, or the error), so you can check what happened after a logon.

The task runs with the highest available privileges, because the `Policies` key can be written only by administrators. The script is stored inside the task definition, not as a file on disk. An elevated task that ran a script file you can edit would hand admin rights to anything running as you.

Sign out stays available everywhere. The guard only hides the power options.

## Turning it on and off

The webapp runs without admin rights, so Windows refuses the change when you flip the switch. The switch then opens a sheet with the one command to run. On the PC, open PowerShell with **Run as administrator**, using your own account, and paste it. From the repo root:

```powershell
.\.venv\Scripts\python.exe -m src.remote_power_guard enable    # register the task
.\.venv\Scripts\python.exe -m src.remote_power_guard disable   # remove it and reset NoClose to 0
.\.venv\Scripts\python.exe -m src.remote_power_guard status    # state and NoClose, no admin needed
```

`disable` also resets `NoClose` to 0 and restarts the Start menu, so turning the guard off never leaves the console without power options. Turning it on takes effect at your next logon or reconnect.

The switch reads its state live from Task Scheduler every time Settings loads. It shows **on**, **off** or **unknown**. Unknown means the state could not be read, and is never shown as off. If the guard is off but `NoClose` is still 1, Settings warns you and shows the `disable` command that brings the options back.

## Shutting down on purpose from a remote session

With the guard on, shut down or restart from a terminal in the remote session:

```powershell
shutdown /s /t 0    # shut down
shutdown /r /t 0    # restart
```

## Undo by hand

If the app is unavailable, run these from an elevated PowerShell:

```powershell
schtasks /Delete /TN "\AppLauncher-Guard\remote-power-guard" /F
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Policies\Explorer" /v NoClose /f
```

Then sign out and back in, or restart Explorer, to get the Start menu back.
