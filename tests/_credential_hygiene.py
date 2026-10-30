"""Credential hygiene for test runs (issue #907).

Two independent guards — either one alone is not enough:

1. **No live credential in a disposable config.** The e2e autoboot builds its
   disposable webapp's config with :func:`disposable_webapp_config`, which
   reads nothing from the real ``config/webapp_config.json`` (credential or
   not, so no live setting leaks into a test either) and carries a per-run
   token. CI's clean checkout runs the same way.
2. **No credential in failure output.** Playwright's failure call log prints
   request headers verbatim, ``Authorization`` included, and pytest writes
   that into its report — which lands on disk wherever the run's output is
   captured. :func:`pytest_runtest_makereport` scrubs every report before any
   reporter sees it: known credential values (the real config's, plus any
   registered at runtime) *and* credential-shaped header / query-param values
   whatever they hold, so a future config mistake still can't put a live
   credential back on disk through an ordinary test failure.

Wired by re-export from ``tests/conftest.py``; loadable on its own with
``-p tests._credential_hygiene``.
"""

from __future__ import annotations

import copy
import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Generator, Optional, Set

import pytest

from src.webapp_config import CREDENTIAL_KEYS, DEFAULT_CONFIG_PATH

REDACTED = "<redacted>"
DISPOSABLE_TOKEN_PREFIX = "e2e-disposable-"
# Value redaction skips anything shorter: a 1-4 char "secret" would scrub every
# occurrence of it from unrelated output. The patterns below still catch such
# a value wherever it sits in a header or query param.
_MIN_SECRET_LEN = 8

# The value stops at whitespace, a quote, or a closing delimiter, so the
# patterns hold for Playwright's call log ("- Authorization: Bearer x"), a
# headers-dict repr ("'Authorization': 'Bearer x'") and JSON alike. The scheme
# stays visible for diagnosis; only the value goes. `[ \t]`, not `\s`: an
# empty header must not swallow the next line.
_VALUE = r"[^\s\"',;)}\]]+"
_PATTERNS = (
    re.compile(
        r"(?i)(\bauthorization[\"']?[ \t]*[:=][ \t]*[\"']?"
        r"(?:(?:bearer|basic|token)[ \t]+)?)" + _VALUE
    ),
    re.compile(r"(?i)(\bx-terminal-token[\"']?[ \t]*[:=][ \t]*[\"']?)" + _VALUE),
    # The SPA's ?token= bootstrap + WS auth param, and the terminal's ?tt=.
    re.compile(r"(?i)([?&](?:token|tt|access_token)=)[^&\s\"'#)\]]+"),
)

_registered: Set[str] = set()
_config_values: Optional[Set[str]] = None


def _collect(value: Any, out: Set[str]) -> None:
    # Strings and dict values only. `api_tokens` is a list of records holding
    # a salted hash — nothing in one is a usable credential, and their labels
    # and timestamps would over-redact unrelated output.
    if isinstance(value, str):
        if len(value.strip()) >= _MIN_SECRET_LEN:
            out.add(value.strip())
    elif isinstance(value, dict):
        for item in value.values():
            _collect(item, out)


def credential_values(raw: Any) -> Set[str]:
    """Every credential string held under ``CREDENTIAL_KEYS`` in a parsed config."""
    out: Set[str] = set()
    if isinstance(raw, dict):
        for key in CREDENTIAL_KEYS:
            _collect(raw.get(key), out)
    return out


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _live_config_values() -> Set[str]:
    # Read once, lazily: the values stay in this process's memory, used only
    # to scrub output. A missing or unparseable config contributes nothing.
    global _config_values
    if _config_values is None:
        _config_values = credential_values(_load_json(DEFAULT_CONFIG_PATH))
    return _config_values


def register_secret(value: str) -> None:
    """Redact ``value`` from every later report — for a credential minted at runtime."""
    if len((value or "").strip()) >= _MIN_SECRET_LEN:
        _registered.add(value.strip())


def redact(text: str) -> str:
    """``text`` with every known credential value and credential-shaped value masked."""
    if not text:
        return text
    for value in sorted(_live_config_values() | _registered, key=len, reverse=True):
        text = text.replace(value, REDACTED)
    for pattern in _PATTERNS:
        text = pattern.sub(rf"\g<1>{REDACTED}", text)
    return text


def disposable_token() -> str:
    """A fresh per-run bearer token, recognisable as disposable in any log."""
    return DISPOSABLE_TOKEN_PREFIX + secrets.token_urlsafe(24)


#: The config keys whose default (or real-config value) is a file or directory
#: in the owner's profile: the fleet-config hook-state directory and the
#: context filter's home folder (#1717). A disposable webapp gets each one
#: under its own ``state_dir`` instead, so it reads (and the context-filter
#: Settings panel writes) nothing real. Pinned by ``tests/unit/repo/test_credential_hygiene.py``.
HOST_STATE_KEYS = {
    "sessions_state_file": "hooks-state/sessions-state.json",
    "rate_limits_file": "hooks-state/rate-limits.json",
    "chief_plan_file": "hooks-state/chief-plan.json",
    "chief_managed_file": "hooks-state/chief-managed.json",
    "chief_inbox_dir": "hooks-state/chief-inbox",
    "context_filter_mode_file": "context-filter/mode.json",
    "context_filter_log_file": "context-filter/shadow.jsonl",
}

_SYNTHETIC_FIVE_HOUR_PCT = 37
_SYNTHETIC_SEVEN_DAY_PCT = 12


def _write_synthetic_rate_limits(path: Path) -> None:
    """A fixed Claude usage payload, so the usage meter reads the same every run.

    The percentages are constants. The stamp sits a day *ahead*: the reader
    calls a file stale once its stamp is 10 minutes old, and a stamp of "now"
    would turn the meter stale partway through a 10+ minute gate run, which
    changes how it renders. Resets are relative to now so their text ("in 3h")
    does not depend on when the run happens.
    """
    now = datetime.now(timezone.utc)
    payload = {
        "captured_at": (now + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "five_hour": {
            "used_percentage": _SYNTHETIC_FIVE_HOUR_PCT,
            "resets_at": int((now + timedelta(hours=3)).timestamp()),
        },
        "seven_day": {
            "used_percentage": _SYNTHETIC_SEVEN_DAY_PCT,
            "resets_at": int((now + timedelta(days=3)).timestamp()),
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def disposable_webapp_config(token: str, state_dir: Path) -> dict:
    """The gate webapp's config: ``token`` as its ``auth_token`` and nothing from the live file.

    Hermetic by construction: no live setting (the chief doorbell, a model
    pick, a folder) reaches a test that assumes the defaults, and no credential
    can leak. The webapp overlays every other default itself, which is also what
    CI's clean checkout runs against.

    Every :data:`HOST_STATE_KEYS` entry is pinned under ``state_dir``: only the
    rate-limits file exists, holding a fixed synthetic payload; the rest are
    absent, which the webapp reads as "nothing there yet". ``state_dir`` must be
    a temp folder the caller owns.
    """
    # A disposable webapp must never write to the live task-os: with no URL,
    # its Screen time Send answers "not set up" instead.
    raw: dict = {"auth_token": token, "task_os_url": ""}
    for key, relative in HOST_STATE_KEYS.items():
        raw[key] = str(state_dir / relative)
    rate_limits = Path(raw["rate_limits_file"])
    rate_limits.parent.mkdir(parents=True, exist_ok=True)
    _write_synthetic_rate_limits(rate_limits)
    return raw


def _string_leaves(node: Any) -> Generator[str, None, None]:
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _string_leaves(value)
    elif isinstance(node, list):
        for item in node:
            yield from _string_leaves(item)


def count_leaked_credentials(source_path: Path, derived: Any) -> int:
    """How many of the config at ``source_path``'s credential values appear in ``derived``.

    Substring, not equality, so a credential embedded in a URL still counts.
    Returns a count, never the values — the caller's message must not carry
    them. A missing or unparseable source has nothing to leak.
    """
    leaves = list(_string_leaves(derived))
    return sum(
        1
        for value in credential_values(_load_json(source_path))
        if any(value in s for s in leaves)
    )


class _RedactedRepr:
    """Stand-in ``longrepr`` for a report whose failure text held a credential.

    Terminal, junit and xdist all accept an object with ``toterminal`` (or its
    ``str``); a scrubbed copy of ``reprcrash`` keeps the short test summary's
    one-line reason.
    """

    def __init__(self, text: str, crash: Any) -> None:
        self._text = text
        if crash is not None and isinstance(getattr(crash, "message", None), str):
            self.reprcrash = copy.copy(crash)
            self.reprcrash.message = redact(crash.message)

    def toterminal(self, tw: Any) -> None:
        for line in self._text.splitlines():
            tw.line(line)

    def __str__(self) -> str:
        return self._text


def scrub_report(report: pytest.TestReport) -> None:
    """Redact credentials from a report's captured output and failure text, in place."""
    report.sections = [(name, redact(content)) for name, content in report.sections]
    longrepr = report.longrepr
    if longrepr is None:
        return
    if isinstance(longrepr, tuple) and len(longrepr) == 3:  # skip: (path, lineno, reason)
        path, lineno, reason = longrepr
        report.longrepr = (path, lineno, redact(str(reason)))
        return
    text = report.longreprtext
    clean = redact(text)
    if clean != text:
        # Only replaced when something was actually masked, so an ordinary
        # failure keeps pytest's own repr (colours, crash line) untouched.
        report.longrepr = _RedactedRepr(clean, getattr(longrepr, "reprcrash", None))


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    report = yield
    scrub_report(report)
    return report
