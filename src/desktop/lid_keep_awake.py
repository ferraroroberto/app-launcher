"""Keep the PC awake with the lid closed, as an opt-in, reversible switch.

The machine-level setting is the active power plan's **lid close action**
(``SUB_BUTTONS`` / ``LIDACTION``), one value for AC and one for battery. This
switch sets both to ``0`` ("Do nothing") and puts the exact prior values back
when turned off. Nothing here runs unless the user flips the switch.

**State is read live, four-valued.** :func:`read_values` asks ``powercfg`` every
time, so the switch never claims a position the machine does not hold:
``on`` (both values 0), ``off``, ``unsupported`` (``powercfg`` ran but the plan
has no lid setting, as on a desktop) or ``unknown`` (``powercfg`` could not be
run or read), which the page shows as unknown, never off.

**Restore is exact.** Before the first change to a plan, its AC and battery
values are written to :data:`STATE_FILE` keyed by the plan's GUID; turning off
writes them back to that plan, then clears the entry. A plan that has since
been deleted has nothing left to restore and is dropped.

``powercfg`` is spawned through an injectable ``runner`` so the unit tests
never change a real power plan.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, NamedTuple, Optional, Sequence

from src import runtime_files
from src._json_io import atomic_write_json
from src.subprocess_flags import NO_WINDOW

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Module-level so tests redirect it at a tmp_path (tests/conftest.py).
STATE_FILE = PROJECT_ROOT / "webapp" / "lid-keep-awake.json"

# Set only by the e2e autoboot: the disposable webapp then reports "no lid"
# without spawning powercfg, so its Settings screenshots do not depend on
# whether the machine running the gate is a laptop.
ABSENT_ENV = "LAUNCHER_LID_KEEP_AWAKE_ABSENT"

# powercfg answers in tens of milliseconds; 10 s is room for a loaded box
# without letting a wedged call stall a Settings load.
POWERCFG_TIMEOUT_S = 10

LID_DO_NOTHING = 0

LidState = Literal["on", "off", "unknown", "unsupported"]

_GUID_RE = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
_INDEX_RE = re.compile(r"\b0x([0-9a-fA-F]+)\b")

# Serialises the read-modify-write of the state file and the powercfg steps of
# one change, so two taps can never interleave their saved values.
_lock = threading.Lock()


class LidKeepAwakeError(OSError):
    """A change could not be made, or could not be confirmed. The message is
    shown on the page, so it names no path or exception class."""


class Unsupported(LidKeepAwakeError):
    """The active power plan has no lid close action (a PC with no lid)."""


class Reading(NamedTuple):
    scheme: str
    ac: int
    dc: int

    @property
    def keeps_awake(self) -> bool:
        return self.ac == LID_DO_NOTHING and self.dc == LID_DO_NOTHING


Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


def _run_powercfg(args: Sequence[str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        ["powercfg", *args],
        capture_output=True,
        text=True,
        # powercfg prints in the console's OEM code page; only hex and GUIDs
        # are read from it, so a replaced character costs nothing.
        encoding="oem" if sys.platform == "win32" else "utf-8",
        errors="replace",
        creationflags=NO_WINDOW,
        check=False,
        timeout=POWERCFG_TIMEOUT_S,
    )


def _powercfg(args: Sequence[str], runner: Optional[Runner]) -> str:
    """The stdout of one powercfg call; :class:`LidKeepAwakeError` when it
    cannot be run or exits non-zero."""
    try:
        done = (runner or _run_powercfg)(args)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("⚠️ lid keep-awake: powercfg %s did not run: %s", args[0], exc.__class__.__name__)
        raise LidKeepAwakeError("Windows power settings could not be run.") from exc
    if done.returncode != 0:
        logger.warning(
            "⚠️ lid keep-awake: powercfg %s exited %s: %s",
            args[0], done.returncode, (done.stderr or done.stdout or "").strip()[:200],
        )
        raise LidKeepAwakeError("Windows power settings refused the request.")
    return done.stdout or ""


def read_values(runner: Optional[Runner] = None) -> Reading:
    """The active plan's lid close action. Raises :class:`Unsupported` when the
    plan has none, :class:`LidKeepAwakeError` when it could not be read."""
    if os.environ.get(ABSENT_ENV):
        raise Unsupported("This PC has no lid close setting.")
    out = _powercfg(["/q", "SCHEME_CURRENT", "SUB_BUTTONS", "LIDACTION"], runner)
    # The labels are localised; the order is not: the plan GUID comes first and
    # the AC then the battery index are the last two 0x values.
    indexes = _INDEX_RE.findall(out)
    guid = _GUID_RE.search(out)
    if not indexes:
        raise Unsupported("This PC has no lid close setting.")
    if len(indexes) < 2 or guid is None:
        logger.warning("⚠️ lid keep-awake: unexpected powercfg output (%d index lines)", len(indexes))
        raise LidKeepAwakeError("The lid close setting could not be read.")
    return Reading(guid.group(0).lower(), int(indexes[-2], 16), int(indexes[-1], 16))


def payload(runner: Optional[Runner] = None) -> Dict[str, Any]:
    """The ``/api/config`` block: the live state and, when it is not on or off,
    the reason, so the page can say why instead of guessing."""
    state: LidState
    try:
        reading = read_values(runner)
    except Unsupported as exc:
        return {"state": "unsupported", "reason": str(exc)}
    except LidKeepAwakeError as exc:
        return {"state": "unknown", "reason": str(exc)}
    state = "on" if reading.keeps_awake else "off"
    return {"state": state, "reason": None}


def _load_saved() -> Dict[str, Dict[str, int]]:
    try:
        raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        # An unreadable file must not be mistaken for "nothing saved": the next
        # turn-on would overwrite the only copy of the prior values.
        logger.warning("⚠️ lid keep-awake: saved values unreadable: %s", exc.__class__.__name__)
        raise LidKeepAwakeError("The saved power plan values could not be read.") from exc
    if not isinstance(raw, dict):
        raise LidKeepAwakeError("The saved power plan values could not be read.")
    saved: Dict[str, Dict[str, int]] = {}
    for guid, pair in raw.items():
        if (
            isinstance(guid, str) and isinstance(pair, dict)
            and all(isinstance(pair.get(k), int) and not isinstance(pair.get(k), bool) for k in ("ac", "dc"))
        ):
            saved[guid] = {"ac": pair["ac"], "dc": pair["dc"]}
    return saved


def _store_saved(saved: Dict[str, Dict[str, int]]) -> None:
    try:
        if saved:
            runtime_files.make_dirs(STATE_FILE.parent)
            atomic_write_json(STATE_FILE, saved)
        else:
            STATE_FILE.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("⚠️ lid keep-awake: saved values not written: %s", exc.__class__.__name__)
        raise LidKeepAwakeError("The prior power plan values could not be saved.") from exc


def _set_values(scheme: str, ac: int, dc: int, runner: Optional[Runner]) -> None:
    _powercfg(["/setacvalueindex", scheme, "SUB_BUTTONS", "LIDACTION", str(ac)], runner)
    _powercfg(["/setdcvalueindex", scheme, "SUB_BUTTONS", "LIDACTION", str(dc)], runner)


def _scheme_exists(scheme: str, runner: Optional[Runner]) -> bool:
    """False only when ``/list`` ran and the plan is not in it; an unreadable
    list counts as present, so a restore is never silently dropped."""
    try:
        listed: List[str] = [g.lower() for g in _GUID_RE.findall(_powercfg(["/list"], runner))]
    except LidKeepAwakeError:
        return True
    return scheme in listed


def enable(runner: Optional[Runner] = None) -> None:
    """Set the active plan's lid close action to Do nothing on AC and battery,
    saving the prior values first. Rolls back and raises when a step fails."""
    with _lock:
        before = read_values(runner)
        if before.keeps_awake:
            return
        saved = _load_saved()
        # An entry already on file is the original: a half-finished earlier
        # change must not replace it with its own half-applied values.
        saved.setdefault(before.scheme, {"ac": before.ac, "dc": before.dc})
        _store_saved(saved)
        try:
            _set_values(before.scheme, LID_DO_NOTHING, LID_DO_NOTHING, runner)
            _powercfg(["/setactive", "SCHEME_CURRENT"], runner)
            after = read_values(runner)
        except LidKeepAwakeError:
            _roll_back(before, saved, runner)
            raise
        if not after.keeps_awake:
            _roll_back(before, saved, runner)
            raise LidKeepAwakeError("Windows did not keep the lid close setting.")
        logger.info("ℹ️ lid keep-awake: on for plan %s (was AC %s, battery %s)", before.scheme, before.ac, before.dc)


def _roll_back(before: Reading, saved: Dict[str, Dict[str, int]], runner: Optional[Runner]) -> None:
    """Put the plan back after a failed turn-on. The saved entry stays when
    this fails too, so the switch's off position can still restore it."""
    try:
        _set_values(before.scheme, before.ac, before.dc, runner)
        _powercfg(["/setactive", "SCHEME_CURRENT"], runner)
    except LidKeepAwakeError:
        logger.warning("⚠️ lid keep-awake: roll back of plan %s failed; saved values kept", before.scheme)
        return
    saved.pop(before.scheme, None)
    try:
        _store_saved(saved)
    except LidKeepAwakeError:
        pass


def disable(runner: Optional[Runner] = None) -> None:
    """Restore every saved plan's AC and battery values, then clear them.

    With nothing saved there is nothing to restore: a plan already off is a
    no-op, and one that is on (set by hand, not by this switch) is an error
    that says so, never a guessed value."""
    with _lock:
        saved = _load_saved()
        if not saved:
            if read_values(runner).keeps_awake:
                raise LidKeepAwakeError(
                    "No prior values are saved. Change the lid close action in Windows Power Options."
                )
            return
        for scheme in list(saved):
            pair = saved[scheme]
            try:
                _set_values(scheme, pair["ac"], pair["dc"], runner)
            except LidKeepAwakeError:
                if _scheme_exists(scheme, runner):
                    raise
                logger.info("ℹ️ lid keep-awake: plan %s no longer exists; dropping its saved values", scheme)
            del saved[scheme]
            _store_saved(saved)
        _powercfg(["/setactive", "SCHEME_CURRENT"], runner)
        if read_values(runner).keeps_awake:
            raise LidKeepAwakeError(
                "The active plan still ignores the lid. It was not set by this switch; "
                "change it in Windows Power Options."
            )
        logger.info("ℹ️ lid keep-awake: off, prior values restored")
