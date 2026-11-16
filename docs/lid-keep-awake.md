# Awake with the lid closed

**Settings › Awake with lid closed** keeps the PC running when the lid is closed. It sets the active Windows power plan's **lid close action** to *Do nothing*, on AC and on battery. It is opt-in and reversible, and nothing changes until you flip it.

## How it works

The switch runs `powercfg` (no window, no admin rights) against the active plan, `SCHEME_CURRENT`, subgroup `SUB_BUTTONS`, setting `LIDACTION`:

- **Read:** `powercfg /q SCHEME_CURRENT SUB_BUTTONS LIDACTION` gives the plan's GUID and its AC and battery values. The switch reads it every time Settings loads, so it shows what the machine holds, never a remembered value.
- **On:** the plan's GUID and both current values go to `webapp/lid-keep-awake.json` first (gitignored), then `/setacvalueindex` and `/setdcvalueindex` set both to `0` and `/setactive SCHEME_CURRENT` applies them. The result is read back; if Windows did not keep it, the change is rolled back and the page says so.
- **Off:** the saved values go back to the plan they came from, even if you have switched plans since, and the saved entry is cleared. A plan that no longer exists has nothing left to restore and is dropped.

Switching plans while the lid switch is on leaves the other plan's own lid action alone. Turn it on again to cover the new plan; each plan keeps its own saved values.

## States

The switch shows **on** (both values `0`), **off**, or **unknown**: `powercfg` could not be run or its answer could not be read, with the reason beside it. Unknown is never shown as off. On a PC whose plan has no lid close action (a desktop, where `powercfg` prints the plan and no values), the row is hidden. A failed change shows Windows' plain reason beside the switch and in a toast.

If the plan already ignores the lid and nothing is saved (you set it by hand), the switch cannot guess a value to put back. Turning it off says so; change the action in Windows Power Options.

## Undo by hand

From any PowerShell, with `<guid>` the plan GUID in `webapp/lid-keep-awake.json` and the saved values for `<ac>` and `<dc>` (`1` is Sleep, `2` is Hibernate, `3` is Shut down):

```powershell
powercfg /setacvalueindex <guid> SUB_BUTTONS LIDACTION <ac>
powercfg /setdcvalueindex <guid> SUB_BUTTONS LIDACTION <dc>
powercfg /setactive SCHEME_CURRENT
```

Or open **Control Panel › Power Options › Choose what closing the lid does**.
