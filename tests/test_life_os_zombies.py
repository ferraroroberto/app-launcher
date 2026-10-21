"""Life OS zombie cleanup (#1738): classifier, sweep, backup/restore, retention, summary.

Synthetic files only. ``HOME`` / ``USERPROFILE`` point at ``tmp_path`` (asserted),
the Life OS tree is a ``tmp_path`` fixture, the shared capture parser is the same
synthetic stand-in the Life OS API tests use, the session host is a stub and the
Telegram notifier is a recorder: nothing here reads the real Life OS folder,
``~/.claude`` or sends a message.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from app.cli.commands import life_os_zombie_cmd
from app.cli.commands.life_os_zombie_cmd import LifeOsZombieCommand
from src import life_os_delete, life_os_history, life_os_title, life_os_zombie_backup, life_os_zombies, session_client
from src.life_os_delete import ConversationDeleteError
from src.life_os_transcript import claude_projects_dir
from src.life_os_zombies import (
    MODE_BACKUP, MODE_DELETE, MODE_DRY_RUN, Verdict, classify, run_sweep, summary_text,
)
from src.notifications import NoopNotifier
from src.webapp_config import WebappConfig

SKILL = "journal-daily"
GREETING = "**Claude**: Hello, what shall we work on today?"
NOW = 1_800_000_000.0
GRACE = 2 * 3600.0


def sid(n: int) -> str:
    return f"e70b4cb1-9f3d-4a21-8c55-2b7d19a4f6{n:02x}"


# ------------------------------------------------------------------ classifier
def _source(body: str = GREETING, **over: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "readable": True, "revision": "r1", "oversized": False, "agent": "claude", "sid": sid(1),
        "reason": "", "native": True, "body": body,
    }
    base.update(over)
    return base


def _classify(source: Dict[str, Any], *, age_s: float = 5 * 3600.0, live: Optional[dict] = None,
              mtime: Optional[float] = -1.0, **kw: Any) -> Verdict:
    return classify(source, mtime=NOW - age_s if mtime == -1.0 else mtime, now=NOW,
                    live=live or {}, grace_s=GRACE, **kw)


class TestClassify:
    def test_a_greeting_only_capture_is_a_zombie(self):
        assert _classify(_source()) == Verdict("zombie")

    def test_a_codex_shaped_zombie(self):
        body = "**Codex**: Ready. Which part of the journal first?"
        assert _classify(_source(body, agent="codex")) == Verdict("zombie")

    def test_one_user_turn_is_kept(self):
        body = f"{GREETING}\n\n**You**: log my morning\n\n**Claude**: done"
        assert _classify(_source(body)) == Verdict("kept", "user_turn")

    def test_a_live_session_is_kept(self):
        assert _classify(_source(), live={sid(1): {"session_id": "s"}}) == Verdict("kept", "live")

    def test_inside_the_grace_window_is_kept(self):
        assert _classify(_source(), age_s=GRACE - 60) == Verdict("kept", "grace")

    def test_a_future_mtime_is_kept_as_a_suspect_clock(self):
        assert _classify(_source(), age_s=-3600.0) == Verdict("kept", "clock")

    @pytest.mark.parametrize("over", [
        {"revision": None},  # dropped below: unreadable has no revision
        {"native": False},
        {"sid": ""},
    ])
    def test_unreadable_or_unidentified_is_skipped_never_a_zombie(self, over):
        src = _source()
        for key, value in over.items():
            if value is None:
                src.pop(key)
            else:
                src[key] = value
        verdict = _classify(src)
        assert verdict.kind == "skipped"

    def test_missing_mtime_is_skipped(self):
        assert _classify(_source(), mtime=None).kind == "skipped"

    def test_an_oversized_capture_is_skipped(self):
        src = _source(oversized=True)
        src.pop("body")
        assert _classify(src) == Verdict("skipped", "oversized")

    def test_a_body_with_no_parsable_turn_is_skipped(self):
        assert _classify(_source("just some text, no speaker label")) == Verdict("skipped", "unparsed")

    def test_many_assistant_blocks_are_kept_as_a_swallowed_real_turn(self):
        body = "\n\n".join(f"**Claude**: part {i}" for i in range(3))
        assert _classify(_source(body)) == Verdict("kept", "substantial")

    def test_a_long_body_is_kept(self):
        body = "**Claude**: " + "x" * 20_000
        assert _classify(_source(body)) == Verdict("kept", "substantial")


# --------------------------------------------------------------------- world
class World:
    """A temporary Life OS tree, Claude home and session host."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp = tmp_path
        self.root = tmp_path / "life-os"
        skill = self.root / ".claude" / "skills" / SKILL
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            f"---\nname: {SKILL}\ndescription: Journal.\n---\n# {SKILL}\n", encoding="utf-8")
        self.conv = skill / "conversations"
        self.conv.mkdir()
        self.backup = tmp_path / "backup"
        home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        assert tmp_path in claude_projects_dir().parents, "must never touch the real home"
        self.project = claude_projects_dir() / "proj-a"
        self.project.mkdir(parents=True)
        fleet = tmp_path / "fleet"
        fleet.mkdir()
        self.sessions: List[dict] = []
        self.rows: List[dict] = []
        self.titles: List[tuple] = []

        cfg = WebappConfig()
        cfg.life_os_dir = str(self.root)
        cfg.fleet_config_dir = str(fleet)
        cfg.sessions_state_file = str(tmp_path / "sessions-state.json")
        self.cfg = cfg

        def parser(fleet_dir, texts):
            out = []
            for text in texts:
                lines = text.splitlines()
                header = lines[0] if lines and lines[0].startswith("<!-- capture ") else ""
                attrs = dict(re.findall(r'(\w+)="([^"]*)"', header))
                out.append({"header": attrs, "body": "\n".join(lines[1:] if header else lines),
                            "native": attrs.get("agent") in ("claude", "codex") and bool(attrs.get("sid"))})
            return out
        monkeypatch.setattr(life_os_history, "_parse_capture_texts", parser)
        monkeypatch.setattr(session_client, "list_sessions", lambda port: self.sessions)
        monkeypatch.setattr(life_os_title, "title_cli", lambda cfg: None)
        self.monkeypatch = monkeypatch

    def add(self, name: str, n: int, body: str = GREETING, *, age_h: float = 48.0,
            agent: str = "claude", transcript: bool = True, title: str = "") -> Path:
        path = self.conv / name
        path.write_text(f'<!-- capture sid="{sid(n)}" agent="{agent}" updated="synthetic" -->\n{body}\n',
                        encoding="utf-8")
        stamp = time.time() - age_h * 3600
        os.utime(path, (stamp, stamp))
        row = {"skill": SKILL, "file": name, "date": name[:10], "slug": name[16:-3], "turns": 1,
               "sid": sid(n), "agent": agent, "topic": "a topic", "title": title}
        self.rows = sorted([*self.rows, row], key=lambda r: r["file"], reverse=True)
        (self.conv / "index.json").write_text(json.dumps(self.rows), encoding="utf-8")
        blocks = [f'<!-- idx file="{r["file"]}" mtime=1 turns=1 sid="{r["sid"]}" agent="{r["agent"]}" -->\n'
                  f'### {r["date"]} · {r["slug"]}\n- **Topic:** {r["topic"]}\n' for r in self.rows]
        (self.conv / "index.md").write_text("# Conversation index\n\n" + "\n".join(blocks), encoding="utf-8")
        if transcript and agent == "claude":
            (self.project / f"{sid(n)}.jsonl").write_text('{"synthetic": true}\n', encoding="utf-8")
            (self.project / sid(n) / "subagents").mkdir(parents=True)
            (self.project / sid(n) / "subagents" / "a.jsonl").write_text("{}", encoding="utf-8")
        return path

    def snapshot(self) -> Dict[str, bytes]:
        files: Dict[str, bytes] = {}
        for base in (self.root, claude_projects_dir()):
            for p in sorted(base.rglob("*")):
                if p.is_file():
                    files[f"{base.name}/{p.relative_to(base).as_posix()}"] = p.read_bytes()
        return files

    def sweep(self, mode: str = MODE_BACKUP, **kw: Any):
        kw.setdefault("backup_dir", self.backup)
        return run_sweep(self.cfg, mode=mode, **kw)


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    return World(tmp_path, monkeypatch)


Z1, Z2 = "2026-08-01-0900-ferry-booking.md", "2026-08-02-1000-nothing-said.md"
REAL = "2026-08-03-1100-real-talk.md"


def _populate(w: World) -> None:
    w.add(Z1, 1)
    w.add(Z2, 2)
    w.add(REAL, 3, f"{GREETING}\n\n**You**: hello there\n\n**Claude**: hi")


# ----------------------------------------------------------------------- sweep
class TestSweep:
    def test_dry_run_counts_and_touches_nothing(self, world):
        _populate(world)
        world.add("2026-08-04-1200-fresh.md", 4, age_h=0.1)
        before = world.snapshot()
        result = world.sweep(MODE_DRY_RUN)
        assert (result.total, result.zombies, result.removed) == (4, 2, 0)
        assert result.kept["user_turn"] == 1 and result.kept["grace"] == 1
        assert world.snapshot() == before
        assert not world.backup.exists()

    def test_backup_moves_what_a_delete_would_remove(self, tmp_path, monkeypatch):
        """Same fixture, two runs: backup leaves the tree exactly as delete does."""
        trees = {}
        for mode in (MODE_DELETE, MODE_BACKUP):
            w = World(tmp_path / mode, monkeypatch)
            _populate(w)
            result = w.sweep(mode)
            assert (result.zombies, result.removed, result.failed) == (2, 2, 0)
            trees[mode] = w.snapshot()
        # Backup dirs sit outside both trees, so the snapshots compare like for like
        # after normalising the per-mode root name.
        def keyed(tree):
            return {k.split("/", 1)[1]: v for k, v in tree.items()}
        assert keyed(trees[MODE_DELETE]) == keyed(trees[MODE_BACKUP])
        left = keyed(trees[MODE_BACKUP])
        assert not any(Z1 in k or Z2 in k or sid(1) in k or sid(2) in k for k in left)
        assert any(REAL in k for k in left) and any(sid(3) in k for k in left)

    def test_backup_layout_has_capture_transcript_sidecar_and_manifest(self, world):
        _populate(world)
        world.sweep(MODE_BACKUP)
        folder = world.backup / date.today().isoformat() / SKILL
        assert (folder / Z1).is_file() and (folder / f"{sid(1)}.jsonl").is_file()
        assert (folder / sid(1) / "subagents" / "a.jsonl").is_file()
        manifest = json.loads((folder / f"{Z1}.manifest.json").read_text("utf-8"))
        assert manifest["sid"] == sid(1) and manifest["transcript_project"] == "proj-a"
        assert manifest["index_json_row"]["file"] == Z1
        assert Z1 in manifest["index_md_block"]
        rows = json.loads((world.conv / "index.json").read_text("utf-8"))
        assert [r["file"] for r in rows] == [REAL]
        assert Z1 not in (world.conv / "index.md").read_text("utf-8")

    def test_delete_mode_goes_through_the_shared_delete_routine(self, world, monkeypatch):
        _populate(world)
        calls = []
        real = life_os_delete.delete_conversation

        def spy(cfg, skill_id, path, source, *, stash=None):
            calls.append((skill_id, path.name, stash))
            return real(cfg, skill_id, path, source, stash=stash)
        monkeypatch.setattr(life_os_zombies, "delete_conversation", spy)
        world.sweep(MODE_DELETE)
        assert sorted(c[1] for c in calls) == [Z1, Z2] and all(c[2] is None for c in calls)
        assert not (world.conv / Z1).exists() and not (world.project / f"{sid(1)}.jsonl").exists()
        assert not world.backup.exists()

    def test_a_live_session_is_never_touched(self, world):
        _populate(world)
        world.sessions = [{"session_id": "s1", "alive": True, "flags": f"--model opus --resume {sid(1)}"}]
        result = world.sweep(MODE_DELETE)
        assert result.kept["live"] == 1 and result.removed == 1
        assert (world.conv / Z1).exists() and not (world.conv / Z2).exists()

    def test_breaker_trips_and_acts_on_none(self, world):
        _populate(world)
        before = world.snapshot()
        result = world.sweep(MODE_DELETE, max_per_run=1)
        assert result.tripped and result.removed == 0 and result.exit_code == life_os_zombies.EXIT_BREAKER
        assert world.snapshot() == before

    def test_session_host_unreachable_acts_on_none_and_says_unconfirmed(self, world, monkeypatch):
        _populate(world)
        before = world.snapshot()

        def down(port):
            raise session_client.SessionHostError("down")
        monkeypatch.setattr(session_client, "list_sessions", down)
        result = world.sweep(MODE_DELETE)
        assert result.unconfirmed and result.removed == 0 and result.exit_code == life_os_zombies.EXIT_UNCONFIRMED
        assert world.snapshot() == before
        assert "not confirmed" in summary_text(result)[0]

    @pytest.mark.parametrize("gained", [
        "\n\n**You**: wait",  # a turn: the re-classification alone would catch it
        "",                   # a reworded greeting: only the revision check does
    ])
    def test_a_capture_that_changes_between_classify_and_act_is_dropped(self, world, monkeypatch, gained):
        _populate(world)
        real = life_os_zombies.read_captures
        state = {"done": False}

        def racing(fleet_dir, paths):
            if len(paths) == 1 and paths[0].name == Z1 and not state["done"]:
                state["done"] = True
                stat = paths[0].stat()
                text = GREETING if gained else "**Claude**: A different greeting."
                paths[0].write_text(
                    f'<!-- capture sid="{sid(1)}" agent="claude" updated="x" -->\n{text}{gained}\n',
                    encoding="utf-8")
                os.utime(paths[0], (stat.st_atime, stat.st_mtime))  # still outside the grace window
            return real(fleet_dir, paths)
        monkeypatch.setattr(life_os_zombies, "read_captures", racing)
        result = world.sweep(MODE_DELETE)
        assert (world.conv / Z1).exists() and result.skipped["changed"] == 1
        assert not (world.conv / Z2).exists() and result.removed == 1

    def test_one_failing_capture_does_not_stop_the_rest(self, world, monkeypatch):
        _populate(world)
        real = life_os_delete.delete_conversation

        def flaky(cfg, skill_id, path, source, *, stash=None):
            if path.name == Z1:
                raise ConversationDeleteError(500, "locked")
            return real(cfg, skill_id, path, source, stash=stash)
        monkeypatch.setattr(life_os_zombies, "delete_conversation", flaky)
        result = world.sweep(MODE_DELETE)
        assert (result.removed, result.failed) == (1, 1)
        assert (world.conv / Z1).exists() and not (world.conv / Z2).exists()
        assert result.exit_code == life_os_zombies.EXIT_FAILED

    def test_unreadable_capture_is_skipped_not_deleted(self, world):
        _populate(world)
        (world.conv / "2026-08-05-0900-binary.md").write_bytes(b"\xff\xfe\x00bad")
        result = world.sweep(MODE_DELETE)
        assert (world.conv / "2026-08-05-0900-binary.md").exists()
        assert sum(result.skipped.values()) == 1

    def test_removals_are_logged_by_skill_and_file_without_content(self, world, caplog):
        world.add(Z1, 1, "**Claude**: PRIVATE-GREETING-TEXT")
        with caplog.at_level("INFO"):
            world.sweep(MODE_BACKUP)
        text = caplog.text
        assert f"skill={SKILL} file={Z1}" in text and "PRIVATE-GREETING-TEXT" not in text


# ------------------------------------------------------------- restore + retention
class TestRestore:
    def test_backup_then_restore_round_trips_the_tree(self, world):
        _populate(world)
        world.add("2026-07-30-0800-old-real.md", 5, f"{GREETING}\n\n**You**: hi\n\n**Claude**: ok")
        original = world.snapshot()
        world.sweep(MODE_BACKUP)
        assert world.snapshot() != original
        day = date.today().isoformat()
        for selector in (f"{day}/{SKILL}",):
            for d, skill, capture in life_os_zombie_backup.find_backups(world.backup, selector):
                out = life_os_zombie_backup.restore_one(world.cfg, world.backup, d, skill, capture)
                assert out.status == "restored", out
        restored = world.snapshot()
        for key in original:
            if key.endswith(("index.json", "index.md")):
                continue
            assert restored[key] == original[key], key
        assert set(restored) == set(original)
        rows = json.loads((world.conv / "index.json").read_text("utf-8"))
        assert sorted(r["file"] for r in rows) == sorted(r["file"] for r in world.rows)
        md = (world.conv / "index.md").read_text("utf-8")
        assert Z1 in md and Z2 in md
        # Newest first: the restored entries sit where the original ones did.
        assert [r["file"] for r in rows] == sorted((r["file"] for r in rows), reverse=True)
        assert md.index(Z2) < md.index(Z1)
        assert not list(world.backup.rglob("*.manifest.json"))

    def test_restore_refuses_to_overwrite_and_leaves_the_backup_alone(self, world):
        _populate(world)
        world.sweep(MODE_BACKUP)
        (world.conv / Z1).write_text("a newer file with the same name", encoding="utf-8")
        day = date.today().isoformat()
        out = life_os_zombie_backup.restore_one(world.cfg, world.backup, day, SKILL, Z1)
        assert out.status == "refused"
        assert (world.conv / Z1).read_text("utf-8") == "a newer file with the same name"
        assert (world.backup / day / SKILL / Z1).is_file()
        assert (world.backup / day / SKILL / f"{sid(1)}.jsonl").is_file()

    def test_restore_refuses_when_a_transcript_has_reappeared(self, world):
        _populate(world)
        world.sweep(MODE_BACKUP)
        (world.project / f"{sid(1)}.jsonl").write_text("new", encoding="utf-8")
        day = date.today().isoformat()
        out = life_os_zombie_backup.restore_one(world.cfg, world.backup, day, SKILL, Z1)
        assert out.status == "refused" and not (world.conv / Z1).exists()

    def test_restore_puts_the_title_back_through_the_title_writer(self, world, monkeypatch):
        world.add(Z1, 1, title="Ferry")
        world.sweep(MODE_BACKUP)
        calls = []

        class Done:
            returncode = 0
        monkeypatch.setattr(life_os_zombie_backup, "run_title_cli",
                            lambda cfg, skill, file, title: calls.append((skill, file, title)) or Done())
        day = date.today().isoformat()
        assert life_os_zombie_backup.restore_one(world.cfg, world.backup, day, SKILL, Z1).status == "restored"
        assert calls == [(SKILL, Z1, "Ferry")]

    @pytest.mark.parametrize("selector", ["", "nonsense", "2026-13", "2026-08-01/../x", "2026-08-01/a/b/c"])
    def test_a_malformed_selector_is_refused(self, selector):
        with pytest.raises(ValueError):
            life_os_zombie_backup.find_backups(Path("."), selector)

    def test_a_manifest_that_does_not_match_its_location_is_refused(self, world):
        _populate(world)
        world.sweep(MODE_BACKUP)
        day = date.today().isoformat()
        path = world.backup / day / SKILL / f"{Z1}.manifest.json"
        data = json.loads(path.read_text("utf-8"))
        data["capture"] = "../escape.md"
        path.write_text(json.dumps(data), encoding="utf-8")
        assert life_os_zombie_backup.restore_one(world.cfg, world.backup, day, SKILL, Z1).status == "refused"


class TestRetention:
    def test_old_dated_folders_go_recent_ones_and_strangers_stay(self, tmp_path):
        root = tmp_path / "backup"
        today = date(2026, 10, 20)
        for name in ((today - timedelta(days=31)).isoformat(), (today - timedelta(days=30)).isoformat(),
                     (today - timedelta(days=1)).isoformat(), "notes", "2026-99-99"):
            (root / name).mkdir(parents=True)
            (root / name / "f.txt").write_text("x", encoding="utf-8")
        (root / "stray.txt").write_text("x", encoding="utf-8")
        purged = life_os_zombie_backup.purge_old_backups(root, 30, today)
        assert purged == 1
        assert sorted(p.name for p in root.iterdir()) == sorted([
            (today - timedelta(days=30)).isoformat(), (today - timedelta(days=1)).isoformat(),
            "notes", "2026-99-99", "stray.txt"])

    def test_a_linked_dated_folder_is_left_alone(self, tmp_path):
        root = tmp_path / "backup"
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("x", encoding="utf-8")
        root.mkdir()
        link = root / "2020-01-01"
        if os.name == "nt":
            import _winapi
            _winapi.CreateJunction(str(outside), str(link))
        else:
            link.symlink_to(outside, target_is_directory=True)
        try:
            assert life_os_zombie_backup.purge_old_backups(root, 30, date(2026, 10, 20)) == 0
            assert (outside / "keep.txt").exists()
        finally:
            if os.name == "nt":
                os.rmdir(link)
            else:
                link.unlink()

    def test_a_sweep_purges_old_backups_except_in_dry_run(self, world):
        old = world.backup / (date.today() - timedelta(days=45)).isoformat()
        old.mkdir(parents=True)
        assert world.sweep(MODE_DRY_RUN).purged == 0 and old.exists()
        assert world.sweep(MODE_BACKUP, retention_days=30).purged == 1 and not old.exists()


# ------------------------------------------------------------------ summary + CLI
class Recorder:
    def __init__(self) -> None:
        self.sent: List[tuple] = []

    def notify(self, title: str, body: str, severity: str = "warning") -> None:
        self.sent.append((title, body, severity))


class TestCommand:
    @pytest.fixture
    def cmd(self, world, monkeypatch):
        monkeypatch.setenv("APP_LAUNCHER_DATA_DIR", str(world.tmp / "runtime"))
        monkeypatch.setattr(life_os_zombie_cmd, "load_webapp_config", lambda: world.cfg)
        recorder = Recorder()
        monkeypatch.setattr(life_os_zombie_cmd, "build_telegram_notifier_from_config",
                            lambda cfg, category=None: recorder)
        world.recorder = recorder
        return world

    def _run(self, *argv: str) -> int:
        import argparse
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        LifeOsZombieCommand.add_parser(sub)
        args = parser.parse_args(["life-os-zombie-cleanup", *argv])
        return LifeOsZombieCommand(None).execute(args)

    def test_default_mode_is_backup_and_sends_one_counts_only_message(self, cmd):
        _populate(cmd)
        assert self._run() == 0
        assert len(cmd.recorder.sent) == 1
        title, body, severity = cmd.recorder.sent[0]
        assert title == "Zombie cleanup (mode=backup)" and body.startswith("2 removed")
        everything = title + body
        for private in (Z1, Z2, REAL, "ferry", "nothing-said", "real-talk", sid(1)):
            assert private not in everything
        assert (cmd.tmp / "runtime" / "life-os-zombie-backup" / date.today().isoformat() / SKILL / Z1).is_file()

    def test_dry_run_sends_nothing(self, cmd):
        _populate(cmd)
        assert self._run("--mode", "dry-run") == 0
        assert cmd.recorder.sent == [] and (cmd.conv / Z1).exists()

    def test_unconfirmed_run_says_not_confirmed_and_exits_4(self, cmd, monkeypatch):
        _populate(cmd)

        def down(port):
            raise session_client.SessionHostError("down")
        monkeypatch.setattr(session_client, "list_sessions", down)
        assert self._run("--mode", "delete") == 4
        title, body, _ = cmd.recorder.sent[0]
        assert "not confirmed" in title and "nothing was touched" in body

    def test_a_missing_telegram_config_is_a_no_op_not_a_failure(self, world, monkeypatch):
        monkeypatch.setenv("APP_LAUNCHER_DATA_DIR", str(world.tmp / "runtime"))
        monkeypatch.setattr(life_os_zombie_cmd, "load_webapp_config", lambda: world.cfg)
        monkeypatch.setattr(life_os_zombie_cmd, "build_telegram_notifier_from_config",
                            lambda cfg, category=None: NoopNotifier())
        _populate(world)
        assert self._run() == 0

    def test_restore_lists_then_restores_with_yes(self, cmd):
        _populate(cmd)
        assert self._run() == 0
        day = date.today().isoformat()
        assert self._run("--restore", day) == 0
        assert not (cmd.conv / Z1).exists()
        assert self._run("--restore", f"{day}/{SKILL}", "--yes") == 0
        assert (cmd.conv / Z1).exists() and (cmd.conv / Z2).exists()
        assert self._run("--restore", "bogus") == 2

    def test_yes_without_restore_is_refused(self, cmd):
        assert self._run("--yes") == 2


def test_summary_counts_only():
    result = life_os_zombies.SweepResult(mode="delete", removed=3, failed=1, purged=2)
    result.kept["live"], result.kept["grace"], result.skipped["unparsed"] = 1, 2, 4
    assert summary_text(result) == (
        "Zombie cleanup (mode=delete)", "3 removed, 3 kept (live/grace), 4 skipped, 1 failed, 2 old backup day(s) purged")


def test_the_sample_job_is_declared_daily_in_backup_mode():
    from src.jobs_config import load_jobs
    sample = Path(__file__).resolve().parent.parent / "config" / "jobs.sample.json"
    job = next(j for j in load_jobs(sample).jobs if j.id == "life-os-zombie-cleanup")
    assert job.args == "life-os-zombie-cleanup --mode backup"
    assert (job.schedule.type, job.schedule.at) == ("daily", "04:00")
    assert job.exit_outcomes == {4: "deferred"} and job.alert_on_failure


class TestSummaryRoutesToTheLogChat:
    """#1742: the summary asks for the log category; real chat routing is pinned
    in tests/test_telegram_log_category.py."""

    @pytest.fixture
    def sent_to(self, world, monkeypatch):
        from src import notifications as notif

        class FakeTelegram:
            sent: List[str] = []

            def __init__(self, bot_token: str, chat_id: str) -> None:
                self.chat_id = chat_id

            def send_text(self, text: str) -> None:
                FakeTelegram.sent.append(self.chat_id)

        monkeypatch.setenv("APP_LAUNCHER_DATA_DIR", str(world.tmp / "runtime"))
        monkeypatch.setattr(life_os_zombie_cmd, "load_webapp_config", lambda: world.cfg)
        monkeypatch.setattr(notif, "TelegramNotifier", FakeTelegram)
        world.sent_chats = FakeTelegram.sent
        return world

    def _run(self, *argv: str) -> int:
        return TestCommand._run(self, *argv)

    def test_summary_goes_to_the_log_chat_when_one_is_configured(self, sent_to):
        sent_to.cfg.telegram_bot_token, sent_to.cfg.telegram_chat_id = "tok", "alerts"
        sent_to.cfg.telegram_chat_log = "log"
        _populate(sent_to)
        assert self._run() == 0
        assert sent_to.sent_chats == ["log"]

    def test_summary_falls_back_to_the_alerts_chat_without_a_log_chat(self, sent_to):
        sent_to.cfg.telegram_bot_token, sent_to.cfg.telegram_chat_id = "tok", "alerts"
        sent_to.cfg.telegram_chat_log = ""
        _populate(sent_to)
        assert self._run() == 0
        assert sent_to.sent_chats == ["alerts"]
