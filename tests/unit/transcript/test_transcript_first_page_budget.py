"""Opening a long session's chat: the first transcript page stays small and fast.

The synthetic session has the shape of a real tool-heavy agent run (counts
taken from one, never its content): per exchange, seven tool calls with no
text, each followed by its result (short, medium or past the display cap)
and metadata rows (an attachment, an
api-request record, a data-flow record), then a thinking block and a reply.
A 40-turn page then spans about a thousand lines and several read windows,
which is where a re-parse on every window step used to cost.
"""

from __future__ import annotations

import gzip
import json
import random
import time
from pathlib import Path

import pytest

from src.session_view import transcript as st
from src.transcript_flavors import _shared
from tests.unit.transcript._support import _parse_whole, _tail_of, _walk

# Wire budget for the first response, gzipped as the webapp serves it.
FIRST_PAGE_GZIP_BUDGET = 100 * 1024
# Best of three on this box measured ~10 ms; the margin absorbs a loaded gate.
FIRST_PAGE_MS_BUDGET = 100

# Real first pages measured 3.8-4.3x under gzip; uniform picks from this many
# words give ~3.5x, so the synthetic page is if anything the harder case.
_WORDS = [f"w{n:x}" for n in range(512)]


def _words(rng: random.Random, n: int) -> str:
    # Varied words, so gzip compresses like real text rather than a repeat.
    return " ".join(rng.choice(_WORDS) for _ in range(n))


def _long_session(path: Path, exchanges: int) -> Path:
    rng = random.Random(2510)
    rows = []
    for i in range(exchanges):
        rows.append({"type": "user", "timestamp": "2026-09-14T10:00:00Z",
                     "message": {"role": "user", "content": _words(rng, 40)}})
        for t in range(7):
            mid, tid = f"m{i}-{t}", f"t{i}-{t}"
            rows.append({"type": "assistant", "timestamp": "2026-09-14T10:00:01Z", "message": {
                "id": mid, "role": "assistant", "content": [
                    {"type": "tool_use", "id": tid, "name": "Bash", "input": {"command": _words(rng, 30)}}]}})
            rows.append({"type": "user", "timestamp": "2026-09-14T10:00:02Z", "message": {
                "role": "user", "content": [{"type": "tool_result", "tool_use_id": tid,
                                             "content": _words(rng, rng.choice((20, 80, 400)))}]}})
            rows.append({"type": "attachment", "attachment": {"type": "synthetic", "content": _words(rng, 900)}})
            rows.append({"type": "api-request", "data": _words(rng, 250)})
            rows.append({"type": "data-flow-records", "data": _words(rng, 30)})
        rows.append({"type": "assistant", "timestamp": "2026-09-14T10:00:03Z", "message": {
            "id": f"m{i}", "role": "assistant", "content": [{"type": "thinking", "thinking": _words(rng, 200)}]}})
        rows.append({"type": "assistant", "timestamp": "2026-09-14T10:00:04Z", "message": {
            "id": f"m{i}", "role": "assistant", "content": [{"type": "text", "text": _words(rng, 120)}]}})
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


@pytest.fixture(scope="module")
def long_session(tmp_path_factory) -> Path:
    return _long_session(tmp_path_factory.mktemp("long") / "t.jsonl", 60)


def test_first_page_fits_the_wire_budget_and_time(long_session: Path):
    page = st.transcript_page(long_session, limit=st.DEFAULT_LIMIT)
    assert sum(1 for e in page["entries"] if st._is_turn(e)) == st.DEFAULT_LIMIT
    wire = len(gzip.compress(json.dumps(page).encode()))
    assert wire <= FIRST_PAGE_GZIP_BUDGET, f"first page is {wire // 1024} KB gzipped"
    best = min(_timed(long_session) for _ in range(3))
    assert best <= FIRST_PAGE_MS_BUDGET, f"first page took {best:.0f} ms"


def _timed(path: Path) -> float:
    started = time.perf_counter()
    st.transcript_page(path, limit=st.DEFAULT_LIMIT)
    return (time.perf_counter() - started) * 1000


def test_first_page_parses_each_line_about_once(long_session: Path, monkeypatch):
    """The deterministic half of the budget: a page that re-parses what it
    has collected on every window step fails here, however fast the box."""
    calls = []
    real = json.loads
    monkeypatch.setattr(_shared.json, "loads", lambda raw, *a, **k: calls.append(1) or real(raw, *a, **k))
    page = st.transcript_page(long_session, limit=st.DEFAULT_LIMIT)
    with long_session.open("rb") as fh:
        fh.seek(page["next_cursor"])
        lines = sum(1 for ln in fh if ln.strip())
    span = long_session.stat().st_size - page["next_cursor"]
    assert span > 3 * st.WINDOW_BYTES, "the page must span several read windows"
    assert len(calls) <= lines * 1.2, f"{len(calls)} parses for {lines} lines"


def test_walking_back_from_the_first_page_loses_and_repeats_nothing(tmp_path: Path):
    path = _long_session(tmp_path / "t.jsonl", 30)
    pages = _walk(path, st.DEFAULT_LIMIT)
    assert len(pages) > 1
    joined = [e for page in reversed(pages) for e in page]
    assert joined == _parse_whole(path, tail=_tail_of(path, st.DEFAULT_LIMIT))
