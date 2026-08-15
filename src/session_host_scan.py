"""Pure PTY-output-protocol scanning — no session state (issue #753).

Split out of ``src/session_host.py`` (a `/codebase-audit` maintainability
finding): OSC window-title parsing, OSC 10/11/12 colour query/reply
stripping, DECSET 2004 bracketed-paste-mode tracking, and prompt-title
derivation from a cooked keystroke stream. Every function here is
stateless — any "carry" a caller needs across reads is an explicit
parameter/return value, never an attribute on ``self`` — so each is
directly unit-testable on its own, unlike the ``PtySession``/
``SessionManager`` lifecycle and IO code that stays in ``session_host.py``.
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple


def _parse_osc_title(buffer: str) -> Tuple[str, str]:
    """Extract OSC window-title sequences from a buffer.

    Returns (remaining_buffer, extracted_title_or_empty).
    Handles OSC 0 and OSC 2 sequences in both BEL and ST terminator forms.
    Strips ANSI/control chars and caps length.
    """
    extracted = ""
    remaining = buffer

    while True:
        # Look for OSC start: ESC ]
        start_idx = remaining.find("\x1b]")
        if start_idx == -1:
            break

        # Look for terminators: BEL (0x07) or ST (ESC \)
        bel_idx = remaining.find("\x07", start_idx)
        st_idx = remaining.find("\x1b\\", start_idx)

        # Determine which terminator comes first
        end_idx = -1
        term_len = 0
        if bel_idx != -1 and (st_idx == -1 or bel_idx < st_idx):
            end_idx = bel_idx
            term_len = 1
        elif st_idx != -1:
            end_idx = st_idx
            term_len = 2

        if end_idx == -1:
            # Incomplete sequence; keep in buffer for next chunk
            break

        try:
            # Extract the full sequence
            seq = remaining[start_idx : end_idx + term_len]
            # Parse: ESC ] <code> ; <text> <term>
            # Find the code (0 or 2)
            code_end = remaining.find(";", start_idx)
            if code_end != -1 and code_end < end_idx:
                code_part = remaining[start_idx + 2 : code_end].strip()
                if code_part in ("0", "2"):
                    text = remaining[code_end + 1 : end_idx]
                    # Strip ANSI/control chars
                    clean = "".join(c for c in text if ord(c) >= 32 or c in "\t")
                    clean = clean.strip()
                    if clean:
                        # Cap at 80 chars
                        extracted = clean[:80]
        except Exception:
            pass

        # Remove the processed sequence and continue
        remaining = remaining[: start_idx] + remaining[end_idx + term_len :]

    return remaining, extracted


# OSC colour query/reply leak (#270). Codex emits OSC 10/11/12 *queries*
# (``ESC]10;?``) at startup; something answers them and the reply
# (``ESC]10;rgb:…``) leaks as visible text on a fresh/dirty xterm. We strip
# both the query and reply forms of OSC 10/11/12 from the output stream at the
# read boundary so the leak is killed for fresh + reconnect, pc + phone. We
# match TIGHTLY — only OSC 10/11/12 with a ``?`` query or an ``rgb:``/``#``
# colour payload — so we never touch title OSC (0/1/2), hyperlink OSC 8,
# clipboard OSC 52, or any CSI.
#
# Terminator handling differs by form (empirical, from a live pywinpty
# capture of Codex startup): the ``rgb:``/``#`` *reply* always carries a BEL
# (\x07) or ST (ESC \) terminator. The ``?`` *query*, however, is emitted by
# Codex **unterminated and back-to-back** — ``ESC]10;?ESC]11;?`` — each query
# implicitly ended by the next ESC, with no BEL/ST at all. The original #270
# pattern required a terminator on every form, so it silently MISSED these
# bare queries: they reached xterm.js, which answered with ``ESC]10;rgb:…``,
# and that reply leaked. So the terminator is OPTIONAL for the ``?`` query
# (a bare ``ESC]1X;?`` is already a complete colour query) and stays REQUIRED
# for the rgb/# reply (its payload needs a clear end so we don't over-strip).
_OSC_COLOR_RE = re.compile(
    r"\x1b\]1[012];"                                   # OSC 10 | 11 | 12 ;
    r"(?:"
    r"\?(?:\x07|\x1b\\)?"                              # query: terminator OPTIONAL
    r"|(?:rgb:[0-9A-Fa-f/]+|#[0-9A-Fa-f]+)(?:\x07|\x1b\\)"  # reply: term REQUIRED
    r")"
)
# A trailing fragment we must hold back to the next read: either the *start*
# of a colour OSC opened but not yet terminated, OR a complete-but-bare ``?``
# query whose BEL/ST terminator may still land in the next chunk (so we don't
# strip the query here and orphan its terminator into the next chunk). Anchored
# to the END, and tight — it only matches valid colour-OSC prefixes (``?`` /
# ``rgb:…`` / ``#…``), never arbitrary trailing text, because it is consulted
# BEFORE the strip: a loose ``[^\x07\x1b]*`` here would wrongly hold real text
# that merely follows a query in the same chunk.
_OSC_COLOR_PARTIAL_RE = re.compile(
    r"\x1b(?:\](?:1(?:[012](?:;(?:"
    r"\?\x1b?"                                  # query (+ pending ST ESC)
    r"|r(?:g(?:b(?::[0-9A-Fa-f/]*\x1b?)?)?)?"   # rgb:… reply in progress
    r"|#[0-9A-Fa-f]*\x1b?"                      # #… reply in progress
    r")?)?)?)?)?\Z"
)
# Cap on the carried partial: if an unterminated ESC] grows past this without a
# terminator it's not really a colour query — flush it as-is so a stray ESC can
# never wedge the stream.
_OSC_CARRY_MAX = 64


def _strip_color_osc(chunk: str, carry: str) -> Tuple[str, str]:
    """Strip OSC 10/11/12 colour query/reply sequences from ``chunk``.

    Stateful across reads: ``carry`` is any trailing partial colour-OSC
    fragment held back from the previous chunk. Returns
    ``(clean_output, new_carry)`` — ``clean_output`` is safe to emit now,
    ``new_carry`` is the partial fragment to prepend to the next chunk.

    Fast path: a chunk with no ESC at all and an empty carry can't contain a
    colour OSC (nor the start of one straddling the boundary), so it passes
    through untouched.
    """
    if not carry and "\x1b" not in chunk:
        return chunk, ""

    data = carry + chunk

    # Hold back a trailing partial/bare colour-OSC BEFORE stripping, so a
    # sequence split across the read boundary — or a bare ``?`` query whose
    # terminator lands in the next chunk — is caught whole next read instead
    # of being half-stripped (which would orphan a BEL/ST into the next
    # chunk). Bound it: a fragment past the cap is flushed rather than carried
    # forever, so a stray ESC can't wedge the stream.
    m = _OSC_COLOR_PARTIAL_RE.search(data)
    if m and (len(data) - m.start()) <= _OSC_CARRY_MAX:
        head, new_carry = data[: m.start()], data[m.start() :]
    else:
        head, new_carry = data, ""
    return _OSC_COLOR_RE.sub("", head), new_carry


# Bracketed-paste mode tracking (DECSET 2004, issue #611). The client-side
# framePaste() in terminal-compose.js only wraps a WS-sent payload in
# \x1b[200~ / \x1b[201~ when xterm's own term.modes.bracketedPasteMode is on
# — sending the literal markers to an agent that never asked for them lands
# as garbage, not a paste. A server-initiated write (the HTTP /input path)
# has no xterm to ask, so this tracks the same signal directly off the PTY's
# *output* stream: the agent announces the mode itself via DECSET 2004
# (`\x1b[?2004h` enable / `\x1b[?2004l` disable). Passive only — never
# strips these sequences from the stream, since the client's own terminal
# still needs to see them to keep its own state in sync.
_BRACKETED_PASTE_RE = re.compile(r"\x1b\[\?2004([hl])")
# A trailing fragment that could be the start of `\x1b[?2004h`/`l` split
# across a read boundary — held back and re-checked with the next chunk
# prepended, so a straddling sequence is never missed. Anchored to the end;
# tight to the seven-char prefix so it can't swallow unrelated trailing text.
_BRACKETED_PASTE_PARTIAL_RE = re.compile(r"\x1b(?:\[(?:\?(?:2(?:0(?:0(?:4)?)?)?)?)?)?\Z")
_BRACKETED_PASTE_CARRY_MAX = 8


def _scan_bracketed_paste_mode(chunk: str, carry: str) -> Tuple[Optional[bool], str]:
    """Track the PTY app's DECSET 2004 state from its *output*.

    Returns ``(latest_or_None, new_carry)``: ``latest`` is the last
    enable(``True``)/disable(``False``) seen in this chunk, or ``None`` if
    the chunk carried no (complete) DECSET 2004 sequence. ``new_carry`` is a
    possibly-split trailing sequence to prepend to the next chunk.
    """
    if not carry and "\x1b" not in chunk:
        return None, ""
    data = carry + chunk
    latest: Optional[bool] = None
    for m in _BRACKETED_PASTE_RE.finditer(data):
        latest = m.group(1) == "h"
    tail = data[-_BRACKETED_PASTE_CARRY_MAX:]
    m = _BRACKETED_PASTE_PARTIAL_RE.search(tail)
    new_carry = tail[m.start():] if m else ""
    return latest, new_carry


def _cook_input_line(raw: str) -> str:
    """Reduce a raw keystroke stream to the visible text the user typed.

    The PTY input path carries raw bytes — printable characters, backspaces,
    arrow-key escape sequences, bracketed-paste markers. To title a session
    from its first prompt (issue #266) we replay that stream into the text it
    produces: apply backspace (DEL/BS), drop ESC-introduced control sequences
    (CSI ``ESC [ … final`` — which also covers ``ESC[200~``/``ESC[201~`` paste
    markers — and OSC ``ESC ] … BEL/ST``), and keep the printable remainder.
    Best-effort, not a full terminal emulator — good enough for a title.
    """
    out: List[str] = []
    i = 0
    n = len(raw)
    while i < n:
        ch = raw[i]
        if ch == "\x1b":
            i += 1
            if i < n and raw[i] == "[":  # CSI: run to a final byte 0x40-0x7E
                i += 1
                while i < n and not ("\x40" <= raw[i] <= "\x7e"):
                    i += 1
                i += 1  # consume the final byte
            elif i < n and raw[i] == "]":  # OSC: run to BEL or ST
                i += 1
                while i < n and raw[i] not in ("\x07", "\x1b"):
                    i += 1
                if i < n and raw[i] == "\x07":
                    i += 1
            else:  # ESC + single char (or a lone trailing ESC)
                i += 1
            continue
        if ch in ("\x7f", "\x08"):  # DEL / BS — erase the last visible char
            if out:
                out.pop()
            i += 1
            continue
        if ord(ch) >= 32:
            out.append(ch)
        i += 1
    return "".join(out)


# First-prompt session title (issue #266). Only Claude Code emits a genuine
# per-conversation OSC title; Antigravity/Copilot emit none and Codex/Pi emit
# only the project folder, so for those agents we derive a human title from
# the first submitted prompt. Cap how long / how many words the derived
# title runs (the un-submitted-input buffering bound, ``_PROMPT_TITLE_BUF_MAX``,
# is about session *state* and stays in ``session_host.py``).
_PROMPT_TITLE_MAX_CHARS = 48
_PROMPT_TITLE_MAX_WORDS = 6


def _derive_prompt_title(text: str) -> str:
    """Derive a short, human-readable session title from a first prompt.

    Deterministic and offline (no LLM, issue #266): collapse whitespace, drop
    control chars, take the leading few words, cap the length. ``text`` should
    already be the cooked visible line from :func:`_cook_input_line`.
    """
    # Collapse all whitespace runs (incl. tabs/newlines) to single spaces
    # first — so whitespace separators survive — then drop residual control
    # chars, which can only sit inside a token at this point.
    clean = "".join(c for c in " ".join(text.split()) if ord(c) >= 32)
    if not clean:
        return ""
    title = " ".join(clean.split(" ")[:_PROMPT_TITLE_MAX_WORDS])
    if len(title) > _PROMPT_TITLE_MAX_CHARS:
        title = title[:_PROMPT_TITLE_MAX_CHARS].rstrip()
    return title
