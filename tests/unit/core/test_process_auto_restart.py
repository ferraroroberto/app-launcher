"""The night auto-restart: window, once a night, never on unknown, only the allowlist.

Every clock is a passed-in datetime and every process a fake; the hub is a lambda.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from src import process_auto_restart as par
from src import process_restart as pr
from src import restart_window as rw


def _cfg(**kw):
    base = dict(process_auto_restart=True, process_auto_restart_start="21:00",
                process_auto_restart_end="05:00", llm_hub_url="http://hub")
    return SimpleNamespace(**{**base, **kw})


def at(day, hhmm):
    hour, minute = map(int, hhmm.split(":"))
    return datetime(2026, 11, day, hour, minute)


class Rig:
    """The hub, the allowlist lookup and the restart, as spies."""

    def __init__(self, monkeypatch, tmp_path, verdict="watch", restart_id="wavelink", relaunch="launched"):
        monkeypatch.setattr(par, "state_path", lambda: tmp_path / "process_auto_restart.json")
        monkeypatch.setattr(par, "_last_skip", None)
        monkeypatch.setattr(par, "_tonight", {})
        self.verdict, self.restart_id, self.relaunch = verdict, restart_id, relaunch
        self.hub_reads, self.restarted = 0, []
        self.refuse = False

    def read_health(self, url):
        self.hub_reads += 1
        return {"verdict": self.verdict, "reason": "r", "fix": None}

    def annotate(self, health):
        # Offers the id whatever the verdict, so the tick's own verdict check is what is tested.
        return dict(health, restart_id=self.restart_id)

    def restart(self, restart_id):
        if self.refuse:
            raise pr.RestartError("not running", 409)
        self.restarted.append(restart_id)
        return {"stopped": ["WaveLink.exe"], "relaunch": self.relaunch}

    def tick(self, now, cfg=None):
        return par.tick_once(cfg or _cfg(), now, read_health=self.read_health,
                             annotate=self.annotate, restart=self.restart)


@pytest.fixture
def rig(monkeypatch, tmp_path):
    return Rig(monkeypatch, tmp_path)


def test_a_flagged_process_is_restarted_inside_the_window(rig):
    assert rig.tick(at(7, "22:00")) == "wavelink"
    assert rig.restarted == ["wavelink"]


def test_a_window_crossing_midnight_covers_the_small_hours(rig):
    assert rig.tick(at(8, "02:14")) == "wavelink"


@pytest.mark.parametrize("when", ["05:00", "12:00", "20:59"])
def test_outside_the_window_nothing_restarts_and_the_hub_is_not_read(rig, when):
    assert rig.tick(at(8, when)) is None
    assert rig.restarted == [] and rig.hub_reads == 0


def test_the_switch_off_never_restarts(rig):
    assert rig.tick(at(7, "22:00"), _cfg(process_auto_restart=False)) is None
    assert rig.restarted == [] and rig.hub_reads == 0


@pytest.mark.parametrize("verdict", ["unknown", "ok"])
def test_a_verdict_that_is_not_a_warning_never_restarts(rig, verdict):
    rig.verdict = verdict
    assert rig.tick(at(7, "22:00")) is None
    assert rig.restarted == []


def test_a_flagged_process_off_the_allowlist_is_never_restarted(rig):
    rig.restart_id = None
    assert rig.tick(at(7, "22:00")) is None
    assert rig.restarted == []


def test_the_real_annotation_offers_nothing_for_an_off_list_process(monkeypatch, tmp_path):
    Rig(monkeypatch, tmp_path)
    notepad = SimpleNamespace(pid=5, info={"name": "notepad.exe"}, name=lambda: "notepad.exe",
                              exe=lambda: r"C:\Windows\notepad.exe")
    monkeypatch.setattr(pr, "_live", lambda name: [notepad])

    def hub(url):
        return {"verdict": "act", "reason": "notepad.exe leaks", "fix": "Restart notepad.exe"}

    def never(restart_id):
        pytest.fail("restarted an off-list process")

    assert par.tick_once(_cfg(), at(7, "22:00"), read_health=hub, restart=never) is None


def test_once_per_process_per_night_across_midnight_and_across_a_restart(rig):
    assert rig.tick(at(7, "21:30")) == "wavelink"
    assert rig.tick(at(7, "23:59")) is None
    assert rig.tick(at(8, "02:14")) is None  # the same night, after midnight
    assert rig.restarted == ["wavelink"]
    assert rig.tick(at(8, "21:30")) == "wavelink"  # the next night
    assert rig.restarted == ["wavelink", "wavelink"]


def test_a_state_file_that_cannot_be_written_still_limits_it_to_once_a_night(rig, monkeypatch, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setattr(par, "state_path", lambda: blocker / "process_auto_restart.json")  # parent is a file
    assert rig.tick(at(7, "22:00")) == "wavelink"
    assert rig.tick(at(7, "22:01")) is None
    assert rig.restarted == ["wavelink"]


def test_a_refused_restart_is_not_recorded_and_tries_again(rig):
    rig.refuse = True
    assert rig.tick(at(7, "22:00")) is None
    rig.refuse = False
    assert rig.tick(at(7, "22:05")) == "wavelink"


def test_the_sheet_line_names_the_time_and_survives_a_webapp_restart(rig):
    rig.tick(at(8, "02:14"))
    assert par.last_auto_restart(at(8, "09:00")) == {
        "owner": "Elgato Wave Link", "at": "02:14", "outcome": "launched"}
    assert par.last_auto_restart(at(8, "02:14") + timedelta(hours=25)) is None


def test_a_process_that_did_not_come_back_is_recorded_as_such(rig):
    rig.relaunch = "not confirmed"
    rig.tick(at(8, "02:14"))
    assert par.last_auto_restart(at(8, "03:00"))["outcome"] == "not confirmed"


def test_skips_log_once_per_reason(rig, caplog):
    caplog.set_level("INFO", logger="launcher.slowreq")
    for minute in ("10:00", "10:01", "10:02"):
        rig.tick(at(7, minute))
    lines = [r.getMessage() for r in caplog.records]
    assert lines.count("🔕 night auto-restart skip: outside the 21:00-05:00 window") == 1


# --- the window itself -----------------------------------------------------------------

@pytest.mark.parametrize("bad", [("9:00", "17:00"), ("25:00", "05:00"), ("21:00", ""),
                                 ("21:00", "21:00"), (None, "05:00")])
def test_a_malformed_or_empty_window_is_refused(bad):
    with pytest.raises(ValueError):
        rw.validate_window(*bad)


def test_a_same_day_window_and_its_night():
    assert rw.in_window(at(7, "13:00"), "13:00", "15:00")
    assert not rw.in_window(at(7, "15:00"), "13:00", "15:00")
    assert rw.night_of(at(7, "14:00"), "13:00", "15:00").day == 7


def test_after_midnight_belongs_to_the_evening_before():
    assert rw.night_of(at(8, "02:00"), "21:00", "05:00").day == 7
    assert rw.night_of(at(7, "22:00"), "21:00", "05:00").day == 7
