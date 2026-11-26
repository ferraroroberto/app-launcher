"""The app's sign-in password: how it is stored, checked and set.

One way for every fleet app, so one fleet script can set the same password
everywhere. The password is stored only as a salted PBKDF2-SHA256 hash,
``pbkdf2_sha256$<iterations>$<salt b64>$<digest b64>``, in its own small JSON
file, ``config/app_password.json`` beside the app's ``src/`` folder::

    {"password_hash": "pbkdf2_sha256$200000$...$..."}

``APP_PASSWORD_FILE`` overrides the path: tests point it at a temp file so a
run never reads or writes the live one. The file sits in its own document so
no app's config loader can rewrite or drop it; ``config/app_password.json``
must be git-ignored.

Any non-empty password is accepted: a digits-only PIN and a long passphrase
alike. There are no complexity rules.

Setting it, from the repo root (prompts twice; never an argument, never
echoed or logged)::

    python -m src.password set
    python -m src.password set --stdin     # one line from stdin, for a script
    python -m src.password status          # is a password stored? prints no secret

``status`` is an installer probe (``src/installer.py``): exit 0 when a password
is stored, 1 when none is, 2 when a file it needs cannot be read. An app still
honouring its old plaintext value names where that value lives, a top-level key
of a JSON file, so it counts as stored::

    python -m src.password status --legacy config/webapp_config.json auth_password

Checking it, in the login route (``legacy`` is the app's old stored value,
plaintext, "" when it has none)::

    if not password.is_configured(legacy=cfg.auth_password):
        raise HTTPException(503, "password auth not configured")
    if not password.check_password(presented, legacy=cfg.auth_password):
        raise HTTPException(401, "bad password")

Until the setter has run there is no stored hash, and the old value is checked
instead, so moving an app onto this module never locks the owner out. Once a
hash is stored the old value is ignored; deleting it from the app's config is
then safe.

Vendor-verbatim (manifest key ``password``), stdlib-only. Pairs with
``src/login_lockout.py``, which limits the guesses.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import hmac
import json
import logging
import os
import secrets
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

ENV_VAR = "APP_PASSWORD_FILE"
DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config" / "app_password.json"
HASH_KEY = "password_hash"
HASH_PREFIX = "pbkdf2_sha256"
# 200k keeps one check near 0.3 s on the fleet PC, cheap enough per login, while
# src/login_lockout.py bounds the guess rate; task-os stores hashes in this same format.
ITERATIONS = 200_000
SALT_BYTES = 16


def password_file() -> Path:
    """Where the hash lives: ``APP_PASSWORD_FILE`` if set, else ``config/app_password.json``."""
    override = os.environ.get(ENV_VAR, "").strip()
    return Path(override) if override else DEFAULT_PATH


def hash_password(password: str) -> str:
    """A salted ``pbkdf2_sha256$...`` hash of ``password``."""
    salt = secrets.token_bytes(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS)
    return "$".join(
        (HASH_PREFIX, str(ITERATIONS), base64.b64encode(salt).decode("ascii"), base64.b64encode(digest).decode("ascii"))
    )


def verify_hash(password: str, stored: str) -> bool:
    """Constant-time check of ``password`` against a :func:`hash_password` value."""
    try:
        prefix, iterations, salt_b64, digest_b64 = stored.split("$")
        if prefix != HASH_PREFIX:
            return False
        salt = base64.b64decode(salt_b64, validate=True)
        expected = base64.b64decode(digest_b64, validate=True)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def stored_hash(path: Path | None = None) -> str:
    """The stored hash, or "" when none is stored or the file cannot be read."""
    target = path or password_file()
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning(f"⚠️ password file unreadable, falling back to the old password: {type(exc).__name__}")
        return ""
    try:
        value = json.loads(raw).get(HASH_KEY, "")
    except (ValueError, AttributeError):
        logger.warning("⚠️ password file is not a JSON object, falling back to the old password")
        return ""
    if not isinstance(value, str) or not value.startswith(HASH_PREFIX + "$"):
        if value:
            logger.warning("⚠️ password file holds no valid hash, falling back to the old password")
        return ""
    return value


def is_configured(legacy: str = "", path: Path | None = None) -> bool:
    """True when a hash is stored or the app still has its old password."""
    return bool(stored_hash(path) or legacy)


def check_password(presented: str, legacy: str = "", path: Path | None = None) -> bool:
    """Whether ``presented`` is the app's password.

    The stored hash decides when there is one; otherwise ``legacy``, the app's
    old plaintext value, is compared in constant time. An empty presented
    password is always refused.
    """
    if not presented:
        return False
    stored = stored_hash(path)
    if stored:
        return verify_hash(presented, stored)
    if not legacy:
        return False
    return hmac.compare_digest(presented.encode("utf-8"), legacy.encode("utf-8"))


def set_password(password: str, path: Path | None = None) -> Path:
    """Store the hash of ``password``, replacing any earlier one; returns the file written."""
    if not password:
        raise ValueError("the password must not be empty")
    target = path or password_file()
    # runtime-write-exempt: the hash is the app's own git-ignored config, written only by its setter
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".tmp")
    # runtime-write-exempt: the hash is the app's own git-ignored config, written only by its setter
    temp.write_text(json.dumps({HASH_KEY: hash_password(password)}, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, target)
    logger.info("✅ app password hash stored")
    return target


def _read_new_password(from_stdin: bool) -> str | None:
    if from_stdin:
        return sys.stdin.readline().rstrip("\r\n")
    first = getpass.getpass("New password: ")
    if getpass.getpass("Again: ") != first:
        print("❌ The two entries differ; nothing was changed.", file=sys.stderr)
        return None
    return first


def _legacy_value(source: str, key: str) -> str | None:
    """The app's old plaintext value at ``key`` of the JSON file ``source``; None when it cannot be read."""
    try:
        return str(json.loads(Path(source).read_text(encoding="utf-8")).get(key) or "")
    except (OSError, ValueError, AttributeError):
        return None


def _unreadable(path: Path) -> bool:
    """True when ``path`` is there but cannot be read, so what it holds is not known."""
    try:
        path.read_bytes()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return False


def _status(legacy: list[str] | None) -> int:
    """Print whether a password is stored; 0 yes, 1 no, 2 cannot tell."""
    target = password_file()
    if stored_hash(target):
        print(f"ℹ️ Password hash stored: {target}")
        return 0
    source, key = legacy or ("", "")
    old = _legacy_value(source, key) if legacy else ""
    if old:
        print(f"ℹ️ Password hash not stored; the old password ({key} in {source}) still works.")
        return 0
    if old is None or _unreadable(target):
        unread = source if old is None else target
        print(f"⚠️ Cannot tell whether a password is stored: {unread} could not be read.")
        return 2
    print(f"ℹ️ No password stored: {target}")
    return 1


def main(argv: list[str] | None = None) -> int:
    """``set`` stores a new password; ``status`` says whether one is stored."""
    # A piped Windows child prints through the ANSI code page, where an emoji raises
    # and the resulting exit 1 would read as "missing".
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="python -m src.password", description="Set or inspect the app password.")
    commands = parser.add_subparsers(dest="command", required=True)
    setter = commands.add_parser("set", help="prompt for a password and store its hash")
    setter.add_argument("--stdin", action="store_true", help="read one line from stdin instead of prompting")
    status = commands.add_parser("status", help="exit 0 when a password is stored, 1 when not, 2 when unknown")
    status.add_argument("--legacy", nargs=2, metavar=("JSON_FILE", "KEY"),
                        help="also count the app's old plaintext password at this key")
    args = parser.parse_args(argv)

    if args.command == "status":
        return _status(args.legacy)

    new = _read_new_password(args.stdin)
    if new is None:
        return 1
    if not new:
        print("❌ The password must not be empty; nothing was changed.", file=sys.stderr)
        return 1
    target = set_password(new)
    print(f"✅ Password set; its hash is in {target}. No restart needed: each login reads it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
