"""Coding-tab transcript: locating a live session's transcript file per harness."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Tuple

from tests.unit.transcript._support import _BRIDGE, _CONV, _RESUMED, _registry_entry


# ----------------------------------------- antigravity source correlation


def _agy_tree(root: Path, folder: str, uuid: str, *, full: bool = True) -> Path:
    """An Antigravity home with one conversation logged for ``folder``."""
    (root / "cache").mkdir(parents=True, exist_ok=True)
    (root / "cache" / "last_conversations.json").write_text(
        json.dumps({folder: uuid}), encoding="utf-8"
    )
    logs = root / "brain" / uuid / ".system_generated" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    written = logs / ("transcript_full.jsonl" if full else "transcript.jsonl")
    written.write_text("{}\n", encoding="utf-8")
    return written


def _agy_session(**extra: Any) -> Dict[str, Any]:
    row = {"session_id": "s1", "agent": "antigravity", "alive": True,
           "project_dir": r"E:\work\project", "started_at": 1_000_000.0}
    row.update(extra)
    return row


def test_find_antigravity_transcript_uses_the_newest_conversation_cache(
    tmp_path: Path, monkeypatch
):
    """Antigravity links its conversation to nothing the launcher knows, and
    omits `conversationId` on a conversation's first history row — so the
    correlation is its own ``workspace → newest conversation`` cache, which
    *is* written the moment the first prompt lands (#1014 probe)."""
    from src import transcript_locate

    uuid = "595bd0ae-ec7f-41dd-a242-2fe7517ee288"
    wanted = _agy_tree(tmp_path, r"E:\work\project", uuid)
    monkeypatch.setattr(transcript_locate, "_AGY_ROOT", tmp_path)
    session = _agy_session()
    assert transcript_locate.find_antigravity_transcript(session, [session]) == wanted
    # The path separator and case of the cache key need not match the
    # session's own spelling.
    assert transcript_locate.find_antigravity_transcript(
        _agy_session(project_dir="e:/work/project/"), []
    ) == wanted
    # A different folder is not this session's conversation.
    assert transcript_locate.find_antigravity_transcript(
        _agy_session(project_dir=r"E:\work\other"), []
    ) is None


def test_find_antigravity_transcript_falls_back_to_the_flat_file(tmp_path: Path, monkeypatch):
    """``transcript_full.jsonl`` is preferred — it alone can serve
    ``entry_full_text`` uncapped — but 55 of 265 conversations on this box
    have only the flat file, so its absence is a fallback, not an error."""
    from src import transcript_locate

    uuid = "595bd0ae-ec7f-41dd-a242-2fe7517ee288"
    flat = _agy_tree(tmp_path, r"E:\work\project", uuid, full=False)
    monkeypatch.setattr(transcript_locate, "_AGY_ROOT", tmp_path)
    assert transcript_locate.find_antigravity_transcript(_agy_session(), []) == flat
    full = flat.with_name("transcript_full.jsonl")
    full.write_text("{}\n", encoding="utf-8")
    assert transcript_locate.find_antigravity_transcript(_agy_session(), []) == full


def test_find_antigravity_transcript_fails_safe_on_ambiguity_and_staleness(
    tmp_path: Path, monkeypatch
):
    """The cache answers "the newest conversation in this folder", not "this
    session's" — so two guards keep it from showing another session's text,
    the rule ``find_codex_transcript`` follows.
    """
    from src import transcript_locate

    uuid = "595bd0ae-ec7f-41dd-a242-2fe7517ee288"
    wanted = _agy_tree(tmp_path, r"E:\work\project", uuid)
    monkeypatch.setattr(transcript_locate, "_AGY_ROOT", tmp_path)

    # A second live Antigravity session in the same folder: the cache cannot
    # say which of the two the uuid belongs to.
    sibling = _agy_session(session_id="s2")
    assert transcript_locate.find_antigravity_transcript(_agy_session(), [sibling]) is None
    # Not a sibling: a dead one, another agent, another folder.
    for other in (
        _agy_session(session_id="s2", alive=False),
        _agy_session(session_id="s2", agent="claude"),
        _agy_session(session_id="s2", project_dir=r"E:\work\other"),
    ):
        assert transcript_locate.find_antigravity_transcript(_agy_session(), [other]) == wanted

    # Nothing written since this session started: the cache is pointing at
    # whatever ran in the folder before it, and there is no transcript yet.
    import os

    stamp = 1_000_000.0 - transcript_locate._AGY_MTIME_SLOP_SECONDS - 1
    os.utime(wanted, (stamp, stamp))
    assert transcript_locate.find_antigravity_transcript(_agy_session(), []) is None

    # A session with no usable launch time, and a cache value that is not a
    # conversation uuid, are both refused before any path is built.
    os.utime(wanted, (2_000_000.0, 2_000_000.0))
    assert transcript_locate.find_antigravity_transcript(
        _agy_session(started_at="not a time"), []
    ) is None
    (tmp_path / "cache" / "last_conversations.json").write_text(
        json.dumps({r"E:\work\project": "../../../etc"}), encoding="utf-8"
    )
    assert transcript_locate.find_antigravity_transcript(_agy_session(), []) is None


# --------------------------------------------------- pi source correlation


def test_find_pi_transcript_matches_the_state_row_key(tmp_path: Path, monkeypatch):
    """Pi's state row is keyed by Pi's own session uuid and carries no
    ``transcript_path``; the file is named after that same uuid (#1013)."""
    from src import transcript_locate

    sid = "01a0b0e2-84d4-787a-ae98-eef7f26179bc"
    folder = tmp_path / "--E--work-project--"
    folder.mkdir()
    wanted = folder / f"2026-09-17T19-40-30-292Z_{sid}.jsonl"
    wanted.write_text("{}\n", encoding="utf-8")
    (folder / "2026-09-06T12-14-59-246Z_01a076a4-aeae-789e-80fb-39494a6844c9.jsonl").write_text(
        "{}\n", encoding="utf-8"
    )
    monkeypatch.setattr(transcript_locate, "_PI_SESSIONS_DIR", tmp_path)
    assert transcript_locate.find_pi_transcript(sid) == wanted
    assert transcript_locate.find_pi_transcript("01a076a4-0000-0000-0000-000000000000") is None


def test_find_pi_transcript_refuses_ambiguity_and_glob_syntax(tmp_path: Path, monkeypatch):
    """Fail safe, the rule ``find_codex_transcript`` follows: anything but
    exactly one match answers ``None`` so the route says "no transcript"
    rather than risking a neighbouring session's text. A key carrying glob
    metacharacters is refused before it can match anything at all."""
    from src import transcript_locate

    sid = "01a0b0e2-84d4-787a-ae98-eef7f26179bc"
    for name in ("--E--work-a--", "--E--work-b--"):
        folder = tmp_path / name
        folder.mkdir()
        (folder / f"2026-09-17T19-40-30-292Z_{sid}.jsonl").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(transcript_locate, "_PI_SESSIONS_DIR", tmp_path)
    assert transcript_locate.find_pi_transcript(sid) is None      # two candidates
    assert transcript_locate.find_pi_transcript("*") is None      # would match every file
    assert transcript_locate.find_pi_transcript("") is None
    assert transcript_locate.find_pi_transcript("../../etc") is None


# -------------------------------------------- copilot source correlation


def _copilot_session_dir(root: Path, folder: str, cwd: str, created: str,
                       *, events: bool = True) -> Path:
    """One Copilot session folder, as the harness lays it out at launch."""
    session = root / folder
    session.mkdir(parents=True, exist_ok=True)
    (session / "workspace.yaml").write_text(
        "id: {0}\ncwd: {1}\ngit_root: {1}\nclient_name: github/cli\n"
        "created_at: {2}\nupdated_at: {2}\n".format(folder, cwd, created),
        encoding="utf-8",
    )
    events_path = session / "events.jsonl"
    if events:
        events_path.write_text("{}\n", encoding="utf-8")
    return events_path


def _copilot_session(**extra: Any) -> Dict[str, Any]:
    # 2026-09-17T21:57:30.465Z, the capture's own launch instant.
    row = {"session_id": "s1", "agent": "copilot", "alive": True,
           "project_dir": r"E:\work\project", "started_at": 1_789_682_250.465}
    row.update(extra)
    return row


def test_find_copilot_transcript_matches_cwd_and_launch_window(tmp_path: Path, monkeypatch):
    """Copilot writes the launcher nothing — no sessions-state row, and its
    own log never contains the launcher's session id — so the correlation is
    its ``workspace.yaml`` sidecar: this folder's ``cwd``, created inside the
    launch window. Measured on two real launches: 3.71 s and 3.72 s after
    ``started_at`` (#1015 capture)."""
    from src import transcript_locate

    wanted = _copilot_session_dir(
        tmp_path, "79c18133-52bd-4f8f-9260-b33c853158be",
        r"E:\work\project", "2026-09-17T21:57:34.176Z",
    )
    monkeypatch.setattr(transcript_locate, "_COPILOT_STATE_DIR", tmp_path)
    assert transcript_locate.find_copilot_transcript(_copilot_session()) == wanted
    # Neither the separator nor the case of the harness's own spelling has
    # to match the session's.
    assert transcript_locate.find_copilot_transcript(
        _copilot_session(project_dir="e:/work/project/")
    ) == wanted
    # A different folder is not this session's conversation.
    assert transcript_locate.find_copilot_transcript(
        _copilot_session(project_dir=r"E:\work\other")
    ) is None
    # A session launched long before this folder existed is not its owner.
    assert transcript_locate.find_copilot_transcript(
        _copilot_session(started_at=1_789_600_000.0)
    ) is None
    # Nor one launched *after* it: the folder is created at launch, so a
    # later session's folder is a different one.
    assert transcript_locate.find_copilot_transcript(
        _copilot_session(started_at=1_789_682_400.0)
    ) is None


def test_find_copilot_transcript_refuses_two_candidates_in_one_window(
    tmp_path: Path, monkeypatch
):
    """Two sessions started in one folder inside the window are ambiguous by
    construction — the sidecar says which folder, never which session — so
    both refuse rather than risk showing the other's conversation, the rule
    ``find_codex_transcript`` follows."""
    from src import transcript_locate

    _copilot_session_dir(tmp_path, "aaaaaaaa-0000-0000-0000-000000000001",
                       r"E:\work\project", "2026-09-17T21:57:34.176Z")
    monkeypatch.setattr(transcript_locate, "_COPILOT_STATE_DIR", tmp_path)
    assert transcript_locate.find_copilot_transcript(_copilot_session()) is not None
    # A second launch in the same folder, 20 s later — still inside the
    # window, so neither folder can be claimed.
    _copilot_session_dir(tmp_path, "aaaaaaaa-0000-0000-0000-000000000002",
                       r"E:\work\project", "2026-09-17T21:57:54.176Z")
    assert transcript_locate.find_copilot_transcript(_copilot_session()) is None
    # A same-folder session outside the window does not make the first
    # ambiguous — which is what keeps the ordinary case answerable.
    for stale in tmp_path.glob("aaaaaaaa-0000-0000-0000-000000000002/*"):
        stale.unlink()
    _copilot_session_dir(tmp_path, "aaaaaaaa-0000-0000-0000-000000000003",
                       r"E:\work\project", "2026-09-17T22:05:00.000Z")
    assert transcript_locate.find_copilot_transcript(_copilot_session()) is not None


def test_find_copilot_transcript_needs_an_events_file(tmp_path: Path, monkeypatch):
    """The folder and its sidecar are written at launch, ``events.jsonl``
    only at the first prompt — so a session nobody has typed into has no
    transcript, and neither do the 28 of 71 older sessions here that kept a
    ``session.db`` instead."""
    from src import transcript_locate

    monkeypatch.setattr(transcript_locate, "_COPILOT_STATE_DIR", tmp_path)
    events = _copilot_session_dir(
        tmp_path, "79c18133-52bd-4f8f-9260-b33c853158be",
        r"E:\work\project", "2026-09-17T21:57:34.176Z", events=False,
    )
    assert transcript_locate.find_copilot_transcript(_copilot_session()) is None
    events.write_text("{}\n", encoding="utf-8")
    assert transcript_locate.find_copilot_transcript(_copilot_session()) == events


def test_find_copilot_transcript_survives_a_damaged_sidecar(tmp_path: Path, monkeypatch):
    """A folder whose ``workspace.yaml`` is missing, unparseable or lacks the
    two keys matches nothing — it must not raise, and must not swallow the
    real match sitting beside it."""
    from src import transcript_locate

    monkeypatch.setattr(transcript_locate, "_COPILOT_STATE_DIR", tmp_path)
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "workspace.yaml").write_text(
        "cwd: E:\\work\\project\ncreated_at: not-a-timestamp\n", encoding="utf-8"
    )
    (tmp_path / "keyless").mkdir()
    (tmp_path / "keyless" / "workspace.yaml").write_text("id: x\n", encoding="utf-8")
    assert transcript_locate.find_copilot_transcript(_copilot_session()) is None
    wanted = _copilot_session_dir(
        tmp_path, "79c18133-52bd-4f8f-9260-b33c853158be",
        r"E:\work\project", "2026-09-17T21:57:34.176Z",
    )
    assert transcript_locate.find_copilot_transcript(_copilot_session()) == wanted


def test_find_copilot_transcript_needs_a_dir_and_a_start(tmp_path: Path, monkeypatch):
    """A session row missing either half of the correlation answers None
    rather than scanning for a best guess."""
    from src import transcript_locate

    monkeypatch.setattr(transcript_locate, "_COPILOT_STATE_DIR", tmp_path)
    _copilot_session_dir(tmp_path, "79c18133-52bd-4f8f-9260-b33c853158be",
                       r"E:\work\project", "2026-09-17T21:57:34.176Z")
    assert transcript_locate.find_copilot_transcript(_copilot_session(project_dir="")) is None
    assert transcript_locate.find_copilot_transcript(_copilot_session(started_at=None)) is None
    # An ISO `started_at` (a row read back from JSON state) works too.
    assert transcript_locate.find_copilot_transcript(
        _copilot_session(started_at="2026-09-17T21:57:30.465Z")
    ) is not None


# -------------------------------------------- claude source correlation


def _claude_folder(root: Path, name: str, *files: Tuple[str, float]) -> Path:
    """One ``~/.claude/projects`` folder with conversations at fixed mtimes."""
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    for stem, written in files:
        path = folder / (stem + ".jsonl")
        path.write_text("{}\n", encoding="utf-8")
        os.utime(path, (written, written))
    return folder


def _claude_session(**extra: Any) -> Dict[str, Any]:
    row = {"session_id": "s1", "agent": "claude", "alive": True,
           "project_dir": r"E:\work\project", "started_at": 1_000_000.0}
    row.update(extra)
    return row


def test_resolve_claude_transcript_takes_the_newest_in_the_cwd_folder(
    tmp_path: Path, monkeypatch
):
    """The fallback for a live session the hook left no row for (#1023):
    Claude files a conversation under the cwd with every non-alphanumeric
    character replaced by `-`, and the newest one there is this session's."""
    from src import transcript_locate

    monkeypatch.setattr(transcript_locate, "_CLAUDE_PROJECTS_DIR", tmp_path)
    folder = _claude_folder(
        tmp_path, "E--work-project", ("old", 1_000_100.0), ("new", 1_000_200.0)
    )
    session = _claude_session()
    assert transcript_locate.resolve_claude_transcript(session, [session])[0] == folder / "new.jsonl"
    # The session's own spelling of the directory need not match the folder's.
    assert transcript_locate.resolve_claude_transcript(
        _claude_session(project_dir="e:/work/project/"), []
    )[0] == folder / "new.jsonl"
    # A different working directory is a different session's conversation.
    assert transcript_locate.resolve_claude_transcript(
        _claude_session(project_dir=r"E:\work\other"), []
    )[0] is None


def test_resolve_claude_transcript_matches_a_lowercased_legacy_folder(
    tmp_path: Path, monkeypatch
):
    """Older Claude Code versions lowercased the folder name as well as
    replacing the separators — 4 of the 107 folders on this box that record
    their own cwd are spelled that way, so the lookup is case-insensitive
    rather than one constructed name."""
    from src import transcript_locate

    monkeypatch.setattr(transcript_locate, "_CLAUDE_PROJECTS_DIR", tmp_path)
    folder = _claude_folder(tmp_path, "e--work-project", ("conv", 1_000_100.0))
    assert transcript_locate.resolve_claude_transcript(
        _claude_session(), []
    )[0] == folder / "conv.jsonl"


def test_resolve_claude_transcript_fails_safe_on_ambiguity_and_staleness(
    tmp_path: Path, monkeypatch
):
    """Two guards, both answering None rather than a confident wrong file:
    a second live Claude session in the same folder makes "newest here"
    meaningless (#537), and a folder untouched since this session started
    holds only what ran there before it."""
    from src import transcript_locate

    monkeypatch.setattr(transcript_locate, "_CLAUDE_PROJECTS_DIR", tmp_path)
    _claude_folder(tmp_path, "E--work-project", ("conv", 1_000_100.0))
    sibling = _claude_session(session_id="s2")
    assert transcript_locate.resolve_claude_transcript(_claude_session(), [sibling])[0] is None
    # A dead sibling, one in another folder, or another agent's, all leave
    # exactly one live claimant here.
    for other in (
        _claude_session(session_id="s2", alive=False),
        _claude_session(session_id="s2", project_dir=r"E:\work\other"),
        _claude_session(session_id="s2", agent="codex"),
    ):
        assert transcript_locate.resolve_claude_transcript(_claude_session(), [other])[0] is not None
    # Nothing written since this session started: refuse, and "no transcript
    # yet" is also the honest answer.
    assert transcript_locate.resolve_claude_transcript(
        _claude_session(started_at=1_000_200.0), []
    )[0] is None
    # ...but only outside the slop that covers spawn-to-first-write.
    assert transcript_locate.resolve_claude_transcript(
        _claude_session(started_at=1_000_140.0), []
    )[0] is not None


def test_resolve_claude_transcript_keeps_its_two_guard_shape_for_transcript(
    tmp_path: Path, monkeypatch
):
    """#1034 criterion 4. The disproof is opt-in, and ``/transcript`` does
    not opt in.

    ``resolve_claude_transcript`` is shared verbatim by the Board drawer and
    ``/transcript`` (#1023), so #1034 parameterised it rather than forking
    it. This pins the un-opted-in shape: a PTY title that flatly disagrees
    with the conversation's declared name still resolves, because on that
    route the scan has no competing source to outrank — refusing would
    turn a rough answer into no answer at all, and its ``source``
    vocabulary would have to grow a case it never had. The drawer's opted-in
    refusal is pinned in ``tests/api/board/test_exchange_sources.py``.
    """
    from src import transcript_locate

    monkeypatch.setattr(transcript_locate, "_CLAUDE_PROJECTS_DIR", tmp_path)
    folder = _claude_folder(tmp_path, "E--work-project", ("conv", 1_000_100.0))
    (folder / "conv.jsonl").write_text(
        json.dumps({"type": "ai-title", "aiTitle": "A totally different name"})
        + "\n",
        encoding="utf-8",
    )
    session = _claude_session(live_title="◐ Issue 1034")

    assert transcript_locate.resolve_claude_transcript(
        session, [session]
    )[0] == folder / "conv.jsonl"


def test_resolve_claude_transcript_needs_a_dir_a_start_and_a_folder(
    tmp_path: Path, monkeypatch
):
    """Half a correlation, an empty folder or no projects directory at all
    answer None rather than scanning for a best guess."""
    from src import transcript_locate

    monkeypatch.setattr(transcript_locate, "_CLAUDE_PROJECTS_DIR", tmp_path)
    _claude_folder(tmp_path, "E--work-project", ("conv", 1_000_100.0))
    assert transcript_locate.resolve_claude_transcript(_claude_session(project_dir=""), [])[0] is None
    assert transcript_locate.resolve_claude_transcript(_claude_session(started_at=None), [])[0] is None
    # An ISO `started_at` (a row read back from JSON state) works too.
    assert transcript_locate.resolve_claude_transcript(
        _claude_session(started_at="1970-01-12T13:46:40Z"), []
    )[0] is not None
    _claude_folder(tmp_path, "E--work-empty")
    assert transcript_locate.resolve_claude_transcript(
        _claude_session(project_dir=r"E:\work\empty"), []
    )[0] is None
    monkeypatch.setattr(transcript_locate, "_CLAUDE_PROJECTS_DIR", tmp_path / "gone")
    assert transcript_locate.resolve_claude_transcript(_claude_session(), [])[0] is None


_OTHER = "0c7a1d2e-9b3f-4e1a-8c6d-5f4e3d2c1b0a"


def test_resumed_claude_session_reads_its_conversation_before_the_first_prompt(
    tmp_path: Path, monkeypatch
):
    """#1155: a `--resume <id>` launch is a new process with a fresh
    `started_at`, and until its first prompt nothing moves the conversation's
    mtime past it (and in a busy folder another live session shares it), so
    both of the scan's guards refuse. The id names the file exactly; Claude
    Code appends a resumed turn to that same `<id>.jsonl` (probed on
    2.1.280), so it is this session's conversation from spawn onwards."""
    from src import transcript_locate

    monkeypatch.setattr(transcript_locate, "_CLAUDE_PROJECTS_DIR", tmp_path)
    folder = _claude_folder(
        tmp_path, "E--work-project",
        (_RESUMED, 1_000_100.0), (_OTHER, 1_000_150.0),
    )
    fresh_start = {"started_at": 2_000_000.0}
    resumed = _claude_session(
        flags=f"--resume {_RESUMED} --model opus", **fresh_start
    )
    neighbour = _claude_session(session_id="s2", **fresh_start)

    # Stale folder, alone in it: the mtime guard refused; the id answers.
    assert transcript_locate.resolve_claude_transcript(resumed, [resumed]) == (
        folder / f"{_RESUMED}.jsonl", "resume_id"
    )
    # Busy folder: the folder guard refused; the id still answers, and it is
    # the resumed conversation, not the folder's newest (_OTHER).
    assert transcript_locate.resolve_claude_transcript(
        resumed, [resumed, neighbour]
    )[0] == folder / f"{_RESUMED}.jsonl"
    # The neighbour is fresh and never borrows the resumed session's file.
    assert transcript_locate.resolve_claude_transcript(
        neighbour, [resumed, neighbour]
    ) == (None, "folder_shared")


def test_fresh_and_bare_resume_sessions_keep_both_guards(
    tmp_path: Path, monkeypatch
):
    """#1155's safety half. Nothing is loosened for a session with no id: a
    fresh launch in a folder of older conversations, and a bare `--resume`
    (the picker, whose choice nothing on the session records), both still
    answer no_transcript until they write, and say which guard refused."""
    from src import transcript_locate

    monkeypatch.setattr(transcript_locate, "_CLAUDE_PROJECTS_DIR", tmp_path)
    _claude_folder(tmp_path, "E--work-project", (_RESUMED, 1_000_100.0))
    for flags in ("--model opus", "--resume --model opus", "--resume"):
        session = _claude_session(flags=flags, started_at=2_000_000.0)
        assert transcript_locate.resolve_claude_transcript(session, [session]) == (
            None, "not_written_since_start"
        ), flags
    # An id with no file in this cwd's folder is not a correlation either.
    elsewhere = _claude_session(
        flags=f"--resume {_OTHER}", started_at=2_000_000.0
    )
    assert transcript_locate.resolve_claude_transcript(elsewhere, [elsewhere])[0] is None


def test_resumed_session_that_moved_on_is_refused_by_its_title(
    tmp_path: Path, monkeypatch
):
    """A `/resume` inside the session moves it to another conversation while
    its launch flags keep the old id. The live-title disproof (#1034) covers
    that: a title that genuinely conflicts with the resumed file's own name
    refuses it rather than showing the conversation the user left."""
    from src import transcript_locate

    monkeypatch.setattr(transcript_locate, "_CLAUDE_PROJECTS_DIR", tmp_path)
    folder = _claude_folder(tmp_path, "E--work-project", (_RESUMED, 1_000_100.0))
    path = folder / f"{_RESUMED}.jsonl"
    path.write_text(
        json.dumps({"type": "ai-title", "aiTitle": "Fix the backup job"}) + "\n",
        encoding="utf-8",
    )
    os.utime(path, (1_000_100.0, 1_000_100.0))
    base = {"flags": f"--resume {_RESUMED}", "started_at": 2_000_000.0}

    moved = _claude_session(live_title="✳ Plan the garden", **base)
    refused_path, why = transcript_locate.resolve_claude_transcript(moved, [moved])
    assert refused_path is None
    assert why == "not_written_since_start; resume_id title_disproved"
    # Agreeing, or no title at all (a detached session), still answers.
    for title in ("✳ Fix the backup job", ""):
        same = _claude_session(live_title=title, **base)
        assert transcript_locate.resolve_claude_transcript(same, [same])[0] == path, title


def _channel_session(key: str, **extra: Any) -> Dict[str, Any]:
    return _claude_session(
        session_id=f"s-{key}", started_at=2_000_000.0,
        web_url=f"https://claude.ai/code/{_BRIDGE[key]}", **extra,
    )


def _channel_world(tmp_path: Path, monkeypatch, keys: str = "abc") -> Tuple[Path, List[Dict[str, Any]]]:
    """Three live channel sessions in one folder, none with a hook row and
    every conversation older than the launch, so each guard of the folder
    scan refuses all three."""
    from src import transcript_locate

    projects, registry = tmp_path / "projects", tmp_path / "sessions"
    monkeypatch.setattr(transcript_locate, "_CLAUDE_PROJECTS_DIR", projects)
    monkeypatch.setattr(transcript_locate, "_CLAUDE_SESSIONS_DIR", registry)
    folder = _claude_folder(projects, "E--work-project", *((_CONV[k], 1_000_100.0 + i) for i, k in enumerate(keys)))
    for pid, key in enumerate(keys, start=100):
        _registry_entry(registry, pid, key)
    return folder, [_channel_session(k) for k in keys]


def test_channel_sessions_in_a_shared_folder_each_read_their_own_conversation(
    tmp_path: Path, monkeypatch
):
    """#1393: three live Telegram channel sessions share one cwd and none has
    a hook row, so the folder scan refuses all of them. Each session's own
    Remote Control link names its Claude process in Claude Code's per-pid
    registry, and that record names the conversation, so every one resolves
    to its own file and never to a neighbour's."""
    from src import transcript_locate

    folder, live = _channel_world(tmp_path, monkeypatch)
    for key, session in zip("abc", live):
        assert transcript_locate.resolve_claude_transcript(session, live) == (
            folder / f"{_CONV[key]}.jsonl", "bridge_id"
        ), key


def test_bridge_id_never_guesses_when_the_registry_cannot_say(tmp_path: Path, monkeypatch):
    """The registry is trusted only when it names exactly this session's
    process in exactly this folder, and its file is on disk."""
    from src import transcript_locate

    folder, live = _channel_world(tmp_path, monkeypatch)
    registry = tmp_path / "sessions"
    refused = "folder_shared"
    a = live[0]

    # No Remote Control link: a session the registry cannot be asked about.
    bare = {k: v for k, v in a.items() if k != "web_url"}
    assert transcript_locate.resolve_claude_transcript(bare, live) == (None, refused)
    # A link that is not a session link.
    odd = {**a, "web_url": "https://example.com/code/" + _BRIDGE["a"] + "/x"}
    assert transcript_locate.resolve_claude_transcript(odd, live) == (None, refused)
    # Two registry records claiming one bridge id: ambiguous, so nothing.
    _registry_entry(registry, 999, "a", sessionId=_CONV["b"])
    assert transcript_locate.resolve_claude_transcript(a, live) == (None, refused)
    (registry / "999.json").unlink()
    # A record from another folder is not this session's.
    _registry_entry(registry, 100, "a", cwd=r"E:\work\elsewhere")
    assert transcript_locate.resolve_claude_transcript(a, live) == (None, refused)
    _registry_entry(registry, 100, "a")
    # A conversation id with no file on disk, or no id at all.
    _registry_entry(registry, 100, "a", sessionId="0c7a1d2e-9b3f-4e1a-8c6d-5f4e3d2c1b0a")
    assert transcript_locate.resolve_claude_transcript(a, live) == (None, refused)
    _registry_entry(registry, 100, "a", sessionId="not-a-uuid")
    assert transcript_locate.resolve_claude_transcript(a, live) == (None, refused)
    # A corrupt record and an absent registry directory both just refuse.
    (registry / "100.json").write_text("{", encoding="utf-8")
    assert transcript_locate.resolve_claude_transcript(a, live) == (None, refused)
    import shutil
    shutil.rmtree(registry)
    assert transcript_locate.resolve_claude_transcript(a, live) == (None, refused)
    assert (folder / f"{_CONV['a']}.jsonl").is_file()


def test_bridge_id_conversation_that_moved_on_is_refused_by_its_title(
    tmp_path: Path, monkeypatch
):
    """The registry's conversation id can lag a ``/resume`` inside the
    session, so the live-title disproof (#1034) applies to it as it does to a
    launch-flag id."""
    from src import transcript_locate

    folder, live = _channel_world(tmp_path, monkeypatch)
    path = folder / f"{_CONV['a']}.jsonl"
    path.write_text(json.dumps({"type": "ai-title", "aiTitle": "Fix the backup job"}) + "\n", encoding="utf-8")
    moved = {**live[0], "live_title": "✳ Plan the garden"}
    assert transcript_locate.resolve_claude_transcript(moved, live) == (
        None, "folder_shared; bridge_id title_disproved"
    )
    same = {**live[0], "live_title": "✳ Fix the backup job"}
    assert transcript_locate.resolve_claude_transcript(same, live)[0] == path
