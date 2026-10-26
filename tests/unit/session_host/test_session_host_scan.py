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


class TestTheParserCannotRaise:
    """The property the removed swallow was pretending to provide (#1009).

    `_parse_osc_title`'s body was wrapped in `except Exception: pass` over
    code that cannot raise - `str.find`, slicing (which clamps rather than
    raising), `ord` on a one-character string, `join`, `strip`. A swallow
    over unraisable code is worse than no guard: it is where the next real
    defect in this scanner would disappear silently, and this parser is
    edited often (six agents, #266/#396/#458).

    So the guard became an assertion. If someone adds an operation here that
    *can* raise, this fails loudly instead of the title quietly going
    missing.
    """

    ESC = "\x1b"
    BEL = "\x07"
    ST = "\x1b\\"

    def _pathological(self):
        esc, bel, st = self.ESC, self.BEL, self.ST
        return [
            "",
            esc,
            esc + "]",
            esc + "]0",
            esc + "];no code" + bel,
            esc + "]0;" + bel,
            esc + "]0;no terminator ever",
            esc + "]0;" + "x" * 500 + bel,
            esc + "]0;ctrl\x01chars\x02" + bel,
            esc + "]0;caf\u00e9 \u2705 \U0001f9f9" + bel,
            esc + "]0;\udcff lone surrogate" + bel,
            esc + "]0;semi;colons;inside" + bel,
            esc + "]0;first" + bel + esc + "]2;second" + st,
            esc + "]2;title" + esc,
            esc + "]" + st,
            esc * 20,
            bel * 20,
            esc + "]0;" + "y" * 300,
        ]

    def test_no_pathological_sequence_raises(self):
        for sample in self._pathological():
            try:
                remaining, extracted = _parse_osc_title(sample)
            except Exception as exc:  # noqa: BLE001
                raise AssertionError(
                    f"_parse_osc_title raised {exc.__class__.__name__} on {sample!r}; "
                    "it is unguarded by design - either fix the operation that "
                    "raises or handle that case explicitly, never re-add a swallow"
                ) from exc
            assert isinstance(remaining, str)
            assert isinstance(extracted, str)

    def test_a_generated_corpus_of_escape_soup_does_not_raise(self):
        import random

        rnd = random.Random(1009)
        alphabet = [self.ESC, "]", ";", self.BEL, "\\", "0", "2", "9", "a", " ", "\t", "\x01", "\u00e9"]
        for _ in range(2000):
            sample = "".join(rnd.choice(alphabet) for _ in range(rnd.randint(0, 40)))
            try:
                _parse_osc_title(sample)
            except Exception as exc:  # noqa: BLE001
                raise AssertionError(
                    f"_parse_osc_title raised {exc.__class__.__name__} on {sample!r}"
                ) from exc

    def test_the_swallow_has_not_come_back(self):
        """A bare `except Exception: pass` here would hide the above again."""
        import pathlib as _pathlib

        source = (
            _pathlib.Path(session_host_scan.__file__).read_text(encoding="utf-8")
        )
        lines = [ln.strip() for ln in source.splitlines()]
        for i, line in enumerate(lines[:-1]):
            if line.startswith("except") and lines[i + 1] == "pass":
                raise AssertionError(
                    f"{session_host_scan.__file__}:{i + 1} re-introduces a swallowed "
                    "exception; these scanners are pure and must fail loudly (#1009)"
                )
