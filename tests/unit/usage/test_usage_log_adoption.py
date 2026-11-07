"""The usage log's privacy contract, held in this app (#1524).

The vendored halves (``src/usage_log.py``, ``_vendored/usage-log/``) are tested
in project-scaffolding. What is pinned here is what only this app decides:
that the router sits behind the bearer-token middleware, that what lands in the
local SQLite file is only kind / view id / control id / session id / device
class / timestamp, that the page's ``data-track`` ids are constants, and that
nothing the browser half does can reach another origin. The browser half runs
under Node in ``tests/js/usage_log.test.mjs`` (machines without Node skip it).

Every value is invented.
"""

from __future__ import annotations

import re
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from app.webapp.shell_html import assemble_shell
from src.subprocess_flags import NO_WINDOW
from src.usage.feature_counts import FEATURE_IDS
from src.usage_log import ID_PATTERN

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_STATIC = _REPO_ROOT / "app" / "webapp" / "static"
_TOKEN = "invented-bearer"
_SESSION = "3f2b8c1e-0d4a-4b6f-9c2e-1a2b3c4d5e6f"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}
_COLUMNS = ["id", "ts", "session_id", "kind", "view_id", "control_id", "device_class", "os_class"]
# The vendored table also carries the public-site columns; in a fleet app they are never filled.
_PUBLIC_SITE_COLUMNS = ["country", "visitor", "ip"]


@pytest.fixture
def usage_env(tmp_path, monkeypatch):
    """Point the runtime-data dir at a temp dir before the app is built."""
    data_dir = tmp_path / "runtime-data"
    monkeypatch.setenv("APP_LAUNCHER_DATA_DIR", str(data_dir))
    return data_dir / "usage.sqlite3"


def _batch(events, **envelope):
    body = {"session_id": _SESSION, "device_class": "phone", "os_class": "ios"}
    body.update(envelope)
    body["events"] = events
    return body


class TestRouterInThisApp:
    def test_events_need_the_bearer_token_off_loopback(self, usage_env, webapp_client):
        client, app, _ = webapp_client
        app.state.webapp_config.auth_token = _TOKEN
        body = _batch([{"kind": "view", "id": "board"}])
        assert client.post("/api/usage/events", json=body).status_code == 401
        assert client.get("/api/usage/summary").status_code == 401
        assert not usage_env.exists(), "a rejected request still wrote to the log"
        ok = client.post("/api/usage/events", json=body, headers=_AUTH)
        assert ok.status_code == 200
        assert ok.json()["accepted"] == 1

    def test_only_the_allowed_columns_reach_the_local_file(self, usage_env, webapp_client):
        client, app, _ = webapp_client
        app.state.webapp_config.auth_token = _TOKEN
        secrets = ["SECRET-MESSAGE", "SECRET-QUERY", "SECRET-USER-NAME", "SECRET-UA-TOKEN"]
        body = _batch(
            [
                {"kind": "session_start"},
                {"kind": "view", "id": "board"},
                {
                    "kind": "tap", "id": "board-refresh", "view": "board",
                    # A hostile or buggy client sends more than the contract.
                    "text": secrets[0], "url": f"https://invented.example/?q={secrets[1]}",
                    "name": secrets[2], "value": secrets[0],
                },
                # Free text and a URL where an id belongs: dropped, not stored.
                {"kind": "tap", "id": f"{secrets[2]} wrote this"},
                {"kind": "tap", "id": f"https://invented.example/a?q={secrets[1]}"},
                {"kind": "message", "id": "board"},
                {"kind": "session_end"},
            ],
            user_agent=f"Mozilla/5.0 ({secrets[3]})",
            ip="203.0.113.7",
        )
        resp = client.post("/api/usage/events", json=body, headers=_AUTH)
        assert resp.status_code == 200
        assert resp.json() == {"accepted": 4, "rejected": 3}

        conn = sqlite3.connect(usage_env)
        try:
            columns = [r[1] for r in conn.execute("PRAGMA table_info(events)")]
            rows = conn.execute(
                "SELECT kind, view_id, control_id, device_class, os_class FROM events ORDER BY id"
            ).fetchall()
            filled = conn.execute(
                "SELECT COUNT(*) FROM events WHERE country IS NOT NULL OR visitor IS NOT NULL OR ip IS NOT NULL"
            ).fetchone()[0]
            everything = "\n".join(conn.iterdump())
        finally:
            conn.close()
        assert columns == _COLUMNS + _PUBLIC_SITE_COLUMNS
        assert filled == 0, "a public-site column was filled by a fleet app"
        assert rows == [
            ("session_start", None, None, "phone", "ios"),
            ("view", "board", None, "phone", "ios"),
            ("tap", "board", "board-refresh", "phone", "ios"),
            ("session_end", None, None, "phone", "ios"),
        ]
        for secret in secrets + ["203.0.113.7", "invented.example"]:
            assert secret not in everything, f"{secret} reached the database"

    def test_the_log_lives_under_the_runtime_data_dir_and_nowhere_else(self, usage_env, webapp_client):
        client, _, _ = webapp_client
        client.post("/api/usage/events", json=_batch([{"kind": "view", "id": "jobs"}]))
        assert usage_env.is_file()
        assert usage_env.parent.name == "runtime-data"


class TestMarkup:
    def test_every_data_track_id_is_a_valid_constant(self):
        html = assemble_shell(_STATIC / "index.html")
        ids = re.findall(r'data-track="([^"]*)"', html)
        assert ids, "no data-track controls found -- wrong file?"
        bad = [i for i in ids if not ID_PATTERN.fullmatch(i)]
        assert not bad, f"data-track ids outside the bounded pattern: {bad}"

    def test_no_script_derives_a_control_id_at_runtime(self):
        """Ids are developer-chosen constants, never built from element text or
        data, so every place a first-party script sets one gives a string
        literal of a valid id. The only other right-hand sides are the one
        parameter each declared control builder (``usage_track_ratchet``
        ``builders`` in ``.fleet.toml``) forwards from its callers, where the
        ratchet holds each caller to a literal."""
        forwarded = {"opts.track", "track"}
        builders = {
            "apps.js", "chief-plan.js", "drawer.js", "proof-chip.js", "changes-overlay.js", "ask-option.js",
        }
        setter = re.compile(
            r"""(?:dataset\.track\s*=\s*|setAttribute\(\s*['"]data-track['"]\s*,\s*)([^;)]+)"""
        )
        offenders = []
        for path in _STATIC.rglob("*.js"):
            if {"_vendored", "vendor"} & set(path.relative_to(_STATIC).parts):
                continue
            code = re.sub(r"/\*.*?\*/|//[^\n]*", "", path.read_text(encoding="utf-8"), flags=re.S)
            for rhs in setter.findall(code):
                rhs = rhs.strip()
                literal = re.fullmatch(r"'([^']*)'", rhs)
                if literal and ID_PATTERN.fullmatch(literal.group(1)):
                    continue
                if rhs in forwarded and path.name in builders:
                    continue
                offenders.append(f"{path.name}: {rhs}")
        assert not offenders, f"control ids that are not literals: {offenders}"

    def test_the_mic_ids_are_literals_and_nothing_fakes_a_click(self):
        """``voice.js`` keeps the dictation mic's id in step with its state from
        literal ids only, and a withheld click is recorded through the usage
        log's direct call: no hidden element is created and clicked."""
        code = re.sub(r"/\*.*?\*/|//[^\n]*", "", (_STATIC / "voice.js").read_text(encoding="utf-8"), flags=re.S)
        ids = re.findall(r"setAttribute\('data-track', '([^']*)'\)", code)
        assert ids == ["mic.late-click", "mic.ignored-arming", "mic.cancel-finalizing", "mic.stop", "mic.start"], ids
        assert all(ID_PATTERN.fullmatch(i) for i in ids), ids
        assert "usage.track('mic.no-click');" in code
        assert "createElement('span')" not in code and ".click()" not in code

    def test_no_script_fakes_a_click_to_record_a_use(self):
        offenders = []
        for path in _STATIC.rglob("*.js"):
            if {"_vendored", "vendor"} & set(path.relative_to(_STATIC).parts):
                continue
            code = re.sub(r"/\*.*?\*/|//[^\n]*", "", path.read_text(encoding="utf-8"), flags=re.S)
            if re.search(r"probe\w*\.click\(\)|dispatchEvent\(\s*new (?:Mouse|Pointer)?Event\(\s*['\"]click", code, flags=re.I):
                offenders.append(path.name)
        assert not offenders, f"scripts that fake a click: {offenders}"

    def test_the_feature_ids_are_a_closed_list_of_literals(self):
        """``feature-track.js`` records a use only for an id on its own literal
        list; every ``feat.*`` string any script names is on it."""
        tracker = _STATIC / "settings" / "feature-track.js"
        code = re.sub(r"/\*.*?\*/|//[^\n]*", "", tracker.read_text(encoding="utf-8"), flags=re.S)
        assert "data-track" not in code and "dataset" not in code, "the tracker no longer touches the DOM"
        assert "FEATURE_IDS.indexOf(id) === -1) return;" in code
        assert code.count("usage.track(id);") == 1
        listed = re.findall(r"^\s*'(feat\.[^']*)',$", code, flags=re.M)
        assert listed == list(FEATURE_IDS), "the page's list drifted from src/usage/feature_counts.py"
        assert all(ID_PATTERN.fullmatch(i) for i in listed), listed

        named: set[str] = set()
        for path in _STATIC.rglob("*.js"):
            if {"_vendored", "vendor"} & set(path.relative_to(_STATIC).parts) or path in (
                tracker, _STATIC / "screen" / "screen-time.js",
            ):
                continue
            named.update(re.findall(r"'(feat\.[^']*)'", path.read_text(encoding="utf-8")))
        assert named == set(FEATURE_IDS), f"ids named but unlisted, or listed but never recorded: {named ^ set(FEATURE_IDS)}"

        sheet = (_STATIC / "screen" / "screen-time.js").read_text(encoding="utf-8")
        assert re.findall(r"\{ id: '(feat\.[^']*)', label:", sheet) == list(FEATURE_IDS)


class TestOnlyTheOwnOrigin:
    @pytest.mark.parametrize(
        "path",
        [
            _STATIC / "usage.js",
            _STATIC / "_vendored" / "usage-log" / "usage-log.js",
        ],
        ids=["usage.js", "usage-log.js"],
    )
    def test_the_browser_half_names_no_host(self, path):
        code = re.sub(r"/\*.*?\*/", "", path.read_text(encoding="utf-8"), flags=re.S)
        assert not re.search(r"https?://|(?<![:\w])//[A-Za-z0-9.-]+\.[a-z]{2,}", code)

    def test_the_endpoint_is_a_same_origin_path(self):
        text = (_STATIC / "usage.js").read_text(encoding="utf-8")
        assert "export const USAGE_ENDPOINT = '/api/usage/events';" in text


class TestManifest:
    def test_both_halves_are_declared_at_one_sha(self):
        toml = (_REPO_ROOT / ".fleet.toml").read_text(encoding="utf-8")
        entries = {
            key: re.search(rf"^{key} = \{{ src = \"([^\"]+)\", sha = \"([0-9a-f]{{40}})\"", toml, re.M)
            for key in ("usage-log", "usage_log")
        }
        assert all(entries.values()), "usage-log / usage_log missing from [vendored]"
        assert entries["usage-log"].group(1) == "app/webapp/static/_vendored/usage-log"
        assert entries["usage_log"].group(1) == "src/usage_log.py"
        assert entries["usage-log"].group(2) == entries["usage_log"].group(2)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required")
def test_the_browser_half_sends_only_the_contract_to_its_own_origin():
    result = subprocess.run(
        ["node", str(_REPO_ROOT / "tests" / "js" / "usage_log.test.mjs")],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=NO_WINDOW,
    )
    assert result.returncode == 0, (
        "usage_log.test.mjs failed:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "OK" in result.stdout
