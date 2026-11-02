"""Tests for src.remote_power_guard with Task Scheduler, the registry, the
elevation check and the Start-menu restart all faked: no test registers a
real task, writes a real registry value or touches Explorer."""

from __future__ import annotations

import base64
import re
from typing import Dict, Optional

import pytest

import src.remote_power_guard as guard

NOT_FOUND = -2147024894  # 0x80070002 as pywin32 reports it
ACCESS_DENIED = -2147024891  # 0x80070005


class FakeComError(Exception):
    """pywin32's shape: DISP_E_EXCEPTION with the real code in excepinfo[5]."""

    def __init__(self, scode: int) -> None:
        super().__init__("com_error")
        self.hresult = -2147352567
        self.excepinfo = (0, None, None, None, 0, scode)


class FakeTask:
    def __init__(self, xml: str, enabled: bool = True) -> None:
        self.xml = xml
        self.Enabled = enabled


class FakeFolder:
    def __init__(self, service: "FakeService", path: str) -> None:
        self.service = service
        self.path = path

    def _key(self, name: str) -> str:
        return (self.path.rstrip("\\") + "\\" + name) if self.path != "\\" else "\\" + name

    def GetTask(self, name: str) -> FakeTask:
        if self.service.read_error is not None:
            raise FakeComError(self.service.read_error)
        try:
            return self.service.tasks[self._key(name)]
        except KeyError:
            raise FakeComError(NOT_FOUND) from None

    def RegisterTask(self, name, xml, flags, user, password, logon_type, sddl) -> FakeTask:
        self.service.registered.append((self._key(name), flags, logon_type))
        task = FakeTask(xml)
        self.service.tasks[self._key(name)] = task
        return task

    def DeleteTask(self, name: str, flags: int) -> None:
        del self.service.tasks[self._key(name)]

    def CreateFolder(self, name: str) -> "FakeFolder":
        path = "\\" + name
        self.service.folders.add(path)
        return FakeFolder(self.service, path)


class FakeService:
    def __init__(self) -> None:
        self.tasks: Dict[str, FakeTask] = {}
        self.folders = {"\\"}
        self.registered: list = []
        self.read_error: Optional[int] = None

    def GetFolder(self, path: str) -> FakeFolder:
        if path not in self.folders:
            raise FakeComError(NOT_FOUND)
        return FakeFolder(self, path)


@pytest.fixture
def fakes(monkeypatch):
    service = FakeService()
    registry: Dict[str, int] = {}
    calls = {"restarts": 0, "elevated": True}

    def read_raw(key: str) -> int:
        return registry.get(key, 0)

    def write_raw(key: str, value: int) -> None:
        registry[key] = value

    def restart() -> None:
        calls["restarts"] += 1

    monkeypatch.setattr(guard.sys, "platform", "win32")
    monkeypatch.setattr(guard, "_connect", lambda: service)
    monkeypatch.setattr(guard, "_read_no_close_raw", read_raw)
    monkeypatch.setattr(guard, "_write_no_close_raw", write_raw)
    monkeypatch.setattr(guard, "_restart_start_menu", restart)
    monkeypatch.setattr(guard, "is_elevated", lambda: calls["elevated"])
    monkeypatch.delenv(guard.TASK_PATH_ENV, raising=False)
    monkeypatch.delenv(guard.POLICY_KEY_ENV, raising=False)
    monkeypatch.setenv("USERDOMAIN", "HOST")
    monkeypatch.setenv("USERNAME", "owner")
    return service, registry, calls


def test_absent_task_reads_off(fakes):
    assert guard.is_enabled() is False
    assert guard.state() == "off"


def test_unreadable_task_reads_unknown_never_off(fakes):
    service, _, _ = fakes
    service.folders.add("\\AppLauncher-Guard")
    service.read_error = ACCESS_DENIED
    assert guard.is_enabled() is None
    assert guard.state() == "unknown"


def test_scheduler_unavailable_reads_unknown(monkeypatch, fakes):
    def boom():
        raise OSError("Task Scheduler service is not running")

    monkeypatch.setattr(guard, "_connect", boom)
    assert guard.state() == "unknown"


def test_non_windows_reads_unknown(monkeypatch):
    monkeypatch.setattr(guard.sys, "platform", "linux")
    assert guard.is_enabled() is None
    assert guard.read_no_close() is None


def test_disabled_task_reads_off(fakes):
    service, _, _ = fakes
    service.folders.add("\\AppLauncher-Guard")
    service.tasks[guard.DEFAULT_TASK_PATH] = FakeTask("<Task/>", enabled=False)
    assert guard.state() == "off"


def test_enable_unelevated_raises_and_touches_nothing(fakes):
    service, registry, calls = fakes
    calls["elevated"] = False
    with pytest.raises(guard.ElevationRequired):
        guard.enable()
    assert service.registered == []
    assert service.folders == {"\\"}
    assert registry == {}


def test_disable_unelevated_raises_and_touches_nothing(fakes):
    service, registry, calls = fakes
    service.tasks[guard.DEFAULT_TASK_PATH] = FakeTask("<Task/>")
    registry[guard.DEFAULT_POLICY_KEY] = 1
    calls["elevated"] = False
    with pytest.raises(guard.ElevationRequired):
        guard.disable()
    assert guard.DEFAULT_TASK_PATH in service.tasks
    assert registry[guard.DEFAULT_POLICY_KEY] == 1
    assert calls["restarts"] == 0


def test_enable_creates_folder_registers_and_reads_back_on(fakes):
    service, _, _ = fakes
    guard.enable()
    assert "\\AppLauncher-Guard" in service.folders
    assert service.registered == [(guard.DEFAULT_TASK_PATH, 6, 3)]
    assert guard.state() == "on"


def test_enable_raises_when_read_back_misses(monkeypatch, fakes):
    monkeypatch.setattr(guard, "is_enabled", lambda: None)
    with pytest.raises(OSError, match="reading it back"):
        guard.enable()


def test_scheduler_refusal_surfaces_as_os_error(fakes):
    service, registry, calls = fakes
    service.folders.add("\\AppLauncher-Guard")
    service.tasks[guard.DEFAULT_TASK_PATH] = FakeTask("<Task/>")
    registry[guard.DEFAULT_POLICY_KEY] = 1
    service.read_error = ACCESS_DENIED
    with pytest.raises(OSError, match="refused to remove"):
        guard.disable()
    assert registry[guard.DEFAULT_POLICY_KEY] == 1
    assert calls["restarts"] == 0


def test_disable_removes_task_resets_no_close_and_restarts_start_menu(fakes):
    service, registry, calls = fakes
    guard.enable()
    registry[guard.DEFAULT_POLICY_KEY] = 1
    assert guard.disable() is True
    assert guard.DEFAULT_TASK_PATH not in service.tasks
    assert registry[guard.DEFAULT_POLICY_KEY] == 0
    assert calls["restarts"] == 1
    assert guard.state() == "off"


def test_disable_when_absent_and_no_close_clear_restarts_nothing(fakes):
    _, registry, calls = fakes
    assert guard.disable() is False
    assert registry == {}
    assert calls["restarts"] == 0


def test_task_xml_has_both_session_triggers_highest_and_own_user():
    xml = guard.task_xml("HOST\\owner")
    assert xml.count("<EventTrigger>") == 2
    assert "EventID=21" in xml and "EventID=25" in xml
    assert guard.SESSION_EVENT_CHANNEL in xml
    assert "<RunLevel>HighestAvailable</RunLevel>" in xml
    assert "<LogonType>InteractiveToken</LogonType>" in xml
    assert "<UserId>HOST\\owner</UserId>" in xml


def test_task_script_is_inline_ascii_and_targets_the_policy_key():
    xml = guard.task_xml("HOST\\owner", key="Software\\Test\\Explorer")
    encoded = re.search(r"-EncodedCommand (\S+)</Arguments>", xml).group(1)
    script = base64.b64decode(encoded).decode("utf-16-le")
    assert script.isascii()
    assert "HKCU:\\Software\\Test\\Explorer" in script
    assert "NoClose" in script
    # Explorer is stopped only in the script's own session.
    assert "$_.SessionId -eq $session" in script


def test_env_overrides_task_path_and_key(monkeypatch, fakes):
    service, registry, _ = fakes
    monkeypatch.setenv(guard.TASK_PATH_ENV, "\\Elsewhere\\guard")
    monkeypatch.setenv(guard.POLICY_KEY_ENV, "Software\\Elsewhere")
    registry["Software\\Elsewhere"] = 1
    service.tasks[guard.DEFAULT_TASK_PATH] = FakeTask("<Task/>")
    info = guard.payload()
    assert info["state"] == "off"
    assert info["no_close"] == 1


def test_payload_carries_both_elevated_commands(fakes):
    info = guard.payload()
    assert info["command_enable"].endswith("-m src.remote_power_guard enable")
    assert info["command_disable"].endswith("-m src.remote_power_guard disable")
    assert str(guard.PROJECT_ROOT) in info["command_enable"]


def test_cli_unelevated_exits_2(fakes):
    _, _, calls = fakes
    calls["elevated"] = False
    assert guard.main(["enable"]) == 2
