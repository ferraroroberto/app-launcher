"""A synthetic host identity for tests: a fake account name, a fake home folder, a fake interpreter path.

A test that reads the real account name or home folder passes on the developer's machine, because
there the real value is the expected one, and fails on every other host (a macOS runner, CI, a
colleague's box). Tests read host identity through this module instead; the conventions ratchet's
`host-identity` rule flags the direct reads.

    from tests.host._synthetic_host import synthetic_host  # in tests/conftest.py, registers the fixture

    def test_shows_config_path(synthetic_host):
        assert render_path() == str(synthetic_host.home / ".config" / "app.toml")

`synthetic_host_env(env, root)` returns a copy of `env` pointing at the same synthetic home, for
code that starts a child process (the e2e harness starts the app under test with it, so the
screenshot privacy check can look for `SYNTHETIC_ACCOUNT` on every page).

What it cannot fake: `os.getlogin()` and `pwd.getpw*` read the operating system, not the
environment, so a test never calls them. `sys.executable` is left alone, because tests spawn
through it; `SyntheticHost.python` is the path a test displays or asserts on instead.

Vendor-verbatim (manifest key `synthetic_host`), stdlib + pytest only.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest

#: A name no real account carries, so finding it on a page or in output means the host leaked through.
SYNTHETIC_ACCOUNT = "synthetic-host-user"
#: The environment variables that name the account; `getpass.getuser()` reads them in this order.
ACCOUNT_VARS = ("LOGNAME", "USER", "LNAME", "USERNAME")
#: The environment variables that name the home folder (`HOME` on POSIX, the rest on Windows).
HOME_VARS = ("HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH")


@dataclass(frozen=True)
class SyntheticHost:
    """The identity a test sees: `account`, `home` (an existing folder under the test's tmp path), `python`."""

    account: str
    home: Path
    python: str


def synthetic_host_env(env: Mapping[str, str], root: Path) -> dict[str, str]:
    """A copy of `env` whose account and home variables name a synthetic home created under `root`."""
    home = root / "home" / SYNTHETIC_ACCOUNT
    home.mkdir(parents=True, exist_ok=True)
    out = dict(env)
    out.update(dict.fromkeys(ACCOUNT_VARS, SYNTHETIC_ACCOUNT))
    out["HOME"] = out["USERPROFILE"] = str(home)
    drive, rest = os.path.splitdrive(str(home))
    if drive:  # Windows; a POSIX path has no drive, and a stale real HOMEDRIVE must not survive
        out["HOMEDRIVE"], out["HOMEPATH"] = drive, rest
    else:
        out.pop("HOMEDRIVE", None)
        out.pop("HOMEPATH", None)
    return out


@pytest.fixture
def synthetic_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SyntheticHost:
    """Point this test's account and home variables at a synthetic home; restored afterwards."""
    env = synthetic_host_env({}, tmp_path)
    for name in (*ACCOUNT_VARS, *HOME_VARS):
        if name in env:
            monkeypatch.setenv(name, env[name])
        else:
            monkeypatch.delenv(name, raising=False)
    home = Path(env["HOME"])
    return SyntheticHost(SYNTHETIC_ACCOUNT, home, str(home / ".venv" / "bin" / "python"))
