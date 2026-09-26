"""Links from Chat and the transcripts open in the PC's Chrome (#1274).

The page hands a link to ``POST /api/open-url`` only when it runs on the PC
itself; the route refuses anyone else, and anything but http(s). Chrome is
found through the registry's App Paths, never a hardcoded path, and the
Windows default is the fallback and the ``desktop_browser: "system"`` choice.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.webapp import middleware
from app.webapp.routers import misc
from src import desktop_browser as db


def test_only_http_links_with_a_host_open():
    assert db.is_web_url("https://github.com/x/y/pull/1")
    assert db.is_web_url("http://127.0.0.1:8445/")
    for bad in ("file:///C:/Windows/win.ini", "javascript:alert(1)", "ms-settings:",
                "https://", r"C:\temp\x.html", ""):
        assert not db.is_web_url(bad), bad
    with pytest.raises(ValueError):
        db.open_url("file:///C:/x", "chrome")


def test_chrome_is_preferred_and_the_default_browser_is_the_fallback(monkeypatch):
    spawned, defaulted = [], []
    monkeypatch.setattr(db.subprocess, "Popen", lambda argv, **kw: spawned.append(argv))
    monkeypatch.setattr(db.webbrowser, "open", lambda url, new=0: defaulted.append(url))

    monkeypatch.setattr(db, "find_chrome", lambda: "C:/Apps/chrome.exe")
    assert db.open_url("https://example.test/a", "chrome") == "chrome"
    assert spawned == [["C:/Apps/chrome.exe", "https://example.test/a"]] and defaulted == []

    # "system" keeps the Windows default, Chrome installed or not.
    assert db.open_url("https://example.test/b", "system") == "system"
    # No Chrome found: the default browser, not an error.
    monkeypatch.setattr(db, "find_chrome", lambda: None)
    assert db.open_url("https://example.test/c", "chrome") == "system"
    assert defaulted == ["https://example.test/b", "https://example.test/c"]


def test_find_chrome_reads_app_paths_and_ignores_a_stale_entry(monkeypatch, tmp_path):
    winreg = pytest.importorskip("winreg")
    exe = tmp_path / "chrome.exe"
    exe.write_bytes(b"")
    entries = {winreg.HKEY_LOCAL_MACHINE: str(tmp_path / "gone.exe"), winreg.HKEY_CURRENT_USER: f'"{exe}"'}

    class _Key:
        def __init__(self, hive):
            self.hive = hive

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def open_key(hive, path):
        assert path.endswith(r"App Paths\chrome.exe")
        if hive not in entries:
            raise OSError("no key")
        return _Key(hive)

    monkeypatch.setattr(winreg, "OpenKey", open_key)
    monkeypatch.setattr(winreg, "QueryValueEx", lambda key, name: (entries[key.hive], 1))
    # HKLM names a missing file, so the user-level entry wins, quotes stripped.
    assert db.find_chrome() == str(exe)


def test_route_opens_only_for_the_pc_and_only_web_links(webapp_client, monkeypatch):
    client, app, _ = webapp_client
    opened = []
    monkeypatch.setattr(misc, "open_url", lambda url, pref: opened.append((url, pref)) or "chrome")
    monkeypatch.delenv("LAUNCHER_SESSION_HOST_PORT", raising=False)

    # Anyone but the PC itself (a phone or laptop over the tailnet) is refused.
    refused = client.post("/api/open-url", json={"url": "https://example.test/"})
    assert refused.status_code == 403 and opened == []

    monkeypatch.setattr(middleware, "LOOPBACK_HOSTS", middleware.LOOPBACK_HOSTS | {"testclient"})
    assert client.post("/api/open-url", json={"url": "file:///C:/x"}).status_code == 400
    app.state.webapp_config = replace(app.state.webapp_config, desktop_browser="system")
    ok = client.post("/api/open-url", json={"url": "https://example.test/pr"})
    assert ok.status_code == 200 and ok.json() == {"opened": True, "browser": "chrome"}
    assert opened == [("https://example.test/pr", "system")]

    # A disposable e2e / verify instance never opens a browser on the desktop.
    monkeypatch.setenv("LAUNCHER_SESSION_HOST_PORT", "18999")
    assert client.post("/api/open-url", json={"url": "https://example.test/x"}).json() == {
        "opened": False, "browser": "none"}
    assert len(opened) == 1
