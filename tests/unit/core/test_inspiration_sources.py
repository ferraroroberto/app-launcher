"""src/inspiration_sources.py, src/inspiration_login.py and /api/inspiration/*.

The launcher loads fleet-config's ``inspiration-scan/sources.py`` by path. These
tests give it a scratch checkout holding a stand-in with the same surface
(``parse_config``, ``ConfigError``, ``sources_path``, ``state_root``) and point
the state root at ``tmp_path``, so no test can reach the owner's real source
list. One test runs the real skill's validator when a sibling checkout has it.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from src.inspiration import login as inspiration_login
from src.inspiration import sources as inspiration_sources
from src.inspiration.toml_write import dumps
from src.webapp_config import PROJECT_ROOT

_SOURCES_PY = '''
import os
from dataclasses import dataclass
from pathlib import Path


class ConfigError(ValueError):
    pass


@dataclass
class Config:
    # The real module declares dataclasses, which must resolve their own module while it loads.
    sources: list


def state_root():
    return Path(os.environ["INSPIRATION_SCAN_ROOT"])


def sources_path():
    return state_root() / "sources.toml"


def _required(entry, key, table):
    value = entry.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"[[{table}]] entry is missing {key!r}")


def parse_config(data, base):
    for table, key in (("newsletter", "feed"), ("youtube", "channel_id"), ("x", "handle")):
        for entry in data.get(table, []):
            _required(entry, key, table)
            if table != "x":
                _required(entry, "name", table)
    settings = data.get("settings", {})
    if "top_n" in settings and (not isinstance(settings["top_n"], int) or settings["top_n"] < 1):
        raise ConfigError("settings.top_n must be a positive integer")
    return data
'''

CHANNEL = "UC" + "a" * 22

SEED = f'''# a comment the writer cannot keep
[profile]
likes = "small tools"
extra_profile_key = "kept"

[[newsletter]]
name = "Letter One"
feed = "https://example.test/feed"
note = "an unshown key"

[[youtube]]
name = "Channel One"
channel_id = "{CHANNEL}"

[[x]]
handle = "someone"

[settings]
top_n = 5
model = "m"
x_scrolls = 2

[future]
flag = true
'''


def _checkout(root: Path) -> Path:
    skill = root / ".claude" / "skills" / "inspiration-scan"
    skill.mkdir(parents=True)
    (skill / "sources.py").write_text(_SOURCES_PY, encoding="utf-8")
    return root


@pytest.fixture
def env(tmp_path, monkeypatch):
    """(fleet-config dir, sources.toml path) with the state root pinned under tmp_path."""
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("INSPIRATION_SCAN_ROOT", str(state))
    return str(_checkout(tmp_path / "fleet-config")), state / "sources.toml"


def _payload(state, **over):
    body = {
        "revision": state["revision"], "likes": state["likes"],
        "newsletter": state["newsletter"], "youtube": state["youtube"], "x": state["x"],
    }
    body.update(over)
    return body


def _saved(path: Path) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8"))


class TestTomlWriter:
    def test_round_trips_every_shape_the_source_list_holds(self):
        doc = tomllib.loads(SEED + 's = "q\\"uote\\\\ \\n tab\\t"\n[nested.deeper]\nlist = [1, 2]\nf = 1.5\n')
        assert tomllib.loads(dumps(doc)) == doc

    def test_awkward_keys_and_text_survive(self):
        doc = {"a b": 'x"y', "t": {"k.e y": [{"z": "é"}, {"z": "\u0001"}]}}
        assert tomllib.loads(dumps(doc)) == doc


class TestState:
    def test_missing_file_is_empty_and_editable_with_no_digest(self, env):
        fc, path = env
        state = inspiration_sources.read_state(fc)
        assert state["available"] and not state["exists"] and state["editable"]
        assert state["revision"] == "" and state["digest"] == {"state": "none"}
        assert not path.exists()

    def test_unusable_skill_is_unavailable_never_empty(self, tmp_path, env):
        assert inspiration_sources.read_state("")["available"] is False
        assert "no inspiration-scan skill" in inspiration_sources.read_state(str(tmp_path / "none"))["reason"]
        broken = _checkout(tmp_path / "broken")
        (broken / ".claude/skills/inspiration-scan/sources.py").write_text("raise RuntimeError('boom')")
        assert "could not be loaded" in inspiration_sources.read_state(str(broken))["reason"]

    def test_invalid_toml_is_reported_and_not_editable(self, env):
        fc, path = env
        path.write_text('[[x]]\nhandle = "a"\nhandle = "b"\n', encoding="utf-8")
        state = inspiration_sources.read_state(fc)
        assert not state["editable"] and "not valid TOML" in state["problem"]

    def test_entry_missing_a_field_is_flagged_but_still_editable(self, env):
        fc, path = env
        path.write_text('[[x]]\nnote = "no handle"\n', encoding="utf-8")
        state = inspiration_sources.read_state(fc)
        assert state["editable"] and "missing 'handle'" in state["problem"]
        assert state["x"] == [{"index": 0, "handle": ""}]


class TestSave:
    def test_each_kind_and_the_note_add_edit_remove_and_settings_survive(self, env):
        fc, path = env
        path.write_text(SEED, encoding="utf-8")
        state = inspiration_sources.read_state(fc)
        assert state["problem"] == "" and state["likes"] == "small tools"

        added = inspiration_sources.save(fc, _payload(
            state, likes="new note",
            newsletter=state["newsletter"] + [{"name": "Two", "feed": "https://two.test/rss"}],
            youtube=state["youtube"] + [{"name": "Chan Two", "channel_id": "UC" + "b" * 22}],
            x=state["x"] + [{"handle": "@other"}]))
        doc = _saved(path)
        assert [e["name"] for e in doc["newsletter"]] == ["Letter One", "Two"]
        assert [e["handle"] for e in doc["x"]] == ["someone", "other"]
        assert doc["profile"]["likes"] == "new note"
        # Everything not shown comes back unchanged.
        assert doc["settings"] == {"top_n": 5, "model": "m", "x_scrolls": 2}
        assert doc["future"] == {"flag": True}
        assert doc["profile"]["extra_profile_key"] == "kept"
        assert doc["newsletter"][0]["note"] == "an unshown key"
        assert added["revision"] != state["revision"]

        edited = [dict(added["newsletter"][0], feed="https://changed.test/feed"), added["newsletter"][1]]
        after_edit = inspiration_sources.save(fc, _payload(added, newsletter=edited))
        assert _saved(path)["newsletter"][0]["feed"] == "https://changed.test/feed"
        assert _saved(path)["newsletter"][0]["note"] == "an unshown key"

        inspiration_sources.save(fc, _payload(after_edit, newsletter=[], youtube=[], x=[]))
        doc = _saved(path)
        assert "newsletter" not in doc and "youtube" not in doc and "x" not in doc
        assert doc["settings"]["top_n"] == 5

    def test_first_save_creates_the_file(self, env):
        fc, path = env
        state = inspiration_sources.read_state(fc)
        inspiration_sources.save(fc, _payload(state, x=[{"handle": "first"}]))
        assert _saved(path)["x"] == [{"handle": "first"}]
        assert not path.with_name("sources.toml.bak").exists()

    def test_keeps_exactly_one_backup_of_the_previous_text(self, env):
        fc, path = env
        path.write_text(SEED, encoding="utf-8")
        state = inspiration_sources.read_state(fc)
        second = inspiration_sources.save(fc, _payload(state, likes="one"))
        inspiration_sources.save(fc, _payload(second, likes="two"))
        backup = path.with_name("sources.toml.bak")
        assert tomllib.loads(backup.read_text(encoding="utf-8"))["profile"]["likes"] == "one"
        assert not list(path.parent.glob("*.tmp"))

    @pytest.mark.parametrize("body,message", [
        ({"newsletter": [{"name": "A", "feed": "ftp://x"}]}, "http"),
        ({"newsletter": [{"name": "", "feed": "https://x.test"}]}, "name is required"),
        ({"youtube": [{"name": "A", "channel_id": "UCshort"}]}, "channel id"),
        ({"x": [{"handle": "has space"}]}, "not an X handle"),
        ({"x": [{"handle": "a"}, {"handle": "@A"}]}, "twice"),
        ({"x": "nope"}, "must be a list"),
        ({"likes": 5}, "must be text"),
        ({"newsletter": [{"name": "A\nB", "feed": "https://x.test"}]}, "one line"),
    ])
    def test_invalid_input_is_refused_with_a_message_and_the_file_is_unchanged(self, env, body, message):
        fc, path = env
        path.write_text(SEED, encoding="utf-8")
        before = path.read_bytes()
        state = inspiration_sources.read_state(fc)
        with pytest.raises(inspiration_sources.InspirationError, match=message) as err:
            inspiration_sources.save(fc, _payload(state, **body))
        assert err.value.status == 422
        assert path.read_bytes() == before
        assert not path.with_name("sources.toml.bak").exists()

    def test_stale_revision_is_refused_so_a_hand_edit_is_not_overwritten(self, env):
        fc, path = env
        path.write_text(SEED, encoding="utf-8")
        state = inspiration_sources.read_state(fc)
        path.write_text(SEED + "\n# edited by hand\n", encoding="utf-8")
        with pytest.raises(inspiration_sources.InspirationError, match="changed since") as err:
            inspiration_sources.save(fc, _payload(state, likes="x"))
        assert err.value.status == 409
        assert "edited by hand" in path.read_text(encoding="utf-8")

    def test_unparseable_file_is_never_overwritten(self, env):
        fc, path = env
        path.write_text('[[x]]\nhandle = "a"\nhandle = "b"\n', encoding="utf-8")
        with pytest.raises(inspiration_sources.InspirationError, match="Fix the file by hand") as err:
            inspiration_sources.save(fc, {"revision": "", "likes": "x"})
        assert err.value.status == 409

    def test_save_never_logs_a_name_or_a_url(self, env, caplog):
        fc, path = env
        caplog.set_level("DEBUG")
        state = inspiration_sources.read_state(fc)
        inspiration_sources.save(fc, _payload(
            state, likes="secret note", newsletter=[{"name": "Private Name", "feed": "https://private.test/f"}]))
        assert "Private" not in caplog.text and "private.test" not in caplog.text and "secret" not in caplog.text

    def test_the_real_skill_validator_accepts_what_is_written(self, tmp_path, monkeypatch):
        fleet = PROJECT_ROOT.parent / "fleet-config"
        if not (fleet / ".claude/skills/inspiration-scan/sources.py").is_file():
            pytest.skip("no sibling fleet-config checkout with the inspiration-scan skill")
        state = tmp_path / "real-state"
        state.mkdir()
        monkeypatch.setenv("INSPIRATION_SCAN_ROOT", str(state))
        current = inspiration_sources.read_state(str(fleet))
        saved = inspiration_sources.save(str(fleet), _payload(
            current, newsletter=[{"name": "N", "feed": "https://example.test/feed"}],
            youtube=[{"name": "Y", "channel_id": CHANNEL}], x=[{"handle": "h"}], likes="note"))
        assert saved["problem"] == "" and len(saved["x"]) == 1
        with pytest.raises(inspiration_sources.InspirationError):
            inspiration_sources.save(str(fleet), _payload(saved, x=[{"handle": "bad handle!"}]))


class TestDigestStatus:
    def _digest(self, path: Path, date: str, body: str) -> None:
        digests = path.parent / "digests"
        digests.mkdir(exist_ok=True)
        (digests / f"{date}.html").write_text(f"<style>x</style><main>{body}</main>", encoding="utf-8")

    def test_reads_the_latest_digest_date_kept_count_and_per_kind_chips(self, env):
        fc, path = env
        self._digest(path, "2026-11-08", '<p class="sub">1 ideas from 2 items read</p>')
        self._digest(path, "2026-11-09", (
            '<h1>Inspiration digest · 2026-11-09</h1><p class="sub">4 ideas from 31 items read</p>'
            '<ul class="chips"><li class="chip ok">Newsletters 2/2 read</li>'
            '<li class="chip warn">X skipped: not logged in (3 of 3)</li>'
            '<li class="chip warn">YouTube 1/2 read · not-read: HTTP 404 &amp; more</li></ul>'))
        digest = inspiration_sources.read_state(fc)["digest"]
        assert digest["state"] == "ok" and digest["date"] == "2026-11-09"
        assert (digest["kept"], digest["items"]) == (4, 31)
        assert digest["chips"] == [
            {"text": "Newsletters 2/2 read", "ok": True},
            {"text": "X skipped: not logged in (3 of 3)", "ok": False},
            {"text": "YouTube 1/2 read · not-read: HTTP 404 & more", "ok": False},
        ]

    def test_unfilled_and_unrecognised_digests_say_so(self, env):
        fc, path = env
        self._digest(path, "2026-11-09", '<p class="sub">Source list not filled in yet</p>')
        assert inspiration_sources.read_state(fc)["digest"] == {"state": "unfilled", "date": "2026-11-09"}
        self._digest(path, "2026-11-10", "<p>something else</p>")
        assert inspiration_sources.read_state(fc)["digest"] == {"state": "unreadable", "date": "2026-11-10"}


def _reset_login() -> None:
    """Forget any recorded login so each test starts from idle."""
    inspiration_login._proc = None
    inspiration_login._stderr = None
    inspiration_login._started_at = 0.0
    inspiration_login._ended_at = 0.0
    inspiration_login._result = {"state": inspiration_login.LoginState.IDLE, "detail": ""}


class _FakeProc:
    def __init__(self, code=None):
        self.code = code

    def poll(self):
        return self.code


class TestLogin:
    @pytest.fixture(autouse=True)
    def _clean(self):
        _reset_login()
        yield
        _reset_login()

    def _ready(self, tmp_path) -> str:
        root = _checkout(tmp_path / "fc2")
        (root / ".claude/skills/inspiration-scan/scan.py").write_text("")
        python = root / ".venv" / "Scripts" / "python.exe"
        python.parent.mkdir(parents=True)
        python.write_text("")
        return str(root)

    def test_start_runs_login_x_under_fleet_configs_python_and_reports_when_it_closes(self, tmp_path, monkeypatch):
        root = self._ready(tmp_path)
        spawned = []
        proc = _FakeProc()
        monkeypatch.setattr(inspiration_login.subprocess, "Popen",
                            lambda cmd, **kw: spawned.append((cmd, kw)) or proc)
        assert inspiration_login.status()["state"] == "idle"
        assert inspiration_login.start(root)["state"] == "running"
        cmd, kw = spawned[0]
        assert cmd[0].endswith("python.exe") and cmd[1].endswith("scan.py") and cmd[2] == "login-x"
        assert kw["cwd"] == root and "creationflags" in kw
        # A second tap while the window is open does not open another.
        assert inspiration_login.start(root)["state"] == "running" and len(spawned) == 1
        assert inspiration_login.status()["state"] == "running"
        proc.code = 0
        done = inspiration_login.status()
        assert done["state"] == "closed" and done["ended_at"] >= done["started_at"]

    def test_a_failed_login_helper_reports_its_last_stderr_line(self, tmp_path, monkeypatch):
        root = self._ready(tmp_path)
        proc = _FakeProc()

        def popen(cmd, **kw):
            kw["stderr"].write(b"noise\nno Playwright Python at somewhere\n")
            return proc

        monkeypatch.setattr(inspiration_login.subprocess, "Popen", popen)
        inspiration_login.start(root)
        proc.code = 1
        failed = inspiration_login.status()
        assert failed["state"] == "failed" and failed["detail"] == "no Playwright Python at somewhere"

    def test_missing_pieces_are_named_and_nothing_is_spawned(self, tmp_path, monkeypatch):
        monkeypatch.setattr(inspiration_login.subprocess, "Popen",
                            lambda *a, **k: pytest.fail("must not spawn"))
        with pytest.raises(inspiration_login.LoginStartError, match="not set"):
            inspiration_login.start("")
        with pytest.raises(inspiration_login.LoginStartError, match="no inspiration-scan skill"):
            inspiration_login.start(str(tmp_path / "none"))
        bare = _checkout(tmp_path / "bare")
        (bare / ".claude/skills/inspiration-scan/scan.py").write_text("")
        with pytest.raises(inspiration_login.LoginStartError, match="Python environment"):
            inspiration_login.start(str(bare))


class TestRoutes:
    @pytest.fixture
    def api(self, webapp_client, env):
        client, app, _ = webapp_client
        fc, path = env
        app.state.webapp_config.fleet_config_dir = fc
        _reset_login()
        return client, path

    def test_get_put_round_trip(self, api):
        client, path = api
        state = client.get("/api/inspiration/sources").json()
        assert state["available"] and state["editable"]
        body = {"revision": state["revision"], "likes": "n", "x": [{"handle": "abc"}]}
        saved = client.put("/api/inspiration/sources", json=body)
        assert saved.status_code == 200 and saved.json()["x"] == [{"index": 0, "handle": "abc"}]
        assert _saved(path)["x"] == [{"handle": "abc"}]

    def test_refusals_carry_the_message_and_status(self, api):
        client, path = api
        state = client.get("/api/inspiration/sources").json()
        bad = client.put("/api/inspiration/sources", json={"revision": state["revision"], "x": [{"handle": "no good"}]})
        assert bad.status_code == 422 and "not an X handle" in bad.json()["detail"]
        stale = client.put("/api/inspiration/sources", json={"revision": "old"})
        assert stale.status_code == 409
        assert not path.exists()

    def test_without_the_skill_it_is_unavailable_and_503(self, webapp_client, tmp_path):
        client, app, _ = webapp_client
        app.state.webapp_config.fleet_config_dir = str(tmp_path / "none")
        assert client.get("/api/inspiration/sources").json()["available"] is False
        assert client.put("/api/inspiration/sources", json={}).status_code == 503

    def test_login_x_starts_and_reports(self, webapp_client, tmp_path, monkeypatch):
        client, app, _ = webapp_client
        _reset_login()
        assert client.get("/api/inspiration/login-x").json()["state"] == "idle"
        app.state.webapp_config.fleet_config_dir = str(tmp_path / "none")
        refused = client.post("/api/inspiration/login-x")
        assert refused.status_code == 409 and "no inspiration-scan skill" in refused.json()["detail"]
        root = _checkout(tmp_path / "ready")
        (root / ".claude/skills/inspiration-scan/scan.py").write_text("")
        (root / ".venv/Scripts").mkdir(parents=True)
        (root / ".venv/Scripts/python.exe").write_text("")
        app.state.webapp_config.fleet_config_dir = str(root)
        monkeypatch.setattr(inspiration_login.subprocess, "Popen", lambda cmd, **kw: _FakeProc())
        assert client.post("/api/inspiration/login-x").json()["state"] == "running"
        assert client.get("/api/inspiration/login-x").json()["state"] == "running"
        _reset_login()
