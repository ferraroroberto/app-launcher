"""Tests for src.lid_keep_awake with powercfg replaced by a fake runner: no
test reads or changes a real power plan."""

from __future__ import annotations

import json
import subprocess
from typing import Dict, List, Optional, Sequence

import pytest

import src.desktop.lid_keep_awake as lid

PLAN_A = "381b4222-f694-41f0-9685-ff5bb260df2e"
PLAN_B = "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"

# The autouse fixture replaces lid._run_powercfg for every test; keep the real one.
REAL_RUN_POWERCFG = lid._run_powercfg


class FakePowercfg:
    """The few powercfg verbs the switch uses, over an in-memory plan table."""

    def __init__(self, plans: Optional[Dict[str, List[int]]] = None, active: str = PLAN_A) -> None:
        self.plans = plans if plans is not None else {PLAN_A: [1, 1]}
        self.active = active
        self.calls: List[List[str]] = []
        self.has_lid = True
        self.fail_on: Optional[str] = None
        self.fail_times = 1 << 30
        self.ignore_sets = False

    def __call__(self, args: Sequence[str]) -> "subprocess.CompletedProcess[str]":
        args = list(args)
        self.calls.append(args)
        verb = args[0]
        if self.fail_on == verb and self.fail_times > 0:
            self.fail_times -= 1
            return self._done(1, err="Invalid Parameters")
        if verb == "/q":
            if not self.has_lid:
                return self._done(0, out=f"Power Scheme GUID: {self.active}  (High performance)\n  GUID Alias: SCHEME_MIN\n")
            ac, dc = self.plans[self.active]
            return self._done(0, out=(
                f"Power Scheme GUID: {self.active}  (Balanced)\n"
                "  Subgroup GUID: 4f971e89-eebd-4455-a8de-9e59040e7347  (Power buttons and lid)\n"
                "    Power Setting GUID: 5ca83367-6e45-459f-a27b-476b1d01c936  (Lid close action)\n"
                "      Possible Setting Index: 000\n      Possible Setting Friendly Name: Do nothing\n"
                "      Possible Setting Index: 001\n      Possible Setting Friendly Name: Sleep\n"
                f"    Current AC Power Setting Index: 0x{ac:08x}\n"
                f"    Current DC Power Setting Index: 0x{dc:08x}\n"
            ))
        if verb in ("/setacvalueindex", "/setdcvalueindex"):
            scheme = self.active if args[1] == "SCHEME_CURRENT" else args[1]
            if scheme not in self.plans:
                return self._done(1, err="The power scheme does not exist")
            if not self.ignore_sets:
                self.plans[scheme][0 if verb == "/setacvalueindex" else 1] = int(args[-1])
            return self._done(0)
        if verb == "/setactive":
            return self._done(0)
        if verb == "/list":
            body = "".join(f"Power Scheme GUID: {g}  (plan)\n" for g in self.plans)
            return self._done(0, out=body)
        raise AssertionError(f"unexpected powercfg {args}")

    @staticmethod
    def _done(code: int, out: str = "", err: str = "") -> "subprocess.CompletedProcess[str]":
        return subprocess.CompletedProcess([], code, stdout=out, stderr=err)

    def writes(self) -> List[List[str]]:
        return [c for c in self.calls if c[0].startswith("/set")]


@pytest.fixture
def fake() -> FakePowercfg:
    return FakePowercfg()


def saved_file() -> Dict[str, Dict[str, int]]:
    return json.loads(lid.STATE_FILE.read_text(encoding="utf-8"))


def test_reads_each_state_live(fake):
    assert lid.payload(fake) == {"state": "off", "reason": None}
    fake.plans[PLAN_A] = [0, 0]
    assert lid.payload(fake)["state"] == "on"
    fake.plans[PLAN_A] = [0, 1]
    assert lid.payload(fake)["state"] == "off", "one value still sleeping is not on"


def test_a_plan_with_no_lid_is_unsupported_not_off(fake):
    fake.has_lid = False
    block = lid.payload(fake)
    assert block["state"] == "unsupported"
    assert "no lid" in block["reason"]


def test_an_unreadable_state_is_unknown_never_off(fake):
    fake.fail_on = "/q"
    assert lid.payload(fake)["state"] == "unknown"

    def missing(args):
        raise FileNotFoundError("powercfg")

    block = lid.payload(missing)
    assert block["state"] == "unknown"
    assert "FileNotFound" not in block["reason"]

    def wedged(args):
        raise subprocess.TimeoutExpired("powercfg", 10)

    assert lid.payload(wedged)["state"] == "unknown"


def test_malformed_output_is_unknown(fake):
    def odd(args):
        return subprocess.CompletedProcess(args, 0, stdout="Current AC Power Setting Index: 0x00000001\n", stderr="")

    assert lid.payload(odd)["state"] == "unknown"


def test_the_e2e_seam_reports_no_lid_without_spawning(monkeypatch, fake):
    monkeypatch.setenv(lid.ABSENT_ENV, "1")
    assert lid.payload(fake)["state"] == "unsupported"
    assert fake.calls == []


def test_enable_saves_the_prior_values_then_sets_both_to_zero(fake):
    fake.plans[PLAN_A] = [1, 2]
    lid.enable(fake)
    assert fake.plans[PLAN_A] == [0, 0]
    assert saved_file() == {PLAN_A: {"ac": 1, "dc": 2}}
    assert fake.writes() == [
        ["/setacvalueindex", PLAN_A, "SUB_BUTTONS", "LIDACTION", "0"],
        ["/setdcvalueindex", PLAN_A, "SUB_BUTTONS", "LIDACTION", "0"],
        ["/setactive", "SCHEME_CURRENT"],
    ]
    assert lid.payload(fake)["state"] == "on"


def test_disable_restores_the_exact_prior_values_and_clears_them(fake):
    fake.plans[PLAN_A] = [1, 2]
    lid.enable(fake)
    lid.disable(fake)
    assert fake.plans[PLAN_A] == [1, 2]
    assert not lid.STATE_FILE.exists()
    assert lid.payload(fake)["state"] == "off"


def test_enable_twice_keeps_the_original_values(fake):
    fake.plans[PLAN_A] = [1, 2]
    lid.enable(fake)
    before = len(fake.calls)
    lid.enable(fake)
    assert fake.writes()[-1] == ["/setactive", "SCHEME_CURRENT"]
    assert len(fake.calls) == before + 1, "already on: one read, no writes"
    assert saved_file() == {PLAN_A: {"ac": 1, "dc": 2}}


def test_disable_when_off_and_nothing_saved_changes_nothing(fake):
    lid.disable(fake)
    assert fake.writes() == []


def test_disable_with_nothing_saved_on_a_plan_set_by_hand_is_an_error(fake):
    fake.plans[PLAN_A] = [0, 0]
    with pytest.raises(lid.LidKeepAwakeError, match="No prior values are saved"):
        lid.disable(fake)
    assert fake.writes() == []
    assert fake.plans[PLAN_A] == [0, 0], "never guesses a value to put back"


def test_a_failed_step_rolls_back_and_raises(fake):
    fake.plans[PLAN_A] = [1, 2]
    fake.fail_on, fake.fail_times = "/setdcvalueindex", 1
    with pytest.raises(lid.LidKeepAwakeError):
        lid.enable(fake)
    assert fake.plans[PLAN_A] == [1, 2], "the AC value written before the failure is put back"
    assert not lid.STATE_FILE.exists()


def test_a_change_windows_did_not_keep_is_an_error_not_a_silent_on(fake):
    fake.ignore_sets = True
    with pytest.raises(lid.LidKeepAwakeError, match="did not keep"):
        lid.enable(fake)
    assert lid.payload(fake)["state"] == "off"


def test_a_failed_rollback_keeps_the_saved_values_for_the_off_switch(fake):
    fake.plans[PLAN_A] = [1, 2]
    fake.fail_on = "/setactive"
    with pytest.raises(lid.LidKeepAwakeError):
        lid.enable(fake)
    assert saved_file() == {PLAN_A: {"ac": 1, "dc": 2}}
    fake.fail_on = None
    lid.disable(fake)
    assert fake.plans[PLAN_A] == [1, 2]
    assert not lid.STATE_FILE.exists()


def test_restore_goes_to_the_plan_that_was_changed_even_after_a_plan_switch(fake):
    fake.plans = {PLAN_A: [1, 2], PLAN_B: [1, 1]}
    lid.enable(fake)
    fake.active = PLAN_B
    lid.disable(fake)
    assert fake.plans[PLAN_A] == [1, 2]
    assert fake.plans[PLAN_B] == [1, 1], "the plan the user switched to is untouched"


def test_a_plan_deleted_since_is_dropped_not_stuck(fake):
    fake.plans = {PLAN_A: [1, 2], PLAN_B: [1, 1]}
    lid.enable(fake)
    del fake.plans[PLAN_A]
    fake.active = PLAN_B
    lid.disable(fake)
    assert not lid.STATE_FILE.exists()


def test_a_restore_that_fails_on_a_live_plan_is_an_error_and_keeps_the_values(fake):
    fake.plans[PLAN_A] = [1, 2]
    lid.enable(fake)
    fake.fail_on = "/setacvalueindex"
    with pytest.raises(lid.LidKeepAwakeError):
        lid.disable(fake)
    assert saved_file() == {PLAN_A: {"ac": 1, "dc": 2}}


def test_off_that_leaves_the_plan_ignoring_the_lid_says_so(fake):
    fake.plans[PLAN_A] = [1, 2]
    lid.enable(fake)
    lid.STATE_FILE.write_text(json.dumps({PLAN_A: {"ac": 0, "dc": 0}}), encoding="utf-8")
    with pytest.raises(lid.LidKeepAwakeError, match="still ignores the lid"):
        lid.disable(fake)


def test_unreadable_saved_values_are_never_read_as_nothing_saved(fake):
    lid.STATE_FILE.write_text("{not json", encoding="utf-8")
    with pytest.raises(lid.LidKeepAwakeError, match="could not be read"):
        lid.enable(fake)
    assert fake.writes() == []
    assert lid.STATE_FILE.read_text(encoding="utf-8") == "{not json"


def test_enable_on_a_pc_with_no_lid_changes_nothing(fake):
    fake.has_lid = False
    with pytest.raises(lid.Unsupported):
        lid.enable(fake)
    assert fake.writes() == []
    assert not lid.STATE_FILE.exists()


def test_the_default_runner_spawns_without_a_window_and_with_a_timeout(monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"], seen["kwargs"] = cmd, kwargs
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(lid.subprocess, "run", fake_run)
    REAL_RUN_POWERCFG(["/q"])
    assert seen["cmd"] == ["powercfg", "/q"]
    assert seen["kwargs"]["creationflags"] == lid.NO_WINDOW
    assert seen["kwargs"]["timeout"] == lid.POWERCFG_TIMEOUT_S


def test_the_saved_values_file_defaults_to_the_repo_webapp_folder():
    """The autouse fixture redirects STATE_FILE, so load a fresh copy of the
    module to see the real default the Settings switch writes to."""
    import importlib.util
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[3]
    spec = importlib.util.spec_from_file_location("_lid_default", lid.__file__)
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)

    assert fresh.STATE_FILE == repo_root / "webapp" / "lid-keep-awake.json"
