"""The PC-health row's owner name and its allowlisted Restart.

Every process here is a fake: nothing real is ever stopped. What matters is that
only an exact name *and* folder match is ever touched, that the refusals each say
their own thing, and that the health payload only offers a restart for a target.
"""
from __future__ import annotations

import psutil
import pytest

from src import process_restart as pr

PLUGIN_DIR = r"C:\Users\x\AppData\Roaming\Elgato\StreamDeck\Plugins\com.exension.hwinfo.sdPlugin"
PLUGIN = PLUGIN_DIR + r"\hwinfo-plugin.exe"
HOST = PLUGIN_DIR + r"\hwinfo.exe"
REAL_HWINFO = r"C:\Program Files\HWiNFO64\HWiNFO64.EXE"


class FakeProc:
    def __init__(self, pid, name, exe, parent=None, stuck=False):
        self.pid, self._name, self._exe, self._parent = pid, name, exe, parent
        self.stuck, self.stopped, self.killed = stuck, False, False
        self.info = {"name": name}

    def name(self): return self._name
    def exe(self): return self._exe
    def parent(self): return self._parent
    def terminate(self): self.stopped = True

    def kill(self): self.killed = True

    def wait(self, timeout=None):
        if self.stuck and not self.killed:
            raise psutil.TimeoutExpired(timeout)


def _table(monkeypatch, *procs):
    monkeypatch.setattr(pr, "_live", lambda name: [p for p in procs if p.name().lower() == name.lower()])


def test_restart_stops_the_plugin_and_its_host_parent(monkeypatch):
    host = FakeProc(1, "hwinfo.exe", HOST)
    plugin = FakeProc(2, "hwinfo-plugin.exe", PLUGIN, parent=host)
    _table(monkeypatch, plugin, host)
    got = pr.restart("streamdeck-hwinfo")
    assert plugin.stopped and host.stopped
    assert sorted(got["stopped"]) == ["hwinfo-plugin.exe", "hwinfo.exe"]


def test_restart_leaves_the_real_hwinfo_alone(monkeypatch):
    real = FakeProc(9, "HWiNFO64.EXE", REAL_HWINFO)
    host = FakeProc(1, "hwinfo.exe", HOST)
    plugin = FakeProc(2, "hwinfo-plugin.exe", PLUGIN, parent=host)
    _table(monkeypatch, plugin, host, real)
    pr.restart("streamdeck-hwinfo")
    assert not real.stopped


def test_a_same_named_process_from_another_folder_is_not_touched(monkeypatch):
    impostor = FakeProc(3, "hwinfo-plugin.exe", r"C:\Temp\hwinfo-plugin.exe")
    _table(monkeypatch, impostor)
    with pytest.raises(pr.RestartError) as err:
        pr.restart("streamdeck-hwinfo")
    assert err.value.status == 409
    assert not impostor.stopped


def test_a_parent_that_is_not_the_plugins_host_survives(monkeypatch):
    explorer = FakeProc(4, "explorer.exe", r"C:\Windows\explorer.exe")
    plugin = FakeProc(2, "hwinfo-plugin.exe", PLUGIN, parent=explorer)
    _table(monkeypatch, plugin, explorer)
    pr.restart("streamdeck-hwinfo")
    assert plugin.stopped and not explorer.stopped


def test_an_id_off_the_allowlist_is_refused_with_its_own_message(monkeypatch):
    victim = FakeProc(5, "notepad.exe", r"C:\Windows\notepad.exe")
    _table(monkeypatch, victim)
    with pytest.raises(pr.RestartError) as err:
        pr.restart("notepad.exe")
    assert err.value.status == 404
    assert "not on the restart allowlist" in str(err.value)
    assert not victim.stopped


def test_a_target_that_is_not_running_says_so_not_refused(monkeypatch):
    _table(monkeypatch)
    with pytest.raises(pr.RestartError) as err:
        pr.restart("streamdeck-hwinfo")
    assert err.value.status == 409
    assert "not running" in str(err.value)


def test_a_process_that_will_not_exit_is_killed_after_the_wait(monkeypatch):
    plugin = FakeProc(2, "hwinfo-plugin.exe", PLUGIN, stuck=True)
    _table(monkeypatch, plugin)
    pr.restart("streamdeck-hwinfo")
    assert plugin.killed


def _health(**kw):
    return {"verdict": "watch", "why": None, "reason": "hwinfo-plugin.exe at 349 k handles",
            "fix": "Restart hwinfo-plugin.exe", **kw}


def test_annotate_names_the_owner_and_offers_restart_for_a_target(monkeypatch):
    _table(monkeypatch, FakeProc(2, "hwinfo-plugin.exe", PLUGIN))
    got = pr.annotate(_health())
    assert got["owner"] == "Stream Deck · HWiNFO plugin"
    assert got["restart_id"] == "streamdeck-hwinfo"
    assert got["fix"] == "Restart hwinfo-plugin.exe"


def test_annotate_names_an_owner_but_no_restart_off_the_allowlist(monkeypatch):
    _table(monkeypatch, FakeProc(7, "HWiNFO64.EXE", REAL_HWINFO))
    got = pr.annotate(_health(fix="Restart HWiNFO64.EXE", reason="x"))
    assert got["owner"] == "HWiNFO64"
    assert got["restart_id"] is None


def test_annotate_offers_nothing_for_a_name_in_the_wrong_folder(monkeypatch):
    _table(monkeypatch, FakeProc(3, "hwinfo-plugin.exe", r"C:\Temp\hwinfo-plugin.exe"))
    assert pr.annotate(_health())["restart_id"] is None


@pytest.mark.parametrize("verdict", ["ok", "unknown"])
def test_annotate_adds_nothing_for_a_verdict_that_is_not_a_warning(monkeypatch, verdict):
    _table(monkeypatch, FakeProc(2, "hwinfo-plugin.exe", PLUGIN))
    got = pr.annotate(_health(verdict=verdict))
    assert got["owner"] is None and got["restart_id"] is None


def test_annotate_reads_the_process_from_the_reason_when_there_is_no_fix(monkeypatch):
    _table(monkeypatch, FakeProc(2, "hwinfo-plugin.exe", PLUGIN))
    got = pr.annotate(_health(fix=None))
    assert got["restart_id"] == "streamdeck-hwinfo"


def test_annotate_with_no_process_named_is_left_plain(monkeypatch):
    _table(monkeypatch)
    got = pr.annotate({"verdict": "watch", "reason": "memory is high", "fix": None})
    assert got["owner"] is None and got["restart_id"] is None


# --- Wave Link: no supervisor, so the restart starts it again ---------------------------

WAVELINK = r"C:\Program Files\Elgato\WaveLink\WaveLink.exe"


class Table:
    """A mutable process table: ``_live`` reads it, a launch edits it."""

    def __init__(self, monkeypatch, *procs):
        self.procs = list(procs)
        self.launched = []
        self.launched_args = []
        self.starts_as = None  # a launch brings this process into the table
        monkeypatch.setattr(pr, "_live", lambda name: [p for p in self.procs if p.name().lower() == name.lower()])
        monkeypatch.setattr(pr, "_wait_for", lambda ready, seconds: ready())
        monkeypatch.setattr(pr, "_launch", self._launch)

    def _launch(self, exe, args=()):
        self.launched.append(exe)
        self.launched_args.append(args)
        if self.starts_as is not None:
            self.procs.append(self.starts_as)


def _wavelink(pid=10, exe=WAVELINK):
    return FakeProc(pid, "WaveLink.exe", exe)


def test_wavelink_is_stopped_and_started_again_from_its_own_path(monkeypatch):
    old = _wavelink()
    table = Table(monkeypatch, old)
    table.starts_as = _wavelink(pid=11)
    got = pr.restart("wavelink")
    assert old.stopped
    assert table.launched == [WAVELINK]
    assert got["relaunch"] == "launched"
    assert got["owner"] == "Elgato Wave Link"


def test_wavelink_a_supervisor_that_brings_it_back_is_reported_and_not_raced(monkeypatch):
    old = _wavelink()
    table = Table(monkeypatch, old)
    old.terminate = lambda: table.procs.append(_wavelink(pid=12))
    got = pr.restart("wavelink")
    assert got["relaunch"] == "supervisor"
    assert table.launched == []


def test_wavelink_that_never_returns_is_not_confirmed_never_ok(monkeypatch):
    table = Table(monkeypatch, _wavelink())
    got = pr.restart("wavelink")
    assert table.launched == [WAVELINK]
    assert got["relaunch"] == "not confirmed"


def test_wavelink_that_cannot_be_started_is_not_confirmed(monkeypatch):
    Table(monkeypatch, _wavelink())

    def boom(exe, args=()):
        raise OSError("blocked")

    monkeypatch.setattr(pr, "_launch", boom)
    assert pr.restart("wavelink")["relaunch"] == "not confirmed"


def test_the_relaunch_path_comes_from_the_process_not_the_caller(monkeypatch):
    other = r"D:\Elgato\WaveLink\WaveLink.exe"
    table = Table(monkeypatch, _wavelink(exe=other))
    table.starts_as = _wavelink(pid=11, exe=other)
    pr.restart("wavelink")
    assert table.launched == [other]


def test_a_wavelink_named_process_from_another_folder_is_not_touched(monkeypatch):
    impostor = _wavelink(exe=r"C:\Temp\WaveLink.exe")
    table = Table(monkeypatch, impostor)
    with pytest.raises(pr.RestartError) as err:
        pr.restart("wavelink")
    assert err.value.status == 409 and not impostor.stopped and table.launched == []


def test_a_supervised_target_is_never_launched_by_the_restart(monkeypatch):
    table = Table(monkeypatch, FakeProc(2, "hwinfo-plugin.exe", PLUGIN))
    got = pr.restart("streamdeck-hwinfo")
    assert got["relaunch"] is None and table.launched == []


def test_wavelink_comes_back_in_the_background_with_runinbk(monkeypatch):
    table = Table(monkeypatch, _wavelink())
    table.starts_as = _wavelink(pid=11)
    pr.restart("wavelink")
    assert table.launched_args == [("-runinbk",)]


def test_the_relaunch_flag_is_fixed_in_code_whatever_the_stopped_process_ran_with(monkeypatch):
    table = Table(monkeypatch, _wavelink())
    table.procs[0].cmdline = lambda: [WAVELINK, "--evil"]
    pr.restart("wavelink")
    assert table.launched_args == [("-runinbk",)]


def test_a_supervised_target_has_no_relaunch_flag():
    assert pr.get_target("streamdeck-hwinfo").relaunch_args == ()


def test_launch_hands_the_path_and_its_arguments_to_windows_start(monkeypatch):
    calls = []
    monkeypatch.setattr(pr.subprocess, "Popen", lambda *a, **kw: calls.append((a, kw)))
    pr._launch(WAVELINK, ("-runinbk",))
    (args, kw), = calls
    cmd = args[0]
    assert cmd[0].lower().endswith("cmd.exe") and cmd[1:] == ["/c", "start", "", WAVELINK, "-runinbk"]
    assert kw["cwd"] == r"C:\Program Files\Elgato\WaveLink"
    assert kw["creationflags"] == pr.NO_WINDOW


def test_annotate_offers_restart_for_a_wavelink_warning(monkeypatch):
    _table(monkeypatch, FakeProc(10, "WaveLink.exe", WAVELINK))
    got = pr.annotate(_health(fix="Restart WaveLink.exe", reason="WaveLink.exe at 340 k handles"))
    assert got["owner"] == "Elgato Wave Link" and got["restart_id"] == "wavelink"
