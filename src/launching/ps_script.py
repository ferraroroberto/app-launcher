"""Start a repo PowerShell script under any execution policy, without weakening it.

``powershell -File x.ps1`` refuses an unsigned script when the effective
execution policy is ``AllSigned`` (or ``Restricted``). Overriding the policy on
the command line would sidestep that by weakening the machine's own control,
so this launcher never does. A script *loaded as text* and invoked as
a ScriptBlock is not a file execution, so the policy does not apply to it and
stays exactly as the machine set it.

``run_script`` is the one place a repo ``.ps1`` is started:

* a policy that lets unsigned local files run (``Bypass`` / ``Unrestricted`` /
  ``RemoteSigned``) -> the plain ``-File`` launch, byte for byte what callers
  did before;
* any other policy, or one that could not be read -> the ScriptBlock launch.

The ScriptBlock launch has no file behind it, so ``$PSScriptRoot`` would be
empty. The bootstrap rewrites that token to the script's real folder, and the
arguments travel in an environment variable (a ``-Command`` string would
re-parse them, splitting a value that contains a space).

CLI, for ``tray.bat`` and other non-Python callers (run from the repo root)::

    python -m src.launching.ps_script scripts/tray_lifecycle.ps1 launch -AppName X
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import List, Mapping, Optional, Sequence, Union

from src.subprocess_flags import NO_WINDOW

POWERSHELL_EXE = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"

# Policies under which an unsigned local script may be started with -File.
FILE_LAUNCH_POLICIES = frozenset({"bypass", "unrestricted", "remotesigned"})

# Bound on the one `Get-ExecutionPolicy` probe (a PowerShell cold start).
_PROBE_TIMEOUT_S = 30

ENV_SCRIPT = "PSL_SCRIPT"
ENV_ARGV = "PSL_ARGV"
# Each argument rides in ENV_ARGV behind one unit-separator character, so an empty
# argument and a value holding spaces both survive (JSON would not: Windows
# PowerShell 5.1 hands a parsed array back as one object).
_ARG_SEP = chr(31)

# One line, single quotes only: it travels as one argv element, and a double
# quote or a backslash before one would be re-escaped on the way. A leading
# `-Name` token is a named parameter (taking the next token as its value unless
# that is another name), anything else is positional -- the same split `-File`
# applies. An `exit` inside the script ends this process with its code; a
# script that simply finishes exits 0, as `-File` does.
BOOTSTRAP = (
    "$path = [IO.Path]::GetFullPath($env:PSL_SCRIPT); "
    "$argv = @($env:PSL_ARGV -split [char]31 | Select-Object -Skip 1); "
    "$pos = @(); $named = @{}; "
    "for ($i = 0; $i -lt $argv.Count; $i++) { "
    "$t = [string]$argv[$i]; "
    "if ($t -match '^-[A-Za-z]') { "
    "if (($i + 1 -lt $argv.Count) -and (([string]$argv[$i + 1]) -notmatch '^-[A-Za-z]')) "
    "{ $named[$t.Substring(1)] = [string]$argv[$i + 1]; $i++ } else { $named[$t.Substring(1)] = $true } "
    "} else { $pos += $t } }; "
    "$PSL_Root = Split-Path -Parent $path; "
    "try { "
    "$text = [IO.File]::ReadAllText($path).Replace('$PSScriptRoot', '$PSL_Root'); "
    "$block = [scriptblock]::Create($text); "
    "& $block @pos @named "
    "} catch { [Console]::Error.WriteLine(($_ | Out-String)); exit 1 }; "
    "exit 0"
)

_log = logging.getLogger(__name__)


def _inherit(stream: object) -> object:
    """The caller's own std handle for the child, or DEVNULL when it has none.

    The child runs with NO_WINDOW, which gives it a console of its own, so a handle left to
    default would be that hidden console and the script's output would never reach the caller
    (a redirected gate log, a tray.bat console).
    """
    try:
        return stream.fileno()  # type: ignore[attr-defined]
    except (AttributeError, OSError, ValueError):
        return subprocess.DEVNULL


def effective_policy(timeout: float = _PROBE_TIMEOUT_S) -> Optional[str]:
    """The effective execution policy, lower-cased, or ``None`` when not confirmed."""
    # A PowerShell 7 parent hands down a PSModulePath that Windows PowerShell 5.1 cannot load
    # Microsoft.PowerShell.Security from, so the probe would fail for want of its own module.
    env = {k: v for k, v in os.environ.items() if k.upper() != "PSMODULEPATH"}
    try:
        done = subprocess.run(
            [POWERSHELL_EXE, "-NoProfile", "-NonInteractive", "-Command", "Get-ExecutionPolicy"],
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout,
            check=False,
            creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _log.warning("⚠️ execution policy not confirmed (%s)", exc)
        return None
    value = done.stdout.strip().lower()
    if done.returncode != 0 or not value:
        _log.warning("⚠️ execution policy not confirmed (exit %s: %s)", done.returncode, done.stderr.strip()[:200])
        return None
    return value


def script_command(
    script: Union[str, Path],
    args: Sequence[str] = (),
    *,
    policy: Optional[str],
) -> tuple[List[str], dict[str, str]]:
    """The argv, plus the extra environment, that starts ``script`` under ``policy``.

    ``policy`` is the effective policy from :func:`effective_policy`; ``None``
    (not confirmed) takes the ScriptBlock launch, which works under all of them.
    """
    base = [POWERSHELL_EXE, "-NoProfile", "-NonInteractive"]
    if policy in FILE_LAUNCH_POLICIES:
        return base + ["-File", str(script), *args], {}
    return (
        base + ["-Command", BOOTSTRAP],
        {ENV_SCRIPT: str(script), ENV_ARGV: "".join(_ARG_SEP + a for a in args)},
    )


def run_script(
    script: Union[str, Path],
    args: Sequence[str] = (),
    *,
    timeout: Optional[float],
    env: Optional[Mapping[str, str]] = None,
    policy: Optional[str] = None,
) -> int:
    """Run ``script`` to completion and return its exit code.

    ``policy`` defaults to a live probe; a caller that already knows it passes it.
    """
    resolved = policy if policy is not None else effective_policy()
    argv, extra = script_command(script, args, policy=resolved)
    if not extra:
        _log.info("ℹ️ execution policy %s: starting %s with -File", resolved, Path(script).name)
    elif resolved is None:
        _log.warning("⚠️ execution policy not confirmed: loading %s as a ScriptBlock", Path(script).name)
    else:
        _log.info("ℹ️ execution policy %s: loading %s as a ScriptBlock (policy unchanged)", resolved, Path(script).name)
    done = subprocess.run(
        argv,
        env={**(os.environ if env is None else env), **extra},
        stdout=_inherit(sys.stdout),
        stderr=_inherit(sys.stderr),
        timeout=timeout,
        creationflags=NO_WINDOW,
        check=False,
    )
    return done.returncode


def main(argv: Optional[List[str]] = None) -> int:
    """``python -m src.launching.ps_script <script> [args...]`` -- args pass through verbatim."""
    items = list(sys.argv[1:] if argv is None else argv)
    if not items:
        print("usage: python -m src.launching.ps_script <script.ps1> [args...]", file=sys.stderr)
        return 2
    script, args = items[0], items[1:]
    if not Path(script).is_file():
        print(f"missing PowerShell script: {script}", file=sys.stderr)
        return 1
    sys.stderr.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    # No timeout and the caller's console: the script's lifetime and output are the caller's.
    return run_script(script, args, timeout=None)


if __name__ == "__main__":
    raise SystemExit(main())
