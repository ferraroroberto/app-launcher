"""``src/console_input.py`` — the detached-session console-input helper (#967).

Pins the record shapes the probe on the issue established (one key pair per
UTF-16 unit, a newline as Shift+Enter, the submitting Enter as a plain
``VK_RETURN``) and the CLI's contract with ``RemoteSession.submit_input``:
text on stdin, one JSON verdict line on stdout, exit 0 only on ``ok``.
The Win32 layer is a fake — nothing here attaches to a real console.
"""

from __future__ import annotations

import json
from typing import List

import pytest

from src import console_input
from src.console_input import (
    KEY_EVENT,
    SCAN_RETURN,
    SHIFT_PRESSED,
    VK_RETURN,
    ConsoleInputError,
    INPUT_RECORD,
    enter_records,
    key_records,
    settle_seconds,
    utf16_units,
)


def _keys(records: List[INPUT_RECORD]):
    return [
        (
            bool(r.Event.KeyEvent.bKeyDown),
            r.Event.KeyEvent.wVirtualKeyCode,
            r.Event.KeyEvent.UnicodeChar,
            r.Event.KeyEvent.dwControlKeyState,
        )
        for r in records
    ]


class FakeConsole:
    """Records every call in order; can be told to fail at attach or write."""

    def __init__(self, fail_attach: str = "", fail_write: str = "") -> None:
        self.calls: list = []
        self.fail_attach = fail_attach
        self.fail_write = fail_write

    def attach(self, pid: int) -> None:
        self.calls.append(("attach", pid))
        if self.fail_attach:
            raise ConsoleInputError(self.fail_attach)

    def write(self, records) -> int:
        self.calls.append(("write", _keys(list(records))))
        if self.fail_write:
            raise ConsoleInputError(self.fail_write)
        return len(records)


def test_utf16_units_split_astral_characters_into_two_units():
    assert utf16_units("a") == [0x61]
    # U+1F600 is a surrogate pair in UTF-16 — two records, not one.
    assert utf16_units("\U0001F600") == [0xD83D, 0xDE00]


def test_key_records_one_down_up_pair_per_unit_carrying_the_unit():
    records = key_records("ab")
    assert len(records) == 4
    assert all(r.EventType == KEY_EVENT for r in records)
    keys = _keys(records)
    assert [k[0] for k in keys] == [True, False, True, False]
    assert [k[2] for k in keys] == [0x61, 0x61, 0x62, 0x62]
    assert all(r.Event.KeyEvent.wRepeatCount == 1 for r in records)


def test_key_records_newline_is_shift_enter_and_cr_is_dropped():
    # CRLF normalises to one soft line break: a Shift+Enter pair whose
    # UnicodeChar is CR — the composers take that as a newline, never as
    # the submit (verified on the issue against Claude Code).
    keys = _keys(key_records("x\r\ny"))
    assert keys[2] == (True, VK_RETURN, 0x0D, SHIFT_PRESSED)
    assert keys[3] == (False, VK_RETURN, 0x0D, SHIFT_PRESSED)
    assert [k[2] for k in keys] == [0x78, 0x78, 0x0D, 0x0D, 0x79, 0x79]
    assert key_records("x\r\ny")[2].Event.KeyEvent.wVirtualScanCode == SCAN_RETURN


def test_enter_records_is_one_plain_return_pair():
    keys = _keys(enter_records())
    assert keys == [(True, VK_RETURN, 0x0D, 0), (False, VK_RETURN, 0x0D, 0)]


def test_settle_scales_with_size_between_floor_and_cap():
    assert settle_seconds(0) == console_input.SETTLE_FLOOR_S
    assert settle_seconds(100) == pytest.approx(0.6)
    assert settle_seconds(10_000) == console_input.SETTLE_CAP_S


def test_type_into_console_writes_text_settles_then_enter(monkeypatch):
    sleeps: list = []
    fake = FakeConsole()
    out = console_input.type_into_console(
        4242, "hi\nthere", submit=True, console=fake, sleep=sleeps.append
    )
    assert out == {"ok": True, "units": 8, "submitted": True}
    assert fake.calls[0] == ("attach", 4242)
    assert fake.calls[1][0] == "write" and len(fake.calls[1][1]) == 16
    # The Enter is its own write, after the settle — never appended to the
    # text records.
    assert fake.calls[2] == ("write", _keys(enter_records()))
    assert sleeps == [settle_seconds(8)]


def test_type_into_console_no_submit_writes_text_only():
    fake = FakeConsole()
    out = console_input.type_into_console(1, "abc", submit=False, console=fake)
    assert out["submitted"] is False and out["units"] == 3
    assert [c[0] for c in fake.calls] == ["attach", "write"]


def test_type_into_console_bare_submit_presses_enter_without_settle():
    sleeps: list = []
    fake = FakeConsole()
    out = console_input.type_into_console(1, "", submit=True, console=fake, sleep=sleeps.append)
    assert out == {"ok": True, "units": 0, "submitted": True}
    assert fake.calls == [("attach", 1), ("write", _keys(enter_records()))]
    assert sleeps == []


def test_cli_prints_ok_verdict_and_exits_zero(capsys, monkeypatch):
    monkeypatch.setattr(console_input.time, "sleep", lambda s: None)
    fake = FakeConsole()
    rc = console_input.main(["777"], text="ping", console=fake)
    assert rc == 0
    line = capsys.readouterr().out.strip()
    assert json.loads(line) == {"ok": True, "units": 4, "submitted": True}
    assert fake.calls[0] == ("attach", 777)


def test_cli_no_submit_flag_skips_the_enter(capsys):
    fake = FakeConsole()
    rc = console_input.main(["777", "--no-submit"], text="draft", console=fake)
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["submitted"] is False
    assert [c[0] for c in fake.calls] == ["attach", "write"]


def test_cli_attach_failure_is_a_json_error_and_exit_one(capsys):
    fake = FakeConsole(fail_attach="AttachConsole(777) failed: [5] Access is denied.")
    rc = console_input.main(["777"], text="ping", console=fake)
    assert rc == 1
    body = json.loads(capsys.readouterr().out)
    assert body["ok"] is False
    assert "AttachConsole(777) failed" in body["error"]
    # Nothing was written after a failed attach.
    assert fake.calls == [("attach", 777)]


def test_cli_reads_the_message_from_stdin_as_utf8(capsys, monkeypatch):
    import io
    import sys as _sys

    monkeypatch.setattr(console_input.time, "sleep", lambda s: None)
    fake = FakeConsole()
    stdin = io.TextIOWrapper(io.BytesIO("héllo".encode("utf-8")), encoding="utf-8")
    monkeypatch.setattr(_sys, "stdin", stdin)
    rc = console_input.main(["9"], console=fake)
    assert rc == 0
    typed = fake.calls[1][1]
    assert [k[2] for k in typed[::2]] == [ord(c) for c in "héllo"]
