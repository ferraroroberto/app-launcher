"""A Life OS resume's flags when the skill's instructions changed, did not,
can't be told, or the Settings toggle is off. Fictional captures, a stubbed
spawn and stubbed git: nothing real is launched or read."""

from __future__ import annotations

import pytest

from src.life_os import skill_refresh as refresh
from tests.api._life_os_support import RESUMABLE_SID
from tests.api.test_life_os_conversations import TestTargetedResume as _TargetedResume

LATEST = "/api/life-os/skills/journal-daily/conversations/resume-latest"
CAPTURE = "2026-08-01-0900-ferry-booking.md"


class TestResumeRefresh:
    @pytest.fixture(autouse=True)
    def _isolated(self, life_os_client, monkeypatch):
        from app.webapp import middleware
        from src.clients import session as session_client
        monkeypatch.setattr(middleware.trust, "LOOPBACK_HOSTS", frozenset({"testclient"}))
        monkeypatch.setattr(session_client, "list_sessions", lambda port: [])
        self.client, self.app, overrides = life_os_client
        self.skill_md = overrides["life_os_dir"] / ".claude" / "skills" / "journal-daily" / "SKILL.md"
        self.git = None
        monkeypatch.setattr(refresh, "run_git", lambda repo, args, **_: self.git)
        self.captured = _TargetedResume()._spawn_capture(monkeypatch)

    def _resume(self, url=LATEST, **body) -> str:
        resp = self.client.post(url, json=body)
        assert resp.status_code == 200, resp.text
        return self.captured["flags"]

    def _toggle(self, on: bool) -> None:
        self.app.state.webapp_config.life_os_refresh_skill_instructions = on

    def test_a_change_prompts_once(self):
        baseline = self._resume()                 # git unknown: no prompt, fingerprint recorded
        self.skill_md.write_text("# journal-daily\nA new boundary.\n", encoding="utf-8")
        changed = self._resume()
        assert changed == baseline + f' "{refresh.refresh_prompt("journal-daily")}"'
        assert changed.startswith(f"--resume {RESUMABLE_SID}")
        assert self._resume() == baseline

    def test_unchanged_and_off_are_byte_identical(self):
        self._toggle(False)
        off = self._resume()
        assert not refresh.SKILL_HASHES_FILE.exists()
        self._toggle(True)
        self.git = ""                             # never committed: unchanged
        assert self._resume() == off
        self.skill_md.write_text("edited", encoding="utf-8")
        self._toggle(False)
        assert self._resume() == off

    def test_no_record_and_a_commit_since_the_capture_prompts(self):
        self.git = "4102444800"                   # 2100-01-01, after any fictional capture
        assert self._resume().endswith(f'"{refresh.refresh_prompt("journal-daily")}"')

    def test_unknown_resumes_without_the_prompt(self, caplog):
        with caplog.at_level("INFO", logger=refresh.__name__):
            flags = self._resume()
        assert "re-read" not in flags
        assert "instructions=unknown" in caplog.text

    def test_targeted_resume_with_a_chosen_model(self):
        self.git = "4102444800"
        capture = {"path": f".claude/skills/journal-daily/conversations/{CAPTURE}",
                   "agent": "claude", "sid": RESUMABLE_SID}
        rows = self.client.get("/api/life-os/skills/journal-daily/conversations").json()["conversations"]
        capture["revision"] = next(r["revision"] for r in rows if r["sid"] == RESUMABLE_SID)
        flags = self._resume("/api/life-os/skills/journal-daily/conversations/launch",
                             action="resume", model="claude:sonnet", capture=capture)
        assert "--model sonnet" in flags and flags.endswith(f'"{refresh.refresh_prompt("journal-daily")}"')

    def test_codex_carries_the_prompt_after_its_flags(self, monkeypatch):
        from app.webapp.routers import life_os_conversations
        async def kept(cfg, agent, sid):
            return "gpt-6-luna", True
        monkeypatch.setattr(life_os_conversations, "_kept_model", kept)
        capture = self.skill_md.parent / "conversations" / CAPTURE
        capture.write_text(capture.read_text(encoding="utf-8").replace('agent="claude"', 'agent="codex"'),
                           encoding="utf-8")
        self.git = "4102444800"
        flags = self._resume()
        assert flags.startswith(f"resume {RESUMABLE_SID} --model gpt-6-luna")
        assert flags.endswith(f'"{refresh.refresh_prompt("journal-daily")}"')


def test_the_setting_defaults_on_and_round_trips(webapp_client):
    from src.webapp_config import load_webapp_config

    client, app, overrides = webapp_client
    key = "life_os_refresh_skill_instructions"
    assert client.get("/api/config").json()[key] is True
    assert client.post("/api/config", json={key: False}).status_code == 200
    assert getattr(app.state.webapp_config, key) is False
    assert getattr(load_webapp_config(overrides["tmp_webapp_cfg_path"]), key) is False
    assert client.post("/api/config", json={key: "false"}).status_code == 400
