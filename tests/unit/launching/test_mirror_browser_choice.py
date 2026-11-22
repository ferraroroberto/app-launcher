"""Settings > Sessions > Window browser: which browser a session's PC window opens in.

Chrome is the default; the chosen browser falls back to the other one and then to the
Windows default; the window runs on its own profile folder; and Stop & Close still finds
the window by its title marker whichever browser hosts it. Fake paths and a fake spawn
only: no real browser starts.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.desktop import browser as desktop_browser
from src.launching import launcher
from src.webapp_config import DEFAULT_MIRROR_BROWSER, WebappConfig, _validate

URL = "https://127.0.0.1:8445/?terminal=sid1"


def _installed(*names: str):
    return lambda name: f"C:/Apps/{name}.exe" if name in names else None


@pytest.mark.parametrize(
    "choice, installed, expected",
    [
        ("chrome", ("chrome", "edge"), "chrome"),
        ("edge", ("chrome", "edge"), "edge"),
        ("chrome", ("edge",), "edge"),
        ("edge", ("chrome",), "chrome"),
        ("chrome", (), None),
        ("edge", (), None),
        ("system", ("chrome", "edge"), None),
        ("nonsense", ("chrome", "edge"), "chrome"),
    ],
)
def test_pick_mirror_browser_falls_back_to_the_other_then_the_default(choice, installed, expected):
    picked = launcher.pick_mirror_browser(choice, _installed(*installed))
    assert (picked[0] if picked else None) == expected


@pytest.fixture
def spawn(monkeypatch):
    """A fake Popen plus a fake default-browser opener; returns both call lists."""
    popen_calls: list = []
    web_calls: list = []
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda argv, **kw: popen_calls.append(argv))
    monkeypatch.setattr(launcher.webbrowser, "open", lambda url: web_calls.append(url))
    return popen_calls, web_calls


def _choose(monkeypatch, choice: str, *installed: str):
    monkeypatch.setattr(launcher, "_mirror_browser_choice", lambda: choice)
    monkeypatch.setattr(launcher, "find_browser", _installed(*installed))


def test_chrome_opens_an_app_window_on_its_own_profile(monkeypatch, spawn):
    popen_calls, web_calls = spawn
    _choose(monkeypatch, "chrome", "chrome", "edge")
    launcher._spawn_terminal_window(URL)
    (argv,) = popen_calls
    assert argv[0] == "C:/Apps/chrome.exe" and web_calls == []
    assert f"--app={URL}" in argv and "--ignore-certificate-errors" in argv
    profile = next(a for a in argv if a.startswith("--user-data-dir="))
    assert Path(profile.split("=", 1)[1]).name == "mirror-window-chrome"


def test_each_browser_gets_its_own_profile_folder():
    assert launcher._mirror_profile_dir("chrome") != launcher._mirror_profile_dir("edge")


def test_a_missing_choice_opens_the_other_browser(monkeypatch, spawn):
    popen_calls, _ = spawn
    _choose(monkeypatch, "chrome", "edge")
    launcher._spawn_terminal_window(URL)
    assert popen_calls[0][0] == "C:/Apps/edge.exe"
    assert popen_calls[0][-3].endswith("mirror-window-edge")


def test_no_browser_or_the_default_choice_uses_the_windows_default(monkeypatch, spawn):
    popen_calls, web_calls = spawn
    _choose(monkeypatch, "chrome")
    launcher._spawn_terminal_window(URL)
    _choose(monkeypatch, "system", "chrome", "edge")
    launcher._spawn_terminal_window(URL)
    assert popen_calls == [] and web_calls == [URL, URL]


def test_a_browser_that_will_not_start_falls_back_to_the_default(monkeypatch, spawn):
    _, web_calls = spawn

    def boom(argv, **kw):
        raise OSError("no")

    monkeypatch.setattr(launcher.subprocess, "Popen", boom)
    _choose(monkeypatch, "edge", "edge")
    launcher._spawn_terminal_window(URL)
    assert web_calls == [URL]


def test_the_choice_is_read_from_the_saved_config(monkeypatch):
    import src.webapp_config as wc

    monkeypatch.setattr(wc, "load_webapp_config", lambda: WebappConfig(mirror_browser="edge"))
    assert launcher._mirror_browser_choice() == "edge"

    def broken():
        raise ValueError("bad file")

    monkeypatch.setattr(wc, "load_webapp_config", broken)
    assert launcher._mirror_browser_choice() == DEFAULT_MIRROR_BROWSER


def test_the_setting_defaults_to_chrome_and_refuses_an_unknown_browser():
    assert WebappConfig().mirror_browser == "chrome"
    _validate(WebappConfig(mirror_browser="edge"))
    with pytest.raises(ValueError, match="mirror_browser"):
        _validate(WebappConfig(mirror_browser="firefox"))


# ------------------------------------------------------------ finding a browser


def test_find_browser_prefers_app_paths_then_the_install_folders(monkeypatch, tmp_path):
    exe = tmp_path / "Google" / "Chrome" / "Application" / "chrome.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    for key in desktop_browser._INSTALL_ROOT_ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(desktop_browser, "_app_paths_exe", lambda name: None)
    assert desktop_browser.find_browser("chrome") is None
    monkeypatch.setenv("ProgramFiles", str(tmp_path))
    assert desktop_browser.find_browser("chrome") == str(exe)
    assert desktop_browser.find_browser("edge") is None
    monkeypatch.setattr(desktop_browser, "_app_paths_exe", lambda name: f"C:/Reg/{name}")
    assert desktop_browser.find_browser("chrome") == "C:/Reg/chrome.exe"
    assert desktop_browser.find_browser("edge") == "C:/Reg/msedge.exe"


# ------------------------------------------------- Stop & Close in either browser

CHROME_TITLE = "app-launcher-mirror-sid1 - Google Chrome"
EDGE_TITLE = "app-launcher-mirror-sid1 and 1 more page - Personal - Microsoft Edge"


@pytest.mark.parametrize("title", [CHROME_TITLE, EDGE_TITLE])
def test_stop_and_close_finds_the_window_by_its_marker_in_either_browser(monkeypatch, title):
    win32gui = MagicMock(name="win32gui")
    windows = {101: "Untitled - Notepad", 202: title, 303: "app-launcher-mirror-sid2 - Google Chrome"}
    win32gui.EnumWindows.side_effect = lambda cb, extra: [cb(h, extra) for h in windows]
    win32gui.GetWindowText.side_effect = lambda h: windows[h]
    monkeypatch.setitem(sys.modules, "win32gui", win32gui)
    launcher._mirror_hwnds.clear()

    assert launcher.close_mirror_window("sid1") is True
    win32gui.PostMessage.assert_called_once_with(202, 0x0010, 0, 0)
