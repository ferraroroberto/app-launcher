"""Issue #270: OSC colour-query/reply leak filter.

Codex emits OSC 10/11/12 colour *queries* (``ESC]10;?``) at startup; the
terminal answers them and the reply (``ESC]10;rgb:…``) leaks as visible text
on a fresh/dirty xterm. ``_strip_color_osc`` removes both forms at the PTY
read boundary — statefully, so a sequence split across two ``read()`` calls is
still fully stripped — while leaving title OSC (0/1/2), hyperlink OSC 8, and
ordinary text byte-exact.
"""

from __future__ import annotations

from src.session_host import _strip_color_osc


def _feed(*chunks: str) -> str:
    """Feed chunks through the stateful filter as the read loop would."""
    carry = ""
    out = []
    for chunk in chunks:
        cleaned, carry = _strip_color_osc(chunk, carry)
        out.append(cleaned)
    return "".join(out)


# ------------------------------------------------------------- whole-chunk strip


def test_strips_query_and_reply_in_one_chunk():
    # (a) ESC]10;?BEL (query) + ESC]11;rgb:…ST (reply) both vanish.
    chunk = "\x1b]10;?\x07\x1b]11;rgb:e6e6/e6e6/e6e6\x1b\\"
    assert _strip_color_osc(chunk, "") == ("", "")


def test_strips_osc12_cursor_color():
    assert _strip_color_osc("\x1b]12;rgb:0a0a/0a0a/0a0a\x07", "") == ("", "")


def test_strips_hex_color_payload():
    assert _strip_color_osc("\x1b]11;#0a0a0a\x07", "") == ("", "")


def test_strips_query_between_normal_text():
    chunk = "before\x1b]10;?\x07after"
    assert _strip_color_osc(chunk, "") == ("beforeafter", "")


# ------------------------------------------------------------- boundary split


def test_split_inside_sequence_is_fully_stripped():
    # (b) the same reply split mid-sequence across two feed() calls — the
    # boundary lands inside ESC]11;rgb:… — must still be fully stripped.
    full = "\x1b]10;?\x07\x1b]11;rgb:e6e6/e6e6/e6e6\x1b\\"
    for cut in range(1, len(full)):
        assert _feed(full[:cut], full[cut:]) == "", f"leak at cut={cut}"


def test_split_with_surrounding_text():
    full = "ABC\x1b]11;rgb:0a0a/0a0a/0a0a\x07DEF"
    for cut in range(1, len(full)):
        assert _feed(full[:cut], full[cut:]) == "ABCDEF", f"leak at cut={cut}"


# ------------------------------------------------------------- don't over-strip


def test_normal_text_survives_byte_exact():
    # (c) adjacent ordinary text untouched.
    text = "hello world\r\n\x1b[32mgreen\x1b[0m done"
    assert _strip_color_osc(text, "") == (text, "")


def test_title_osc_survives():
    # (d) OSC 0 title sequence is NOT a colour query — keep it.
    title = "\x1b]0;my window title\x07"
    assert _strip_color_osc(title, "") == (title, "")


def test_title_osc2_survives():
    title = "\x1b]2;another title\x1b\\"
    assert _strip_color_osc(title, "") == (title, "")


def test_hyperlink_osc8_survives():
    # OSC 8 hyperlink — must pass through untouched.
    link = "\x1b]8;;https://example.com\x07link text\x1b]8;;\x07"
    assert _strip_color_osc(link, "") == (link, "")


def test_clipboard_osc52_survives():
    clip = "\x1b]52;c;SGVsbG8=\x07"
    assert _strip_color_osc(clip, "") == (clip, "")


def test_csi_untouched():
    # A CSI DA-style sequence is not OSC — leave it (the #128 _force_repaint
    # path owns DA-leak handling on reconnect).
    csi = "\x1b[?1;2c\x1b[31mred\x1b[0m"
    assert _strip_color_osc(csi, "") == (csi, "")


# ------------------------------------------------------------- carry semantics


def test_trailing_partial_is_carried_not_dropped():
    # (e) a lone trailing partial sequence is carried, never dropped or
    # duplicated. The fragment is held back, then completes on the next feed.
    cleaned, carry = _strip_color_osc("text\x1b]10;rgb:e6", "")
    assert cleaned == "text"
    assert carry == "\x1b]10;rgb:e6"
    # Complete it next chunk — the whole sequence vanishes, no duplication.
    cleaned2, carry2 = _strip_color_osc("e6/e6e6/e6e6\x07", carry)
    assert cleaned2 == ""
    assert carry2 == ""


def test_lone_trailing_esc_is_carried():
    cleaned, carry = _strip_color_osc("done\x1b", "")
    assert cleaned == "done"
    assert carry == "\x1b"


def test_runaway_partial_is_flushed_not_wedged():
    # An ESC] that never terminates and grows past the cap must be flushed,
    # not carried forever — a stray ESC can't wedge the stream.
    cleaned, carry = _strip_color_osc("\x1b]10;" + "x" * 200, "")
    assert carry == ""  # flushed
    assert "\x1b]10;" in cleaned + carry


def test_fast_path_no_escape():
    # No ESC] and empty carry — pass straight through.
    assert _strip_color_osc("plain text only", "") == ("plain text only", "")
