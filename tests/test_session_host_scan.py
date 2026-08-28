"""src.session_host_scan._parse_osc_title (issue #796).

Regression coverage for the unbounded-buffer bug: the previous
implementation returned the *entire* scanned buffer as ``remaining``
(never dropping already-scanned plain text), so
``PtySession._osc_buffer`` grew without bound for the life of a session.
"""

from __future__ import annotations

from src import session_host_scan
from src.session_host_scan import _parse_osc_title

# Fixed expectation, not imported from the module under test: pre-fix code
# has no carry cap at all (that's the bug), so importing the constant would
# make this file fail to collect against the old code instead of failing
# the assertion that proves the regression.
_OSC_TITLE_CARRY_MAX = getattr(session_host_scan, "_OSC_TITLE_CARRY_MAX", 256)


class TestParseOscTitle:
    def test_extracts_bel_terminated_title(self):
        buf = "plain\x1b]0;my title\x07more plain"
        remaining, title = _parse_osc_title(buf)
        assert title == "my title"
        assert remaining == ""

    def test_extracts_st_terminated_title(self):
        buf = "plain\x1b]2;my title\x1b\\more plain"
        remaining, title = _parse_osc_title(buf)
        assert title == "my title"
        assert remaining == ""

    def test_plain_text_with_no_osc_is_dropped_not_carried(self):
        """The core regression: no OSC anywhere means nothing pending —
        `remaining` must not echo the whole buffer back."""
        buf = "just a big chunk of plain terminal output, no OSC here"
        remaining, title = _parse_osc_title(buf)
        assert title == ""
        assert remaining == ""

    def test_incomplete_trailing_sequence_is_carried(self):
        buf = "plain text\x1b]0;partial titl"
        remaining, title = _parse_osc_title(buf)
        assert title == ""
        assert remaining == "\x1b]0;partial titl"

    def test_carried_fragment_completes_on_next_chunk(self):
        first = "plain text\x1b]0;partial titl"
        remaining, title = _parse_osc_title(first)
        assert title == ""
        second = remaining + "e\x07after"
        remaining2, title2 = _parse_osc_title(second)
        assert title2 == "partial title"
        assert remaining2 == ""

    def test_buffer_never_grows_unbounded_across_many_reads(self):
        """The exact shape of the issue's measurement: thousands of 4 KB
        plain-text chunks, one OSC title every 10th chunk. `remaining`
        must stay tiny (never accumulate the scanned plaintext)."""
        osc_buffer = ""
        max_remaining_len = 0
        for i in range(2000):
            if i % 10 == 0:
                chunk = "\x1b]0;title-%d\x07" % i + "x" * 4096
            else:
                chunk = "x" * 4096
            osc_buffer += chunk
            osc_buffer, _title = _parse_osc_title(osc_buffer)
            max_remaining_len = max(max_remaining_len, len(osc_buffer))
        assert max_remaining_len <= _OSC_TITLE_CARRY_MAX

    def test_unterminated_sequence_past_cap_is_flushed(self):
        """A stray/malformed `ESC]` that never terminates must not be
        carried forever — it's dropped once it exceeds the carry cap."""
        buf = "\x1b]0;" + "x" * (_OSC_TITLE_CARRY_MAX + 50)
        remaining, title = _parse_osc_title(buf)
        assert title == ""
        assert remaining == ""
