"""Screen control (#1521): the coordinate mapping, the events, held input, and
the one input path.

Nothing here injects: every session runs against a
:class:`src.desktop_input.RecordingInjector` (and ``tests/conftest.py``'s
autouse ``fake_injector`` replaces the process injector for the whole suite).

The mapping expectations are literal numbers, worked out once from the
formula the tower probe proved (``round((px - vx) * 65535 / (vw - 1))``), so a
change to the formula fails here rather than re-deriving itself.

The last section is the security pin: input reaches ``SendInput`` only
through this module, and this module only through the gated Screen socket.
A second path (an HTTP route, another socket, a direct Win32 call anywhere
else) fails one of those tests.
"""

from __future__ import annotations

import ast
import math
from pathlib import Path
from typing import List

import pytest

from src import desktop_input as di
from src.desktop_capture import Monitor

REPO = Path(__file__).resolve().parent.parent

# The tower: a 3840x2160 virtual desktop at origin (0, -1080), the second
# display up and to the right of the primary.
TOWER_PRIMARY = Monitor(id=0, left=0, top=0, width=1920, height=1080, primary=True)
TOWER_UPPER = Monitor(id=1, left=1920, top=-1080, width=1920, height=1080, primary=False)
TOWER = [TOWER_PRIMARY, TOWER_UPPER]

# A 150% scaled 4K primary beside a 100% 1080p. The process is per-monitor-v2
# DPI aware, so rects are physical: 3840x2160, not the logical 2560x1440.
SCALED = Monitor(id=0, left=0, top=0, width=3840, height=2160, primary=True)
BESIDE = Monitor(id=1, left=3840, top=0, width=1920, height=1080, primary=False)
SCALED_LAYOUT = [SCALED, BESIDE]

MOVE = di.MOUSEEVENTF_MOVE | di.MOUSEEVENTF_ABSOLUTE | di.MOUSEEVENTF_VIRTUALDESK

# Taken at import, before tests/conftest.py's autouse fixture swaps it out.
_REAL_GET_INJECTOR = di.get_injector


# ------------------------------------------------------------- the mapping


def test_the_tower_virtual_desktop_is_the_bounding_box():
    assert di.virtual_desktop(TOWER) == (0, -1080, 3840, 2160)
    assert di.virtual_desktop(SCALED_LAYOUT) == (0, 0, 5760, 2160)


@pytest.mark.parametrize(
    "monitor, x, y, expected",
    [
        # The primary: below the upper display, so its top edge is mid-desktop.
        (TOWER_PRIMARY, 0, 0, (0, 32783)),
        (TOWER_PRIMARY, 1, 0, (32759, 32783)),
        (TOWER_PRIMARY, 0, 1, (0, 65535)),
        (TOWER_PRIMARY, 1, 1, (32759, 65535)),
        (TOWER_PRIMARY, 0.5, 0.5, (16388, 49174)),
        # The upper display: negative origin on the virtual desktop.
        (TOWER_UPPER, 0, 0, (32776, 0)),
        (TOWER_UPPER, 1, 0, (65535, 0)),
        (TOWER_UPPER, 0, 1, (32776, 32752)),
        (TOWER_UPPER, 1, 1, (65535, 32752)),
        (TOWER_UPPER, 0.5, 0.5, (49164, 16391)),
    ],
)
def test_tower_corners_and_centre(monitor, x, y, expected):
    assert di.map_point(monitor, di.virtual_desktop(TOWER), x, y) == expected


@pytest.mark.parametrize(
    "monitor, x, y, expected",
    [
        (SCALED, 0, 0, (0, 0)),
        (SCALED, 1, 1, (43686, 65535)),
        (SCALED, 0.5, 0.5, (21849, 32783)),
        (BESIDE, 0, 0, (43698, 0)),
        (BESIDE, 1, 1, (65535, 32752)),
        (BESIDE, 0.5, 0.5, (54622, 16391)),
    ],
)
def test_a_150_percent_monitor_maps_in_physical_pixels(monitor, x, y, expected):
    assert di.map_point(monitor, di.virtual_desktop(SCALED_LAYOUT), x, y) == expected


def test_the_scaled_centre_is_not_the_logical_centre():
    """Mapping the scaled display by its logical 2560x1440 size would land
    a third of the way in, not in the middle."""
    logical = Monitor(id=0, left=0, top=0, width=2560, height=1440, primary=True)
    desktop = di.virtual_desktop(SCALED_LAYOUT)
    assert di.map_point(logical, desktop, 0.5, 0.5) != di.map_point(SCALED, desktop, 0.5, 0.5)


def test_the_far_edge_stays_on_its_own_monitor():
    """x = 1.0 is the primary's last pixel, never the next display's first."""
    desktop = di.virtual_desktop(SCALED_LAYOUT)
    right_edge = di.map_point(SCALED, desktop, 1.0, 0.5)[0]
    next_first = di.map_point(BESIDE, desktop, 0.0, 0.5)[0]
    assert right_edge < next_first


# ---------------------------------------------------------- event builders


def _session(injector=None, monitor=TOWER_PRIMARY, monitors=TOWER):
    return di.InputSession(injector or di.RecordingInjector(), monitor, monitors)


def test_a_click_is_one_batch_move_down_up():
    rec = di.RecordingInjector()
    assert _session(rec).handle({"type": "click", "button": "left", "x": 0.5, "y": 0.5})
    assert rec.batches == [[
        di.MouseEvent(MOVE, 16388, 49174),
        di.MouseEvent(di.MOUSEEVENTF_LEFTDOWN),
        di.MouseEvent(di.MOUSEEVENTF_LEFTUP),
    ]]


def test_a_right_click_uses_the_right_button():
    rec = di.RecordingInjector()
    _session(rec).handle({"type": "click", "button": "right", "x": 0, "y": 0})
    flags = [e.flags for e in rec.events]
    assert flags[1:] == [di.MOUSEEVENTF_RIGHTDOWN, di.MOUSEEVENTF_RIGHTUP]


def test_scroll_moves_then_wheels_both_axes_clamped():
    rec = di.RecordingInjector()
    _session(rec).handle({"type": "scroll", "x": 0.5, "y": 0.5, "dx": -60, "dy": 99999})
    move, wheel, hwheel = rec.events
    assert move.flags == MOVE
    assert (wheel.flags, wheel.data) == (di.MOUSEEVENTF_WHEEL, di.MAX_SCROLL)
    assert (hwheel.flags, hwheel.data) == (di.MOUSEEVENTF_HWHEEL, -60)


def test_a_zero_scroll_sends_nothing():
    rec = di.RecordingInjector()
    session = _session(rec)
    assert session.handle({"type": "scroll", "x": 0.5, "y": 0.5, "dx": 0, "dy": 0}) is False
    assert rec.batches == [] and session.count == 0


def _typed(events: List[di.KeyEvent]) -> str:
    units = [e.scan for e in events if e.flags & di.KEYEVENTF_UNICODE and not e.up]
    return b"".join(u.to_bytes(2, "little") for u in units).decode("utf-16-le")


def test_text_types_unicode_verbatim():
    events = di.text_events("ñ€ hola")
    assert _typed(events) == "ñ€ hola"
    assert all(e.vk == 0 and e.flags & di.KEYEVENTF_UNICODE for e in events)
    # Every unit goes down and then up.
    assert [e.up for e in events] == [False, True] * 7


def test_text_outside_the_bmp_is_a_surrogate_pair():
    events = di.text_events("😀")
    assert len(events) == 4
    assert _typed(events) == "😀"


def test_a_newline_is_enter_and_control_characters_are_dropped():
    events = di.text_events("a\nb\x07")
    vks = [(e.vk, e.up) for e in events if not e.flags & di.KEYEVENTF_UNICODE]
    assert vks == [(di.VK_RETURN, False), (di.VK_RETURN, True)]
    assert _typed(events) == "ab"


def test_ctrl_c_is_virtual_keys_around_the_letter():
    events = di.combo_events("c", ["ctrl"])
    assert [(e.vk, e.up) for e in events] == [
        (di.VK_CONTROL, False), (ord("C"), False), (ord("C"), True), (di.VK_CONTROL, True),
    ]
    assert not any(e.flags & di.KEYEVENTF_UNICODE for e in events)


def test_modifiers_go_down_in_order_and_up_in_reverse():
    events = di.combo_events("esc", ["shift", "ctrl"])
    assert [(e.vk, e.up) for e in events] == [
        (di.VK_CONTROL, False), (di.VK_SHIFT, False),
        (di.VK_ESCAPE, False), (di.VK_ESCAPE, True),
        (di.VK_SHIFT, True), (di.VK_CONTROL, True),
    ]


def test_arrows_delete_and_win_are_extended_keys():
    for key in ("up", "down", "left", "right", "delete"):
        down, _up = di.combo_events(key, [])
        assert down.flags & di.KEYEVENTF_EXTENDEDKEY, key
    win_down = di.combo_events("d", ["win"])[0]
    assert win_down.vk == di.VK_LWIN and win_down.flags & di.KEYEVENTF_EXTENDEDKEY
    enter_down = di.combo_events("enter", [])[0]
    assert not enter_down.flags & di.KEYEVENTF_EXTENDEDKEY


@pytest.mark.parametrize(
    "msg",
    [
        {"type": "click", "x": 1.5, "y": 0.5},
        {"type": "click", "x": -0.1, "y": 0.5},
        {"type": "click", "x": math.nan, "y": 0.5},
        {"type": "click", "x": "0.5", "y": 0.5},
        {"type": "click", "x": True, "y": 0.5},
        {"type": "click", "y": 0.5},
        {"type": "click", "button": "x1", "x": 0.5, "y": 0.5},
        {"type": "key", "key": "f13"},
        {"type": "key", "key": "ñ"},
        {"type": "key", "key": "c", "mods": ["hyper"]},
        {"type": "key", "key": "c", "mods": "ctrl"},
        {"type": "text", "text": ""},
        {"type": "text", "text": 5},
        {"type": "text", "text": "a" * (di.MAX_TEXT_CHARS + 1)},
        {"type": "teleport", "x": 0.5, "y": 0.5},
        {},
    ],
)
def test_a_bad_message_sends_nothing(msg):
    rec = di.RecordingInjector()
    session = _session(rec)
    with pytest.raises(di.InputRejected):
        session.handle(msg)
    assert rec.batches == [] and session.count == 0


def test_a_text_frame_that_is_not_an_input_message_is_ignored():
    rec = di.RecordingInjector()
    session = _session(rec)
    for frame in ("hello", "[1, 2]", '{"type": "click", "x": 9, "y": 9}', "x" * 9000):
        assert session.handle_text(frame) is False
    assert rec.batches == [] and session.count == 0
    assert session.handle_text('{"type": "text", "text": "ok"}') is True
    assert session.count == 1


# ------------------------------------------------------------ held input


def test_a_drag_holds_the_button_until_up():
    rec = di.RecordingInjector()
    session = _session(rec)
    session.handle({"type": "down", "button": "left", "x": 0.1, "y": 0.1})
    assert session.held[0] == {"left"}
    session.handle({"type": "move", "x": 0.4, "y": 0.4})
    session.handle({"type": "up", "button": "left", "x": 0.5, "y": 0.5})
    assert session.held == (set(), set())
    flags = [e.flags for e in rec.events]
    assert flags == [MOVE, di.MOUSEEVENTF_LEFTDOWN, MOVE, MOVE, di.MOUSEEVENTF_LEFTUP]
    assert session.count == 3


def test_a_second_down_on_a_held_button_does_not_press_it_again():
    rec = di.RecordingInjector()
    session = _session(rec)
    session.handle({"type": "down", "x": 0.1, "y": 0.1})
    session.handle({"type": "down", "x": 0.2, "y": 0.2})
    downs = [e for e in rec.events if e.flags == di.MOUSEEVENTF_LEFTDOWN]
    assert len(downs) == 1


def test_disconnect_releases_a_held_button():
    rec = di.RecordingInjector()
    session = _session(rec)
    session.handle({"type": "down", "button": "left", "x": 0.1, "y": 0.1})
    assert session.release_all() == 1
    assert rec.batches[-1] == [di.MouseEvent(di.MOUSEEVENTF_LEFTUP)]
    assert session.held == (set(), set())
    assert session.release_all() == 0, "nothing left to release"


class _ShortInjector(di.RecordingInjector):
    """Accepts only the first ``n`` events of the next batch, as Windows does
    when input is blocked mid-batch."""

    def __init__(self, n: int) -> None:
        super().__init__()
        self.n = n

    def send(self, events):
        super().send(events)
        n, self.n = self.n, len(events) * 100
        return min(n, len(events))


def test_a_combo_cut_short_leaves_its_modifier_held_until_release():
    rec = _ShortInjector(1)
    session = _session(rec)
    session.handle({"type": "key", "key": "c", "mods": ["ctrl", "shift"]})
    _buttons, keys = session.held
    assert {k.vk for k in keys} == {di.VK_CONTROL}
    session.release_all()
    released = rec.batches[-1]
    assert [(e.vk, e.up) for e in released] == [(di.VK_CONTROL, True)]


def test_disconnect_releases_every_held_button_and_modifier():
    rec = _ShortInjector(2)
    session = _session(rec)
    # Ctrl and Alt went down, then input was refused.
    session.handle({"type": "key", "key": "t", "mods": ["ctrl", "alt"]})
    session.handle({"type": "down", "button": "right", "x": 0.5, "y": 0.5})
    session.handle({"type": "down", "button": "left", "x": 0.5, "y": 0.5})
    assert session.release_all() == 4
    released = rec.batches[-1]
    keys = {(e.vk, e.up) for e in released if isinstance(e, di.KeyEvent)}
    buttons = {e.flags for e in released if isinstance(e, di.MouseEvent)}
    assert keys == {(di.VK_CONTROL, True), (di.VK_MENU, True)}
    assert buttons == {di.MOUSEEVENTF_LEFTUP, di.MOUSEEVENTF_RIGHTUP}


def test_the_fake_input_env_gives_a_recorder(monkeypatch):
    """What the gate's disposable webapp gets (``LAUNCHER_SCREEN_FAKE_INPUT=1``)."""
    monkeypatch.setattr(di, "_injector", None)
    monkeypatch.setenv(di.FAKE_INPUT_ENV, "1")
    assert isinstance(_REAL_GET_INJECTOR(), di.RecordingInjector)


def test_without_the_env_the_injector_is_the_real_one(monkeypatch):
    # Built, never called: constructing it injects nothing.
    monkeypatch.setattr(di, "_injector", None)
    monkeypatch.delenv(di.FAKE_INPUT_ENV, raising=False)
    assert isinstance(_REAL_GET_INJECTOR(), di.SendInputInjector)


# ------------------------------------------------ the one input path (security)


_INJECTION_NAMES = {"SendInput", "keybd_event", "mouse_event", "SetCursorPos"}
_INJECTION_MODULES = {"pyautogui", "pynput", "pydirectinput", "keyboard", "mouse"}


def _python_files():
    for root in ("src", "app"):
        yield from (REPO / root).rglob("*.py")
    yield REPO / "launcher.py"


def _identifiers(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            yield node.id
        elif isinstance(node, ast.Attribute):
            yield node.attr


def _imported_modules(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            yield base
            for alias in node.names:
                yield f"{base}.{alias.name}" if base else alias.name


def test_only_desktop_input_injects():
    """No code but ``src/desktop_input.py`` calls a Win32 input-injection
    API or imports an input-automation library."""
    offenders = []
    for path in _python_files():
        rel = path.relative_to(REPO).as_posix()
        if rel == "src/desktop_input.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = set(_identifiers(tree)) & _INJECTION_NAMES
        mods = {m.split(".")[0] for m in _imported_modules(tree)} & _INJECTION_MODULES
        if names or mods:
            offenders.append(f"{rel}: {sorted(names | mods)}")
    assert offenders == [], "input injection outside src/desktop_input.py: " + "; ".join(offenders)


def test_only_the_screen_socket_reaches_desktop_input():
    """``src.desktop_input`` is imported by the Screen router alone."""
    importers = []
    for path in _python_files():
        rel = path.relative_to(REPO).as_posix()
        if rel == "src/desktop_input.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported = any("desktop_input" in m for m in _imported_modules(tree))
        # importlib.import_module("src.desktop_input") counts too.
        dynamic = any(
            isinstance(n, ast.Constant) and isinstance(n.value, str) and "src.desktop_input" in n.value
            for n in ast.walk(tree)
        )
        if imported or dynamic:
            importers.append(rel)
    assert importers == ["app/webapp/routers/desktop_screen.py"]


def _screen_ws_body() -> List[ast.stmt]:
    tree = ast.parse((REPO / "app/webapp/routers/desktop_screen.py").read_text(encoding="utf-8"))
    funcs = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)}
    return funcs["screen_ws"].body


def test_the_input_session_is_built_once_inside_the_gated_socket():
    """The router builds an InputSession (and asks for the injector) in one
    place: ``screen_ws``, after the gate's refusal has returned."""
    source = (REPO / "app/webapp/routers/desktop_screen.py").read_text(encoding="utf-8")
    assert source.count("desktop_input.InputSession(") == 1
    assert source.count("desktop_input.get_injector(") == 1

    body = _screen_ws_body()
    gate_at = session_at = None
    for i, stmt in enumerate(body):
        text = ast.unparse(stmt)
        if gate_at is None and "_gate_refusal(" in text:
            gate_at = i
        if "desktop_input.InputSession(" in text:
            session_at = i
    assert gate_at is not None and session_at is not None
    # Right after the gate: a refusal closes and returns.
    refusal_check = body[gate_at + 1]
    assert isinstance(refusal_check, ast.If) and isinstance(refusal_check.body[-1], ast.Return)
    assert session_at > gate_at + 1


def test_the_screen_router_has_no_other_route():
    """Two routes: the monitor list (no input) and the gated socket."""
    from app.webapp.routers import desktop_screen

    paths = sorted((getattr(r, "path", ""), type(r).__name__) for r in desktop_screen.router.routes)
    assert paths == [("/api/screen/monitors", "APIRoute"), ("/api/screen/ws", "APIWebSocketRoute")]
