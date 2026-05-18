"""Mirror-window HWND tracking + Win32 close — webapp-side (issue #20).

Architecture (approach C from issue #20): the webapp process owns the
HWND-by-sid map. ``open_local_terminal_window`` polls the desktop after
spawning the Edge ``--app`` window and stashes the HWND once the mirror
page sets its unique ``document.title``. On Stop & Close, the webapp's
stop route forwards to the session-host (cooperative WS shutdown) AND
PostMessages ``WM_CLOSE`` to the HWND so the window vanishes even if
the page is unresponsive.

These tests mock ``win32gui`` entirely — they never touch a real window.
The launcher module imports ``win32gui`` lazily inside the functions
under test, so we patch it via ``monkeypatch.setitem(sys.modules, …)``.
"""

from __future__ import annotations

import sys
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src import launcher


# Window message constant — matches winuser.h. Tests assert the exact
# value so a refactor that swaps in something else would be caught.
WM_CLOSE = 0x0010


@pytest.fixture(autouse=True)
def _clear_hwnd_registry():
    """Module-level _mirror_hwnds leaks across tests otherwise."""
    launcher._mirror_hwnds.clear()
    yield
    launcher._mirror_hwnds.clear()


@pytest.fixture
def fake_win32gui(monkeypatch):
    """Replace win32gui in sys.modules so launcher's lazy import picks it up.

    Returns a MagicMock; tests configure ``.EnumWindows`` /
    ``.GetWindowText`` / ``.PostMessage`` as needed.
    """
    mod = MagicMock(name="win32gui")
    # Calling EnumWindows(cb, extra) invokes cb(hwnd, extra) for every
    # top-level window. Default: no windows. Tests override per-case.
    mod.EnumWindows.side_effect = lambda cb, extra: None
    mod.GetWindowText.return_value = ""
    monkeypatch.setitem(sys.modules, "win32gui", mod)
    # pywintypes is imported alongside for the error type.
    pywintypes_mod = MagicMock(name="pywintypes")
    pywintypes_mod.error = type("error", (Exception,), {})
    monkeypatch.setitem(sys.modules, "pywintypes", pywintypes_mod)
    return mod


# --------------------------------------------------------------- registry


class TestMirrorHwndRegistry:
    """register / forget / close — the tiny in-process HWND map."""

    def test_close_calls_postmessage_with_wm_close(self, fake_win32gui):
        launcher.register_mirror_hwnd("sid-abc", 12345)
        ok = launcher.close_mirror_window("sid-abc")
        assert ok is True
        fake_win32gui.PostMessage.assert_called_once_with(
            12345, WM_CLOSE, 0, 0
        )

    def test_close_unknown_sid_returns_false_no_postmessage(self, fake_win32gui):
        ok = launcher.close_mirror_window("never-registered")
        assert ok is False
        fake_win32gui.PostMessage.assert_not_called()

    def test_register_overwrites_previous_hwnd(self, fake_win32gui):
        launcher.register_mirror_hwnd("sid-1", 1111)
        launcher.register_mirror_hwnd("sid-1", 2222)
        launcher.close_mirror_window("sid-1")
        fake_win32gui.PostMessage.assert_called_once_with(
            2222, WM_CLOSE, 0, 0
        )

    def test_forget_removes_mapping(self, fake_win32gui):
        launcher.register_mirror_hwnd("sid-2", 9999)
        launcher.forget_mirror_hwnd("sid-2")
        ok = launcher.close_mirror_window("sid-2")
        assert ok is False
        fake_win32gui.PostMessage.assert_not_called()

    def test_close_clears_registration_on_success(self, fake_win32gui):
        launcher.register_mirror_hwnd("sid-3", 4242)
        launcher.close_mirror_window("sid-3")
        # Second call: nothing stashed any more, no second PostMessage.
        ok = launcher.close_mirror_window("sid-3")
        assert ok is False
        assert fake_win32gui.PostMessage.call_count == 1

    def test_close_swallows_pywintypes_error_dead_hwnd(
        self, fake_win32gui, monkeypatch
    ):
        """Window may have been closed manually before Stop & Close ran."""
        pywintypes_error = sys.modules["pywintypes"].error
        fake_win32gui.PostMessage.side_effect = pywintypes_error("dead hwnd")
        launcher.register_mirror_hwnd("sid-4", 5555)
        ok = launcher.close_mirror_window("sid-4")
        assert ok is False  # PostMessage failed
        # And the dead entry is dropped so future calls are clean.
        assert "sid-4" not in launcher._mirror_hwnds

    def test_forget_unknown_sid_is_silent(self, fake_win32gui):
        """Idempotent — fine to call on a sid that was never registered."""
        launcher.forget_mirror_hwnd("never-registered")  # no raise


# ----------------------------------------------------- EnumWindows polling


class TestHwndLookupAfterSpawn:
    """``open_local_terminal_window`` polls EnumWindows post-spawn."""

    @staticmethod
    def _enum_windows_returning(windows):
        """Build a fake EnumWindows that iterates (hwnd, title) pairs.

        The callback signature mirrors pywin32's: ``cb(hwnd, extra)``,
        and the test driver invokes ``win32gui.GetWindowText(hwnd)`` to
        get the title. We model that by routing the lookup back through
        the mock's GetWindowText.
        """
        def _side_effect(cb, extra):
            for hwnd, _title in windows:
                cb(hwnd, extra)
        return _side_effect

    @staticmethod
    def _gettext_lookup(windows):
        mapping = {hwnd: title for hwnd, title in windows}
        return lambda hwnd: mapping.get(hwnd, "")

    def test_finds_matching_title_and_registers_hwnd(
        self, fake_win32gui, monkeypatch
    ):
        sid = "deadbeef" + "0" * 24
        windows = [
            (100, "VS Code - app-launcher"),
            (200, "Microsoft Edge"),
            (300, f"app-launcher-mirror-{sid}"),
            (400, "Slack"),
        ]
        fake_win32gui.EnumWindows.side_effect = self._enum_windows_returning(
            windows
        )
        fake_win32gui.GetWindowText.side_effect = self._gettext_lookup(windows)
        # Skip the actual subprocess spawn — only the polling step is
        # under test here.
        monkeypatch.setattr(launcher, "_spawn_terminal_window", MagicMock())
        # Force the polling loop to be synchronous (call inline rather
        # than in a thread) so the test doesn't have to sleep.
        monkeypatch.setattr(launcher, "_run_in_thread", lambda fn: fn())

        launcher.open_local_terminal_window("http://127.0.0.1:8445/?terminal=" + sid, sid=sid)

        assert launcher._mirror_hwnds.get(sid) == 300

    def test_ignores_unrelated_windows(self, fake_win32gui, monkeypatch):
        sid = "feedface" + "1" * 24
        windows = [
            (10, "Visual Studio Code"),
            (20, "app-launcher-mirror-OTHER-SID"),  # different sid
        ]
        fake_win32gui.EnumWindows.side_effect = self._enum_windows_returning(
            windows
        )
        fake_win32gui.GetWindowText.side_effect = self._gettext_lookup(windows)
        monkeypatch.setattr(launcher, "_spawn_terminal_window", MagicMock())
        monkeypatch.setattr(launcher, "_run_in_thread", lambda fn: fn())

        launcher.open_local_terminal_window("http://127.0.0.1/?terminal=" + sid, sid=sid)

        assert sid not in launcher._mirror_hwnds

    def test_polling_times_out_without_registering(
        self, fake_win32gui, monkeypatch
    ):
        """Window never appears (~3 s budget) — no registration, no raise."""
        sid = "cafebabe" + "2" * 24
        # EnumWindows always returns no windows.
        fake_win32gui.EnumWindows.side_effect = lambda cb, extra: None
        monkeypatch.setattr(launcher, "_spawn_terminal_window", MagicMock())
        # Track sleeps so the test stays fast — replace time.sleep with
        # a tick counter that advances a fake clock past the deadline.
        ticks = SimpleNamespace(now=0.0)
        monkeypatch.setattr(launcher.time, "monotonic", lambda: ticks.now)
        def fake_sleep(seconds):
            ticks.now += seconds
        monkeypatch.setattr(launcher.time, "sleep", fake_sleep)
        monkeypatch.setattr(launcher, "_run_in_thread", lambda fn: fn())

        launcher.open_local_terminal_window("http://127.0.0.1/?terminal=" + sid, sid=sid)

        assert sid not in launcher._mirror_hwnds
        # Real-world budget is ~3 s; the test asserts the loop actually
        # advanced the (fake) clock past 2 s before giving up.
        assert ticks.now >= 2.0

    def test_no_sid_skips_polling(self, fake_win32gui, monkeypatch):
        """Backward compat: bare-URL call (no sid) doesn't poll."""
        monkeypatch.setattr(launcher, "_spawn_terminal_window", MagicMock())
        thread_spawner = MagicMock()
        monkeypatch.setattr(launcher, "_run_in_thread", thread_spawner)

        launcher.open_local_terminal_window("http://127.0.0.1:8445/")

        thread_spawner.assert_not_called()
        fake_win32gui.EnumWindows.assert_not_called()


# ------------------------------------------------------- import-error guard


class TestOptionalPywin32Dependency:
    """Mirror-window features must degrade gracefully if pywin32 is missing.

    The launcher boots on machines without pywin32 (CI / non-Windows
    contributors) — the close call should just return False instead of
    crashing the stop route.
    """

    def test_close_returns_false_when_win32gui_missing(self, monkeypatch):
        # Make `import win32gui` fail inside launcher.close_mirror_window.
        monkeypatch.setitem(sys.modules, "win32gui", None)
        launcher.register_mirror_hwnd("sid-x", 4242)
        ok = launcher.close_mirror_window("sid-x")
        assert ok is False
