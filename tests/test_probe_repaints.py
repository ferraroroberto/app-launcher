"""``scripts/probe_repaints.py`` — #930's full-viewport-repaint detector.

#930's conclusion is that the launcher must **not** suppress these repaints:
they are legitimate output from Claude Code, and a faithful terminal renders
them. What the launcher keeps instead is the ability to *measure* them, so a
future change can be judged against a number rather than a hunch. These tests
pin the detector's shape against synthetic byte fixtures — no real transcript
is read, so nothing here depends on a machine's own session history.

The fixtures are built from the exact escape sequence the agent emits:
``ESC[H`` then ``N x (ESC[2K ESC[1B)`` then ``ESC[H``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import probe_repaints

ESC = b"\x1b"


def preamble(rows: int) -> bytes:
    """The full-viewport repaint preamble for an ``rows``-tall viewport."""
    return ESC + b"[H" + (ESC + b"[2K" + ESC + b"[1B") * rows + ESC + b"[H"


def streamed(text: str) -> bytes:
    """Ordinary streaming output — no cursor addressing."""
    return text.encode()


class TestFindRepaints:
    def test_finds_nothing_in_plain_streaming_output(self):
        stream = streamed("a paragraph\r\nand another\r\n") * 50
        assert probe_repaints.find_repaints(stream) == []

    def test_finds_one_preamble_and_reports_its_viewport_height(self):
        stream = streamed("before") + preamble(40) + streamed("after")
        found = probe_repaints.find_repaints(stream)
        assert [r.rows for r in found] == [40]
        assert found[0].offset == len(b"before")

    def test_finds_each_preamble_in_order_with_its_own_height(self):
        stream = (
            streamed("x" * 10)
            + preamble(40)
            + streamed("y" * 20)
            + preamble(13)
            + streamed("z" * 30)
        )
        found = probe_repaints.find_repaints(stream)
        assert [r.rows for r in found] == [40, 13]
        assert [r.offset for r in found] == [10, 10 + len(preamble(40)) + 20]

    def test_ignores_a_relative_cursor_up_live_region_rewrite(self):
        """Ink's ordinary in-place rewrite must not read as a full repaint.

        The agent redraws its live region by stepping the cursor *up* and
        painting over it (``ESC[7A`` …). That is the common case by far; only
        the home-and-erase-the-whole-viewport form duplicates scrollback, so
        counting the relative form would drown the signal.
        """
        rewrite = ESC + b"[29D" + ESC + b"[4B" + ESC + b"[7A" + streamed("Working")
        assert probe_repaints.find_repaints(rewrite * 20) == []

    def test_ignores_an_erase_run_that_is_not_bracketed_by_cursor_home(self):
        """``ESC[2K ESC[1B`` alone clears rows without repainting from row 1."""
        stream = streamed("a") + (ESC + b"[2K" + ESC + b"[1B") * 40 + streamed("b")
        assert probe_repaints.find_repaints(stream) == []

    def test_a_single_row_preamble_still_counts(self):
        assert [r.rows for r in probe_repaints.find_repaints(preamble(1))] == [1]


class TestRowsHistogram:
    def test_counts_repaints_per_viewport_height(self):
        stream = preamble(40) + preamble(13) + preamble(40)
        found = probe_repaints.find_repaints(stream)
        assert probe_repaints.rows_histogram(found) == {13: 1, 40: 2}

    def test_empty_for_a_clean_stream(self):
        assert probe_repaints.rows_histogram([]) == {}


class TestDensityByRows:
    def test_attributes_bytes_to_the_height_they_were_produced_at(self):
        """A short viewport must show a *higher* repaint rate per MB.

        This is #930's dose-response in miniature: the same work, measured
        against two viewport heights, is what shows the rate tracks ``1/rows``
        and therefore that viewport height — not the resize path — is the
        lever.
        """
        # A long quiet run at 40 rows, then the same amount of work at 13
        # broken up by a repaint every time the live region reaches the top.
        stream = (
            streamed("a" * 5000)
            + preamble(40)
            + streamed("b" * 50)
            + preamble(13)
            + streamed("c" * 50)
            + preamble(13)
            + streamed("d" * 50)
        )
        density = probe_repaints.density_by_rows(
            len(stream), probe_repaints.find_repaints(stream)
        )
        tall_count, tall_bytes = density[40]
        short_count, short_bytes = density[13]
        assert tall_count == 1
        assert short_count == 2
        assert short_count / short_bytes > 5 * (tall_count / tall_bytes)

    def test_tail_after_the_last_repaint_is_attributed(self):
        stream = streamed("a" * 100) + preamble(40) + streamed("b" * 900)
        density = probe_repaints.density_by_rows(
            len(stream), probe_repaints.find_repaints(stream)
        )
        count, size = density[40]
        assert count == 1
        assert size == len(stream)

    def test_no_repaints_means_no_rows_reported(self):
        assert probe_repaints.density_by_rows(1000, []) == {}


class TestLoggedResizes:
    LOG = (
        "20:30:41 [INFO] src.session_host: PTY 1fd26061 resize 40x120 -> 37x51\n"
        "20:30:52 [INFO] src.session_host: PTY 1fd26061 resize 37x51 -> 13x51\n"
        "20:31:02 [INFO] src.session_host: PTY aaaaaaaa resize 40x51 -> 13x51\n"
    )

    def test_selects_only_the_requested_session(self):
        assert probe_repaints.logged_resizes(self.LOG, "1fd26061") == [
            (40, 120, 37, 51),
            (37, 51, 13, 51),
        ]

    def test_matches_a_full_session_id_against_the_truncated_log_form(self):
        """The log records 8 characters; callers pass the transcript stem."""
        full = "1fd260617a274c07a559e3298e27f650"
        assert len(probe_repaints.logged_resizes(self.LOG, full)) == 2

    def test_unknown_session_reports_nothing(self):
        assert probe_repaints.logged_resizes(self.LOG, "deadbeef") == []

    def test_empty_log_reports_nothing(self):
        assert probe_repaints.logged_resizes("", "1fd26061") == []


class TestCli:
    def test_reports_a_transcript_and_exits_clean(self, tmp_path, capsys):
        path = tmp_path / "0123456789abcdef0123456789abcdef.transcript"
        path.write_bytes(streamed("hello") + preamble(40) + streamed("world"))
        assert probe_repaints.main([str(path)]) == 0
        out = capsys.readouterr().out
        assert "repaints=1" in out
        assert "{40: 1}" in out

    def test_by_rows_prints_the_per_height_rate(self, tmp_path, capsys):
        path = tmp_path / "0123456789abcdef0123456789abcdef.transcript"
        path.write_bytes(streamed("x" * 500) + preamble(13))
        assert probe_repaints.main([str(path), "--by-rows"]) == 0
        assert "repaints/MB" in capsys.readouterr().out

    def test_resize_log_names_the_repaints_no_resize_explains(
        self, tmp_path, capsys
    ):
        """#930's reopening finding, as a number the tool prints directly."""
        sid = "1fd260617a274c07a559e3298e27f650"
        path = tmp_path / f"{sid}.transcript"
        path.write_bytes(preamble(40) * 5)
        log = tmp_path / "session-host.log"
        log.write_text(TestLoggedResizes.LOG, encoding="utf-8")
        assert probe_repaints.main([str(path), "--resize-log", str(log)]) == 0
        out = capsys.readouterr().out
        assert "logged resizes=2" in out
        assert "3 repaints with no resize" in out

    def test_missing_file_is_reported_without_crashing(self, tmp_path, capsys):
        assert probe_repaints.main([str(tmp_path / "nope.transcript")]) == 0
        assert "not a file" in capsys.readouterr().err


class TestDocumentedMechanism:
    """The disproof is a decision, so it is written down where it is found."""

    DOC = Path(__file__).resolve().parent.parent / "docs" / "launcher-owned-pty.md"

    def test_doc_records_that_most_repaints_are_not_resize_driven(self):
        text = self.DOC.read_text(encoding="utf-8")
        assert "probe_repaints" in text, (
            "the mechanism section must point at the tool that measures it"
        )
