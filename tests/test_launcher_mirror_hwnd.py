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


# ------------------------------------------------ close-time title-scan (#199)


def _enum_windows_returning(windows):
    """Fake EnumWindows that iterates ``(hwnd, title)`` pairs as cb(hwnd, extra)."""
    def _side_effect(cb, extra):
        for hwnd, _title in windows:
            cb(hwnd, extra)
    return _side_effect


def _gettext_lookup(windows):
    mapping = {hwnd: title for hwnd, title in windows}
    return lambda hwnd: mapping.get(hwnd, "")


class TestCloseTimeTitleScan:
    """``close_mirror_window`` falls back to a live title-scan when no usable
    HWND is registered — the path that survives a webapp restart (issue #199).
    """

    def test_unregistered_sid_closes_window_found_by_title(self, fake_win32gui):
        """Registry empty (restart wiped it) but the window is still up —
        the close-time scan finds and dismisses it."""
        sid = "deadbeef" + "0" * 24
        windows = [(100, "VS Code"), (300, f"app-launcher-mirror-{sid}")]
        fake_win32gui.EnumWindows.side_effect = _enum_windows_returning(windows)
        fake_win32gui.GetWindowText.side_effect = _gettext_lookup(windows)

        ok = launcher.close_mirror_window(sid)

        assert ok is True
        fake_win32gui.PostMessage.assert_called_once_with(300, WM_CLOSE, 0, 0)

    def test_registered_hwnd_preferred_without_scanning(self, fake_win32gui):
        """A live registered HWND closes directly — no EnumWindows sweep."""
        launcher.register_mirror_hwnd("sid-reg", 999)

        ok = launcher.close_mirror_window("sid-reg")

        assert ok is True
        fake_win32gui.PostMessage.assert_called_once_with(999, WM_CLOSE, 0, 0)
        fake_win32gui.EnumWindows.assert_not_called()

    def test_dead_registered_hwnd_falls_back_to_scan(self, fake_win32gui):
        """A stale registered HWND (PostMessage raises) still closes the
        window if a fresh scan finds the real one."""
        sid = "feedface" + "1" * 24
        windows = [(700, f"app-launcher-mirror-{sid}")]
        fake_win32gui.EnumWindows.side_effect = _enum_windows_returning(windows)
        fake_win32gui.GetWindowText.side_effect = _gettext_lookup(windows)
        dead_err = sys.modules["pywintypes"].error
        posted: list[int] = []

        def _post(hwnd, msg, wparam, lparam):
            posted.append(hwnd)
            if hwnd == 123:  # the stale registered HWND
                raise dead_err("dead hwnd")

        fake_win32gui.PostMessage.side_effect = _post
        launcher.register_mirror_hwnd(sid, 123)

        ok = launcher.close_mirror_window(sid)

        assert ok is True
        assert posted == [123, 700]  # tried the stale one, then the scanned one

    def test_no_window_anywhere_returns_false(self, fake_win32gui):
        """Neither a registered HWND nor a titled window — clean False."""
        ok = launcher.close_mirror_window("ghost" + "0" * 27)
        assert ok is False
        fake_win32gui.PostMessage.assert_not_called()


class TestOrphanMirrorSweep:
    """``close_orphan_mirror_windows`` reconciles restart-orphaned windows
    against the live-session list (issue #199)."""

    def test_closes_orphans_keeps_live(self, fake_win32gui):
        live = "live" + "a" * 28
        orphan_a = "orphana" + "b" * 25
        orphan_b = "orphanb" + "c" * 25
        windows = [
            (1, "Visual Studio Code"),
            (2, f"app-launcher-mirror-{live}"),
            (3, f"app-launcher-mirror-{orphan_a}"),
            (4, f"app-launcher-mirror-{orphan_b}"),
            (5, "Slack"),
        ]
        fake_win32gui.EnumWindows.side_effect = _enum_windows_returning(windows)
        fake_win32gui.GetWindowText.side_effect = _gettext_lookup(windows)

        closed = launcher.close_orphan_mirror_windows([live])

        assert closed == 2
        posted = {c.args[0] for c in fake_win32gui.PostMessage.call_args_list}
        assert posted == {3, 4}  # only the two orphans

    def test_empty_live_list_closes_all_mirrors(self, fake_win32gui):
        """Caller's contract: an empty list means *no* session is live, so
        every mirror is an orphan. (Callers must not pass an empty list just
        because the session-host was unreachable — that's enforced upstream.)"""
        windows = [
            (2, "app-launcher-mirror-aaa"),
            (3, "app-launcher-mirror-bbb"),
        ]
        fake_win32gui.EnumWindows.side_effect = _enum_windows_returning(windows)
        fake_win32gui.GetWindowText.side_effect = _gettext_lookup(windows)

        assert launcher.close_orphan_mirror_windows([]) == 2

    def test_no_mirror_windows_returns_zero(self, fake_win32gui):
        windows = [(1, "Microsoft Edge"), (2, "Notepad")]
        fake_win32gui.EnumWindows.side_effect = _enum_windows_returning(windows)
        fake_win32gui.GetWindowText.side_effect = _gettext_lookup(windows)

        assert launcher.close_orphan_mirror_windows(["whatever"]) == 0
        fake_win32gui.PostMessage.assert_not_called()
