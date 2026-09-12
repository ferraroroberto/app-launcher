"""Regression pins for test-run credential hygiene (issue #907).

Both halves of ``tests/_credential_hygiene.py``: the disposable e2e config
carries no credential from the real one, and a failing test's output carries
no credential at all. Every credential below is an obvious fake.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import textwrap
from dataclasses import fields
from pathlib import Path

import pytest

from src.webapp_config import CREDENTIAL_KEYS, WebappConfig
from tests import _credential_hygiene as hygiene

PROJECT_ROOT = Path(__file__).resolve().parent.parent

_FAKE_CREDENTIALS = {
    "auth_token": "fake-live-auth-token-907",
    "auth_password": "fake-live-password-907",
    "pushover_api_token": "fake-live-pushover-token-907",
    "pushover_user_key": "fake-live-pushover-user-907",
    "telegram_bot_token": "fake-live-telegram-token-907",
    "secrets": {"deploy_hook": "fake-live-job-secret-907"},
    "webhook_secrets": {"legacy_hook": "fake-live-legacy-secret-907"},
    "api_tokens": [{"id": "t1", "salt": "fake-salt-907", "hash": "fake-hash-907"}],
}


def _write_real_config(tmp_path: Path) -> Path:
    path = tmp_path / "webapp_config.json"
    path.write_text(
        json.dumps({
            **_FAKE_CREDENTIALS,
            "projects_dir": "E:/example/projects",
            "tunnel_url": "https://example.test/?token=fake-live-auth-token-907",
        }),
        encoding="utf-8",
    )
    return path


def test_every_credential_shaped_field_is_listed() -> None:
    """A new credential field must join CREDENTIAL_KEYS, or the disposable
    config would silently start carrying it."""
    shaped = {
        f.name
        for f in fields(WebappConfig)
        if re.search(r"token|password|secret|_key$|credential", f.name)
    }
    assert shaped <= set(CREDENTIAL_KEYS), sorted(shaped - set(CREDENTIAL_KEYS))


def test_disposable_config_drops_every_credential(tmp_path: Path) -> None:
    real = _write_real_config(tmp_path)
    cfg = hygiene.disposable_webapp_config(real, "disposable-token-for-this-run")

    dumped = json.dumps(cfg)
    for key in CREDENTIAL_KEYS:
        if key != "auth_token":
            assert key not in cfg, key
    assert "fake-live" not in dumped
    assert "fake-salt-907" not in dumped
    assert cfg["auth_token"] == "disposable-token-for-this-run"
    assert cfg["projects_dir"] == "E:/example/projects"  # realistic values survive
    # A credential embedded in a non-credential value is masked, not kept.
    assert cfg["tunnel_url"] == f"https://example.test/?token={hygiene.REDACTED}"


def test_disposable_config_without_a_real_file_is_just_the_token(tmp_path: Path) -> None:
    cfg = hygiene.disposable_webapp_config(tmp_path / "absent.json", "tok-12345678")
    assert cfg == {"auth_token": "tok-12345678"}


def test_leak_check_counts_a_credential_the_builder_let_through(tmp_path: Path) -> None:
    real = _write_real_config(tmp_path)
    assert hygiene.count_leaked_credentials(real, hygiene.disposable_webapp_config(real, "t" * 12)) == 0
    # The verbatim copy #907 replaced: every credential leaks, including the
    # one hiding inside a URL.
    verbatim = json.loads(real.read_text(encoding="utf-8"))
    assert hygiene.count_leaked_credentials(real, verbatim) == 7


@pytest.mark.parametrize(
    "shape",
    [
        "    - Authorization: Bearer {v}",              # Playwright call log
        "{{'Authorization': 'Bearer {v}'}}",            # headers dict repr
        '{{"authorization": "Bearer {v}"}}',            # JSON
        "authorization={v}",                            # no scheme
        "x-terminal-token: {v}",
        "wss://127.0.0.1:1/ws?session=s1&token={v}",    # WS auth param
        "https://127.0.0.1:1/?tt={v}&x=1",              # terminal token
    ],
)
def test_redact_masks_credential_shaped_values(shape: str) -> None:
    value = "unregistered-credential-value-907"
    out = hygiene.redact(shape.format(v=value))
    assert value not in out
    assert hygiene.REDACTED in out
    assert hygiene.redact(out) == out  # idempotent


def test_redact_masks_registered_values_in_any_shape() -> None:
    hygiene.register_secret("runtime-minted-secret-907")
    out = hygiene.redact('setItem("launcher.token", "runtime-minted-secret-907")')
    assert "runtime-minted-secret-907" not in out


def test_redact_leaves_ordinary_text_alone() -> None:
    text = "AssertionError: expected 3 sessions, got 2\nauthorization header missing"
    assert hygiene.redact(text) == text


_FAILING_AUTHED_TEST = textwrap.dedent('''
    from playwright.sync_api import sync_playwright

    CREDENTIAL = "fake-live-credential-in-a-failing-test-907"

    def test_authenticated_request_fails():
        headers = {"Authorization": f"Bearer {CREDENTIAL}"}
        print("request headers:", headers)
        with sync_playwright() as p:
            ctx = p.request.new_context()
            try:
                # Port 1 refuses: Playwright raises with a call log that
                # lists every request header verbatim.
                ctx.get("http://127.0.0.1:1/api/status", headers=headers, timeout=5000)
            finally:
                ctx.dispose()
''')


def _run_inner_pytest(test_dir: Path, *extra: str) -> str:
    env = {
        **os.environ,
        "PYTHONPATH": str(PROJECT_ROOT),
        "PYTHONUTF8": "1",
        # Hermetic: only the plugins named with -p load.
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    }
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *extra, "test_inner.py"],
        cwd=test_dir,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=90,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    return proc.stdout + proc.stderr


def test_failed_authenticated_request_leaves_no_credential_in_output(tmp_path: Path) -> None:
    """The #907 leak, end to end: a test making an authenticated request fails,
    and pytest's output must not carry the credential — neither from
    Playwright's call log nor from captured stdout."""
    (tmp_path / "test_inner.py").write_text(_FAILING_AUTHED_TEST, encoding="utf-8")
    credential = "fake-live-credential-in-a-failing-test-907"

    # Control: without the hook the credential really does reach the output,
    # so the treated run below is proving a redaction, not an absence.
    control = _run_inner_pytest(tmp_path)
    assert "1 failed" in control, control
    assert credential in control, "leak shape no longer reproduces — re-derive this pin"

    scrubbed = _run_inner_pytest(tmp_path, "-p", "tests._credential_hygiene")
    assert "1 failed" in scrubbed, scrubbed
    assert credential not in scrubbed
    assert "Authorization: Bearer " + hygiene.REDACTED in scrubbed  # call log, masked
    assert "Captured stdout call" in scrubbed
