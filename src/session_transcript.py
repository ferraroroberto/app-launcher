"""Paginated, typed transcript pages for one live Coding session (#953).

The Coding tab's transcript overlay reads a session's *native* history —
Claude Code's hook JSONL, a Codex rollout, Grok Build's ACP-style
``updates.jsonl`` stream, a Pi session JSONL, an Antigravity CLI
conversation log, or a Copilot CLI ``events.jsonl``
(:data:`FLAVORS`) — as an ordered list of typed
entries the phone can render chat-style: typed ``user`` prompts and
``assistant`` replies expanded, everything else (``tool_call`` +
paired result, ``thinking``, ``system`` plumbing, sub-agent traffic) folded.

Every reader here is **bounded**: a long session's JSONL runs to many MB, so
a page is assembled from fixed-size byte windows read *backwards* from a
cursor until enough conversation turns are in hand, within a
:data:`REQUEST_BYTE_CAP` budget that charges a huge line only
:data:`LINE_CHARGE_CAP` (#1120) and never past :data:`REQUEST_READ_CEILING`
real bytes per request. The cursor handed back is the byte
offset of the oldest line the page kept, so the next (older) page reads
strictly before it — no turn is duplicated or dropped across pages.

Since #1050 the cursor also runs *forwards*: :func:`transcript_tail` reads
only what has been appended since a given offset, so a chat view that is
being looked at can stay live without re-reading the newest page on a timer
(measured on this box: 0.05–0.10 ms for a tick's worth of appends against
18–45 ms to rebuild the newest page of a multi-MB session, and 0.02 ms for
the ``stat`` that skips the read entirely while the file is unchanged). The
same bounds apply: a forward read never exceeds :data:`REQUEST_BYTE_CAP`,
and a gap wider than that asks the caller to reload rather than replay.

Deliberately **not** on the session-host import closure (CLAUDE.md
"session-host"): this is a webapp-only reader, so a change here is never a
``:8446`` restart concern.

The six line grammars (:data:`FLAVORS`) live one per module under
``src/transcript_flavors/`` (#1309) — this module keeps only what is common
to all of them: the byte-window paging above, the live-tail cursor, on-demand
image decoding, and the registry that dispatches to each flavour's builder.
"""

from __future__ import annotations

import base64
import binascii
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.transcript_flavors._shared import (
    Entry, EntryBuilder, Line, _loads, _line_image_blocks, _image_data, cap_diff,
)
from src.transcript_flavors.antigravity import _antigravity_line_key, antigravity_entries
from src.transcript_flavors.claude import _claude_line_key, claude_entries
from src.transcript_flavors.codex import _codex_line_key, codex_entries
from src.transcript_flavors.copilot import _copilot_line_key, copilot_entries
from src.transcript_flavors.grok import _grok_line_key, grok_entries
from src.transcript_flavors.pi import _pi_line_key, pi_entries

# One backwards read step. 256 KB is the same window the Board's
# last-exchange reader uses (`board_transcript._EXCHANGE_TAIL_BYTES`).
WINDOW_BYTES = 256 * 1024
# Budget for one page, whatever the turn count: a long run of tool results
# must not turn one request into a whole-file read. Hitting it returns a
# short page with a cursor, not an error. A backwards page charges each line
# at most LINE_CHARGE_CAP against it (#1120); forward reads and the
# copy-full-text read still count real bytes against it.
REQUEST_BYTE_CAP = 2 * 1024 * 1024
# A backwards page charges one line at most this much against
# REQUEST_BYTE_CAP (#1120). Screenshot tool results are ~1 MB base64 lines,
# so charging them in full left a 2 MiB page holding two or three of them and
# often no user or assistant turn at all. The charge cap lets a page reach
# its `limit` turns past them instead.
LINE_CHARGE_CAP = 64 * 1024
# The real-bytes ceiling that keeps a charged page bounded: however few bytes
# its lines were charged, a page never reads more than this. Measured on a
# synthetic run of 1 MB lines: ~40 ms to read and parse 16 MiB, against ~6 ms
# for a 2 MiB page.
REQUEST_READ_CEILING = 16 * 1024 * 1024
DEFAULT_LIMIT = 40
MAX_LIMIT = 100

# Entry kinds that count as a *conversation turn* for pagination — the
# folded kinds ride along with whichever turns they sit between.
CONVERSATION_KINDS = frozenset({"user", "assistant"})


# ------------------------------------------------------------------ reading


def _read_lines_before(path: Path, start: int, end: int) -> Tuple[List[Line], Optional[int]]:
    """Complete lines of ``path`` that begin in ``[start, end)``.

    Returns ``(lines, oldest_offset)`` — each line as ``(byte offset,
    decoded text)`` in file order, and the offset of the oldest line kept
    (``None`` when the window held no complete line). A line torn at
    ``start`` is dropped — its start lies before the window, so the *next*
    (older) window contains it whole — unless ``start`` is 0 or the byte
    before it is a newline, in which case nothing is torn. Raises OSError on
    a read failure; callers turn that into a distinct "read failed".
    """
    if end <= start:
        return [], None
    read_from = start - 1 if start > 0 else 0
    with path.open("rb") as fh:
        fh.seek(read_from)
        raw = fh.read(end - read_from)
    if start > 0:
        # raw[0] is the byte *before* the window: a newline means the window
        # starts on a line boundary; anything else means a torn first line.
        if raw[:1] == b"\n":
            raw = raw[1:]
            pos = start
        else:
            cut = raw.find(b"\n")
            if cut < 0:
                return [], None
            raw = raw[cut + 1:]
            pos = start + cut  # the dropped prefix spanned [start-1, start+cut)
    else:
        pos = 0
    lines: List[Line] = []
    oldest: Optional[int] = None
    for chunk in raw.split(b"\n"):
        offset = pos
        pos += len(chunk) + 1
        if not chunk.strip():
            continue
        if oldest is None:
            oldest = offset
        lines.append((offset, chunk.decode("utf-8", errors="replace")))
    return lines, oldest


LineKey = Callable[[str], Optional[str]]


def _drop_partial_leading_message(lines: List[Line], line_key: LineKey) -> List[Line]:
    """Drop every message whose span reaches the window's leading edge.

    A window edge can fall inside a multi-line message: Claude writes one
    line per content block under one ``message.id``, and a message's span
    runs from its first to its last line *with tool results (and a
    sub-agent's own messages) in between* — measured on a real transcript,
    1 in 13 messages carries a tool result between its ``tool_use`` and its
    trailing text. The lines before the edge are unread, so a keyed line at
    the edge may be a message's tail: drop through that message's last line,
    and repeat while the new leading line is keyed (a nested sub-agent
    message inside a parent's span). Dropping a message that was in fact
    whole costs nothing — the next, older window re-reads those lines,
    since it ends where the kept lines begin — and guarantees a page never
    opens with half a message.
    """
    keys = [line_key(raw) for _, raw in lines]
    i = 0
    while i < len(lines) and keys[i] is not None:
        key = keys[i]
        last = max(j for j in range(i, len(lines)) if keys[j] == key)
        i = last + 1
    return lines[i:]


def _read_lines_from(path: Path, start: int, span: int, eof: int) -> Tuple[List[Line], int]:
    """Complete lines of ``path`` starting exactly at ``start`` — a known
    line boundary, unlike :func:`_read_lines_before`'s arbitrary window edge
    — for up to ``span`` bytes.

    Returns ``(lines, end)``: ``end`` is the byte position just past the
    last complete line kept. A line torn at the window's far edge is
    dropped (``end`` stops before it) unless the window reached ``eof``, in
    which case the file has nothing more to complete it with, so it counts
    as whole. ``(``[]``, start)`` means nothing complete fit — the caller
    widens ``span`` and reads the same ``start`` again.
    """
    with path.open("rb") as fh:
        fh.seek(start)
        raw = fh.read(span)
    end = start + len(raw)
    if end < eof:
        cut = raw.rfind(b"\n")
        if cut < 0:
            return [], start
        raw = raw[:cut]
        end = start + cut + 1
    lines: List[Line] = []
    pos = start
    for chunk in raw.split(b"\n"):
        offset = pos
        pos += len(chunk) + 1
        if chunk.strip():
            lines.append((offset, chunk.decode("utf-8", errors="replace")))
    return lines, end


def entry_full_text(path: Path, offset: int, flavor: str) -> Optional[Dict[str, Any]]:
    """The uncapped text of the turn entry beginning at byte ``offset``.

    The counterpart of a capped, ``truncated: true`` entry from
    :func:`transcript_page` — the copy-full-text route (#985) calls this
    when the phone wants the whole thing. A ``user`` entry is always one
    line; a Claude ``assistant`` entry can span several (one line per
    content block, same ``message.id``), so this reads forward from
    ``offset`` — mirroring :func:`_drop_partial_leading_message`'s span
    logic in the opposite direction — until a read line past the last one
    keyed to that message confirms nothing more will merge into it, or the
    read hits :data:`REQUEST_BYTE_CAP` (a message that huge is already a
    pathological case; the result then stays best-effort and reports
    itself ``truncated`` rather than blocking on an unbounded read).

    Returns ``None`` when ``offset`` no longer starts a turn there — the
    file was rotated/truncated since the page was served, or the offset
    was never one to begin with.
    """
    build, line_key, _ = _flavor(flavor)
    eof = path.stat().st_size
    if offset < 0 or offset >= eof:
        return None
    lines: List[Line] = []
    pos = offset
    target_key: Optional[str] = None
    bytes_read = 0
    hit_cap = False
    span = WINDOW_BYTES
    while True:
        more, new_pos = _read_lines_from(path, pos, span, eof)
        if not more and new_pos == pos:
            span *= 2
            if bytes_read + span > REQUEST_BYTE_CAP:
                hit_cap = True
                break
            continue
        bytes_read += new_pos - pos
        pos = new_pos
        if not lines and more:
            target_key = line_key(more[0][1])
        lines.extend(more)
        if target_key is None:
            break  # a single-line entry (user, or any non-assistant kind)
        last_match = max(
            (i for i, (_, raw) in enumerate(lines) if line_key(raw) == target_key),
            default=-1,
        )
        if last_match < len(lines) - 1:
            break  # a later, differently-keyed line confirms the span closed
        if pos >= eof:
            break
        if bytes_read >= REQUEST_BYTE_CAP:
            hit_cap = True
            break
        span = WINDOW_BYTES
    if not lines or lines[0][0] != offset:
        return None
    entries = build(lines, uncapped=True)
    for e in entries:
        if e.get("offset") == offset and _is_turn(e):
            return {"text": e["text"], "truncated": bool(e.get("truncated")) or hit_cap}
    return None


# ------------------------------------------------ per-step diffs (#1349)
#
# A builder hands every edit/write action its diff capped at
# ``_shared.DIFF_FULL_BYTES``; a page carries far less per step, since one
# page holds up to MAX_LIMIT turns and a turn can hold many edits. The rest
# is one request away: :func:`entry_diff` re-reads the step by ``offset`` and
# its index ``n`` among the tool calls built from that line.
DIFF_INLINE_LINES = 80
DIFF_INLINE_BYTES = 6_000


def _inline_diffs(entries: List[Entry]) -> List[Entry]:
    """Trim each tool call's diff to the page caps and give it the ref
    :func:`entry_diff` answers to: ``diff.offset``, the line the call was
    built from (a folded entry's own offset is not exposed), and
    ``diff.n``, its index among that line's calls."""
    seen: Dict[int, int] = {}
    for e in entries:
        if e.get("kind") != "tool_call" or not isinstance(e.get("offset"), int):
            continue
        offset = e["offset"]
        n = seen.get(offset, 0)
        seen[offset] = n + 1
        action = e.get("action")
        diff = action.get("diff") if isinstance(action, dict) else None
        if not diff:
            continue
        inline = cap_diff(diff["hunks"], DIFF_INLINE_BYTES, DIFF_INLINE_LINES)
        inline["numbered"] = diff["numbered"]
        inline["truncated"] = inline["truncated"] or diff["truncated"]
        inline["offset"] = offset
        inline["n"] = n
        action["diff"] = inline
    return entries


def entry_diff(path: Path, offset: int, n: int, flavor: str) -> Optional[Dict[str, Any]]:
    """The whole diff (up to ``DIFF_FULL_BYTES``) of the ``n``-th tool call
    built from the line at byte ``offset`` — a page's ``diff_truncated``
    step, fetched on demand.

    Reads forward from ``offset`` like :func:`entry_full_text`, until the
    call's result has been read too (a Claude result carries the recorded
    diff that replaces the one worked out from the input) or
    :data:`REQUEST_BYTE_CAP`, so each request is bounded. ``None`` when that
    line holds no such call with a diff any more (rotated, or a bad ref).
    """
    build, _line_key, _ = _flavor(flavor)
    eof = path.stat().st_size
    if offset < 0 or offset >= eof:
        return None
    lines: List[Line] = []
    pos = offset
    span = WINDOW_BYTES
    while True:
        more, new_pos = _read_lines_from(path, pos, span, eof)
        if not more and new_pos == pos:
            if (pos - offset) + span * 2 > REQUEST_BYTE_CAP:
                break  # one line past the cap: nothing bounded reads it
            span *= 2
            continue
        lines.extend(more)
        pos, span = new_pos, WINDOW_BYTES
        if lines[0][0] != offset:
            return None
        calls = [e for e in build(lines) if e.get("kind") == "tool_call" and e.get("offset") == offset]
        call = calls[n] if 0 <= n < len(calls) else None
        if call is not None and call.get("result") is not None:
            break
        if pos >= eof or pos - offset >= REQUEST_BYTE_CAP:
            break
    if not lines or lines[0][0] != offset:
        return None
    calls = [e for e in build(lines) if e.get("kind") == "tool_call" and e.get("offset") == offset]
    if not 0 <= n < len(calls):
        return None
    action = calls[n].get("action")
    if not isinstance(action, dict) or not action.get("diff"):
        return None
    return {"path": action.get("path"), "diff": action["diff"]}


def _page(
    path: Path, before: Optional[int], limit: int, build: EntryBuilder, line_key: LineKey
) -> Dict[str, Any]:
    """Assemble one page of entries ending at byte ``before``.

    Reads :data:`WINDOW_BYTES` windows backwards until ``limit``
    conversation turns (``user`` + ``assistant`` entries) are collected, the
    file start is reached, the kept lines have been charged
    :data:`REQUEST_BYTE_CAP` (each at most :data:`LINE_CHARGE_CAP`), or
    :data:`REQUEST_READ_CEILING` real bytes have been read.
    The page is then trimmed at a turn boundary so it holds at most
    ``limit`` turns, and ``next_cursor`` is the offset of the oldest line it
    kept (``None`` once the file start is included). A page never opens
    inside a multi-line message, and a cut never splits one.
    """
    size = path.stat().st_size
    at_eof = before is None or before > size
    end = size if at_eof else max(0, before)
    limit = max(1, min(int(limit), MAX_LIMIT))
    collected: List[Line] = []
    bytes_read = 0
    charged = 0
    span = WINDOW_BYTES
    while True:
        start = max(0, end - span)
        lines, oldest = _read_lines_before(path, start, end)
        bytes_read += end - start
        if start == 0:
            collected = lines + collected
            break
        if oldest is not None:
            lines = _drop_partial_leading_message(lines, line_key)
        if not lines:
            # Nothing whole in the window — a single record wider than it (an
            # image-paste prompt carries base64 blocks), or one message
            # spanning it. Widen *in place* — `end` stays put — so it is
            # eventually read whole; sliding `end` into its middle would tear
            # its tail off for good.
            span *= 2
            continue
        collected = lines + collected
        charged += sum(min(len(raw), LINE_CHARGE_CAP) for _, raw in lines)
        if (
            _turns(build(collected)) >= limit
            or charged >= REQUEST_BYTE_CAP
            or bytes_read >= REQUEST_READ_CEILING
        ):
            break
        # Next window ends where the oldest kept line begins (the lines
        # dropped above it get re-read whole). A span that had to widen keeps
        # its width (#1120): huge lines come in runs (a screenshot per tool
        # call), and restarting each one at WINDOW_BYTES re-read ~3.75 MB to
        # land one 1 MB line, spending the read ceiling on re-reads.
        end = lines[0][0]

    entries = build(collected)
    # Trim to `limit` turns at a turn boundary: drop every raw line before the
    # line that opens the oldest surviving turn — pulled back to that
    # message's first line so its thinking/tool blocks stay with its text —
    # then rebuild so a tool result whose call fell off the page stands on
    # its own instead of vanishing. Sidechain turns are never cut points
    # (`_is_turn`), so the cut message is a top-level one, which nothing
    # else's span contains.
    # A page that includes the file start and fits in `limit` keeps every
    # line; any other page is trimmed to its oldest turn — even at exactly
    # `limit` turns — so it never opens at a raw window edge that may fall
    # between a tool call and its result. (A page with no turn at all — a
    # multi-MB autonomous stretch under the byte cap — keeps the edge.)
    turn_offsets = [e["offset"] for e in entries if _is_turn(e)]
    whole_file = start == 0 and len(turn_offsets) <= limit
    if turn_offsets and not whole_file:
        cut = turn_offsets[max(0, len(turn_offsets) - limit)]
        idx = next(i for i, ln in enumerate(collected) if ln[0] == cut)
        cut_key = line_key(collected[idx][1])
        if cut_key is not None:
            idx = next(i for i, ln in enumerate(collected) if line_key(ln[1]) == cut_key)
        collected = collected[idx:]
        entries = build(collected)
    # The next page reads strictly before the oldest line this one kept;
    # None once that line is the file's first (an empty `collected` means an
    # empty file — the loop only exits without lines at offset 0).
    next_cursor: Optional[int] = collected[0][0] if collected else None
    if next_cursor == 0:
        next_cursor = None
    # Where this page's provisional region begins (#1050) — the newest
    # message, which the agent may still be appending to. No lines at all (an
    # empty file, or one holding only blanks) still needs a cursor a live
    # read can resume from, and that is the end of the file.
    idx = _pending_from(collected, build, line_key) if collected else None
    tail = collected[idx][0] if idx is not None else size
    # A turn keeps its offset — the copy-full-text route (#985) needs it to
    # ask for the uncapped entry; the folded kinds have no such use and stay
    # unexposed, same as before. The exception is the live tail (#1050): every
    # entry from `tail` on carries one, folded kinds included, because that is
    # the region a live view re-renders and it needs a stable key per card to
    # carry an open disclosure across the rebuild. `entries` itself stays one
    # complete list — the page contract every other caller reads is unchanged.
    # Diff refs (#1349) are taken first: they need every call's offset.
    _inline_diffs(entries)
    for e in entries:
        if not _is_turn(e) and not (at_eof and e.get("offset", -1) >= tail):
            e.pop("offset", None)
    return {
        "entries": entries,
        "next_cursor": next_cursor,
        # Only a page that ends at EOF has a live tail to resume from: an
        # older page ends mid-file, where nothing is provisional.
        "tail": tail if at_eof else None,
        "size": size,
    }


# --------------------------------------------------------------- live tail


def _pending_from(lines: List[Line], build: EntryBuilder, line_key: LineKey) -> int:
    """Index of the first line of the trailing *provisional* region.

    Everything from there on may still grow. A harness can write one message
    as several lines — Claude writes one per content block under one
    ``message.id`` — so a read that ends at EOF can be holding half a
    message; and the very last line of a file being appended to may be a
    partial write with no newline yet. For a keyed flavour the answer is the
    newest keyed span (found by taking the key of the last keyed line and
    scanning back to that span's first line).

    For an **unkeyed** flavour (codex, grok, pi, antigravity, copilot —
    ``line_key`` always ``None``, so every line here reads standalone), the
    keyed rule degenerates to "just the last line", which is wrong for a
    tool call: its result is always a separate, later raw line — often the
    next tick's read entirely — so settling the call as soon as it is read
    and then rendering its result as an orphan card once it finally arrives
    is #1310. And when call and result *do* land in the same read (a page
    load, or a tail read that caught up in one tick), building the whole
    window together pairs them correctly, but the naive "just the last
    line" split still cuts between them: the call goes into ``settled``
    without its result (rebuilt alone, so it renders unresolved) and the
    result goes into ``pending`` alone, which is exactly the orphan branch
    of :func:`_attach_result` — the call it belongs to isn't in that half's
    build.

    The fix widens the pending region backwards, one line at a time, for as
    long as the **suffix from there on, built on its own, still contains an
    orphan** (a standalone ``tool_result`` — the tell that some call it
    would pair with sits outside this window). That is the general form of
    "the oldest tool call without a result": it stops exactly at the oldest
    call whose result — found or not — needs to stay grouped with it, using
    the builder itself as the oracle rather than re-deriving each flavour's
    call/result grammar here. No orphan even at the last line (plain text,
    or every call in view already has its result) → same fallback as
    before, the last line alone.

    Never empty for a non-empty read, deliberately: the tail of a live file
    is provisional by definition, and re-reading one line per tick costs
    nothing — :func:`_tail`'s size pre-check skips even that while the file
    is unchanged. Holding the region back instead of settling it is what
    keeps an assistant message from rendering as two cards when its trailing
    text arrives a tick after its tool call.

    The mirror of :func:`_drop_partial_leading_message`, which does this job
    at a backwards window's leading edge.
    """
    if not lines:
        return 0
    keys = [line_key(raw) for _, raw in lines]
    last_keyed = next(
        (i for i in range(len(lines) - 1, -1, -1) if keys[i] is not None), None
    )
    if last_keyed is not None:
        key = keys[last_keyed]
        return next(i for i, k in enumerate(keys) if k == key)
    idx = len(lines) - 1
    while idx > 0 and any(e.get("kind") == "tool_result" for e in build(lines[idx:])):
        idx -= 1
    return idx


def _split_pending(
    lines: List[Line], build: EntryBuilder, line_key: LineKey
) -> Tuple[List[Entry], List[Entry], Optional[int]]:
    """``(settled, pending, tail)`` for a read that ends at EOF.

    ``settled`` entries are final — the client appends them and never touches
    them again. ``pending`` is the provisional tail (:func:`_pending_from`),
    which the client re-renders wholesale each time it changes. ``tail`` is
    the byte offset the next live read starts from, i.e. where ``pending``
    begins; ``None`` when there was nothing to read at all.

    The two halves are built separately, which is exactly why the split has
    to fall on a message boundary: a builder run over half a message would
    report half its text as a whole entry.

    **Offsets as render keys.** Both halves keep an offset on every kind,
    folded ones included — unlike a page's ``entries``, which keep the #985
    contract of turns only, since a turn is the one kind the copy-full-text
    route can re-read. Everything a live read returns is content the client
    may have to re-render, so each card needs a stable identity to carry an
    open disclosure across a rebuild. Nothing re-reads a folded entry by its
    offset; here it is an identity, not a cursor.
    """
    if not lines:
        return [], [], None
    idx = _pending_from(lines, build, line_key)
    settled = build(lines[:idx]) if idx else []
    pending = build(lines[idx:])
    return settled, pending, lines[idx][0]


def _tail(
    path: Path,
    after: int,
    size_seen: Optional[int],
    build: EntryBuilder,
    line_key: LineKey,
) -> Dict[str, Any]:
    """Everything appended to ``path`` since byte offset ``after``.

    The forward counterpart of :func:`_page` — the transcript cursor paged
    only backwards before #1050, so a live view had no way to ask for "just
    what is new" and had to refetch the newest page whole.

    ``after`` is a previous read's ``tail``, so it is always a line boundary
    *and* always outside a message span. ``size_seen`` is the file size that
    read observed: when the file has not grown since, this answers from a
    single ``stat`` without opening the file, which is what keeps an idle
    chat view's cost at roughly nothing per tick.

    ``reset`` asks the caller to reload the newest page instead of appending:
    the file was rotated or truncated under the cursor, or the gap is wider
    than :data:`REQUEST_BYTE_CAP` — a view that was hidden long enough to
    fall that far behind wants the newest turns, not a 2 MB replay of what it
    missed.
    """
    eof = path.stat().st_size
    unchanged = {
        "changed": False, "reset": False, "entries": [], "pending": [],
        "tail": after, "size": eof,
    }
    if after > eof or eof - after > REQUEST_BYTE_CAP:
        return {**unchanged, "changed": True, "reset": True}
    if size_seen is not None and size_seen == eof:
        return unchanged
    lines: List[Line] = []
    pos = after
    span = WINDOW_BYTES
    while pos < eof:
        more, new_pos = _read_lines_from(path, pos, span, eof)
        if not more and new_pos == pos:
            # A single record wider than the window — widen in place, as
            # `_page` does, rather than stepping over its tail.
            span *= 2
            if span > REQUEST_BYTE_CAP:
                break
            continue
        lines.extend(more)
        pos = new_pos
        span = WINDOW_BYTES
    settled, pending, tail = _split_pending(lines, build, line_key)
    if tail is None:
        # Nothing complete yet: the file grew by a partial line. Report the
        # size so the next tick's pre-check stays cheap — the line completes
        # (and the size grows again) or it never does, in which case there is
        # genuinely nothing to show.
        return unchanged
    return {
        "changed": True, "reset": False,
        "entries": settled, "pending": pending, "tail": tail, "size": eof,
    }


def _is_turn(entry: Entry) -> bool:
    return entry.get("kind") in CONVERSATION_KINDS and not entry.get("sidechain")


def _turns(entries: List[Entry]) -> int:
    return sum(1 for e in entries if _is_turn(e))


# ------------------------------------------------------ transcript images (#1265)
#
# An image block rides the transcript as base64. The page never carries the
# bytes: an entry lists where its images are, ``{"offset": <line offset>,
# "n": <index>}``, and the image route re-reads that one line and decodes
# block ``n`` on demand. ``n`` counts the line's image blocks in a fixed walk
# order (its content list, with a Claude ``tool_result``'s own content in
# place), so it is stable for as long as the line is. A block with no data
# keeps the ``[image]`` placeholder, as does every harness whose transcript
# records none.

IMAGE_LINE_CAP = 32 * 1024 * 1024      # a raw line holding an image, base64 and all
IMAGE_BYTE_CAP = 16 * 1024 * 1024      # one decoded image
_IMAGE_FLAVORS = frozenset({"claude", "pi"})
_IMAGE_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


def _sniff_image(data: bytes) -> Optional[str]:
    """The media type of an allowlisted image format, from its own bytes."""
    for magic, media in _IMAGE_MAGIC:
        if data.startswith(magic):
            return media
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def transcript_image(path: Path, offset: int, n: int, flavor: str) -> Optional[Tuple[bytes, str]]:
    """``(bytes, media type)`` of image ``n`` on the line starting at ``offset``.

    None when the flavour records no images, ``offset`` doesn't start a line
    (or the file moved on), the line is over :data:`IMAGE_LINE_CAP`, block
    ``n`` isn't there or carries no data, it doesn't decode, it is over
    :data:`IMAGE_BYTE_CAP`, or its bytes aren't an allowlisted image format.
    The declared media type is never trusted. Raises OSError on a read failure.
    """
    if flavor not in _IMAGE_FLAVORS or offset < 0 or n < 0:
        return None
    with path.open("rb") as fh:
        if offset > 0:
            fh.seek(offset - 1)
            if fh.read(1) != b"\n":
                return None
        raw = fh.read(IMAGE_LINE_CAP + 1)
    end = raw.find(b"\n")
    if end < 0:
        if len(raw) > IMAGE_LINE_CAP:
            return None
        end = len(raw)
    obj = _loads(raw[:end].decode("utf-8", "replace"))
    msg = obj.get("message") if obj and isinstance(obj.get("message"), dict) else {}
    blocks = _line_image_blocks(msg.get("content"))
    if n >= len(blocks):
        return None
    data = _image_data(blocks[n])
    if not data or len(data) > IMAGE_BYTE_CAP * 4 // 3 + 4:
        return None
    try:
        decoded = base64.b64decode(data, validate=False)
    except (binascii.Error, ValueError):
        return None
    media = _sniff_image(decoded)
    return (decoded, media) if media and len(decoded) <= IMAGE_BYTE_CAP else None


# How far a harness's own history can be trusted to mark a failed tool call
# (#1020). Third element of every :data:`FLAVORS` row so it cannot drift
# from the parser that produces the flag:
#
# ``reported``  every failed call is recorded structurally, so an unmarked
#               call did succeed.
# ``partial``   some failures are recorded and some are not, so an unmarked
#               call is "not known to have failed", not "fine".
# ``none``      nothing distinguishes a failed call from a working one.
#
# The client renders the difference: it marks what is known failed and says
# "outcome not recorded" for the rest of a `partial`/`none` flavour, rather
# than letting silence read as success. Each value below is a measurement,
# recorded on #1020 — not a guess from the harness's documentation.
TOOL_ERRORS_REPORTED = "reported"
TOOL_ERRORS_PARTIAL = "partial"
TOOL_ERRORS_NONE = "none"

# The line grammars this reader understands: flavour name → (entry builder,
# line key, tool-error fidelity). One table so a new harness is one row here
# plus its parser, rather than a branch in every dispatch site. The names are
# the endpoint's `_FLAVOR_BY_AGENT` values
# (app/webapp/routers/session_transcript.py).
FLAVORS: Dict[str, Tuple[EntryBuilder, LineKey, str]] = {
    # `is_error` on every `tool_result` block.
    "claude": (claude_entries, _claude_line_key, TOOL_ERRORS_REPORTED),
    # No success/error field at any level — see the reader's own comment; a
    # rejected `apply_patch` states so in its fixed tool message (#1356).
    "codex": (codex_entries, _codex_line_key, TOOL_ERRORS_PARTIAL),
    # `status: "failed"` on the `tool_call_update` that carries the result.
    "grok": (grok_entries, _grok_line_key, TOOL_ERRORS_REPORTED),
    # `isError` on every `toolResult` message.
    "pi": (pi_entries, _pi_line_key, TOOL_ERRORS_REPORTED),
    # `status: "ERROR"` catches a tool that could not run; a shell command
    # exiting non-zero is still `DONE`, with the code only in prose.
    "antigravity": (antigravity_entries, _antigravity_line_key, TOOL_ERRORS_PARTIAL),
    # `success: false` catches a tool-level failure and
    # `shellExecution.exitCode` a command-level one — but the latter key is
    # absent from most completions on disk, leaving those exits unrecorded.
    "copilot": (copilot_entries, _copilot_line_key, TOOL_ERRORS_PARTIAL),
}


def _flavor(flavor: str) -> Tuple[EntryBuilder, LineKey, str]:
    """The builder + line key + tool-error fidelity for ``flavor``,
    defaulting to Claude's — the same fallback the endpoint applies to a
    session with no agent field."""
    return FLAVORS.get(flavor, FLAVORS["claude"])


# ---------------------------------------------------------------- public


def transcript_page(
    path: Any,
    *,
    before: Optional[int] = None,
    limit: int = DEFAULT_LIMIT,
    flavor: str = "claude",
) -> Dict[str, Any]:
    """One page of typed entries from ``path``, newest-last.

    ``before`` is a byte offset (the previous page's ``next_cursor``; ``None``
    = the end of the file); ``limit`` is the maximum number of conversation
    turns (``user`` + ``assistant`` entries — roughly two per exchange).
    ``flavor`` picks the line grammar — see :data:`FLAVORS`. Raises
    ``OSError`` when the file can't be read — a distinct condition from "no
    transcript", which the caller establishes *before* calling.
    """
    build, line_key, tool_errors = _flavor(flavor)
    page = _page(Path(str(path)), before, limit, build, line_key)
    # A property of the harness, not of the page — but it travels with the
    # page because that is what the client has in hand when it decides
    # whether an unmarked tool call means "succeeded" or "nobody can tell".
    page["tool_errors"] = tool_errors
    return page


def transcript_tail(
    path: Any,
    *,
    after: int,
    size: Optional[int] = None,
    flavor: str = "claude",
) -> Dict[str, Any]:
    """Entries appended since byte offset ``after`` (#1050).

    ``after`` is the ``tail`` of the caller's last read — of the newest page
    from :func:`transcript_page`, or of the previous call to this. ``size``
    is the file size that read reported; passing it lets an unchanged file
    answer from one ``stat``, with no open and no parse.

    Returns ``changed`` (was there anything new), ``reset`` (the cursor no
    longer applies — reload the newest page), ``entries`` (settled, append
    them), ``pending`` (the provisional tail, re-render it wholesale), and
    the ``tail``/``size`` to pass to the next call. Raises ``OSError`` when
    the file can't be read, exactly as :func:`transcript_page` does.
    """
    build, line_key, tool_errors = _flavor(flavor)
    out = _tail(Path(str(path)), int(after), size, build, line_key)
    _inline_diffs(out["entries"])
    _inline_diffs(out["pending"])
    out["tool_errors"] = tool_errors
    return out
