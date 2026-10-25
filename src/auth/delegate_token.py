"""The delegate token: a narrow, revocable credential for an external agent.

An agent hosted by a third party may read the Board and file issues for the
fleet chief to triage, and nothing else. It never gets the main bearer token,
which can spawn coding sessions that run with full permissions on the host.

The record lives in ``webapp_config.delegate_token`` as
``{"salt": ..., "hash": ..., "created_at": ...}``; an empty dict means off,
which is the default. As with :mod:`src.auth.api_tokens`, only the salted SHA-256
hash is stored and the raw value exists only in the regenerate response, so
regenerating replaces the hash and the old value stops matching at once.

The allow-list is closed: a request that matches no row in
:data:`ALLOWED_ROUTES` is refused, whatever the path. Leaf module (stdlib plus
the hashing helper it shares with ``api_tokens``), imported by the auth
middleware, the tokens router and tests.
"""

from __future__ import annotations

import hmac
import secrets
from datetime import datetime
from typing import Any, Dict, FrozenSet, Optional, Tuple

from src.auth.api_tokens import hash_token

# The same strength class as a minted API token and scripts/gen_token.py.
_TOKEN_BYTES = 32
_SALT_BYTES = 16

#: ``(method, exact path)`` pairs the delegate token may call. Exact match
#: only, so no prefix ever widens it.
ALLOWED_ROUTES: FrozenSet[Tuple[str, str]] = frozenset({
    ("GET", "/api/board"),
    ("GET", "/api/board/chief-plan"),
    ("POST", "/api/board/issues/file"),
})

REFUSAL = (
    "the delegate token can only read the Board and the chief's plan "
    "and file issues"
)


def mint() -> Tuple[Dict[str, str], str]:
    """A fresh ``(record, raw_token)``. Show the raw value once, then drop it."""
    raw = secrets.token_urlsafe(_TOKEN_BYTES)
    salt = secrets.token_hex(_SALT_BYTES)
    record = {
        "salt": salt,
        "hash": hash_token(raw, salt),
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    return record, raw


def _valid(record: Any) -> Optional[Dict[str, str]]:
    """The record when it is complete and well-formed, else ``None`` (off).

    A hand-edited or half-written record fails closed: it matches nothing.
    """
    if not isinstance(record, dict):
        return None
    salt = str(record.get("salt") or "")
    digest = str(record.get("hash") or "")
    if not salt or not digest:
        return None
    try:
        bytes.fromhex(salt)
    except ValueError:
        return None
    return {"salt": salt, "hash": digest, "created_at": str(record.get("created_at") or "")}


def enabled(record: Any) -> bool:
    return _valid(record) is not None


def matches(presented: str, record: Any) -> bool:
    """Constant-time check of a presented bearer against the delegate record."""
    valid = _valid(record)
    if not presented or valid is None:
        return False
    return hmac.compare_digest(hash_token(presented, valid["salt"]), valid["hash"])


def allowed(method: str, path: str) -> bool:
    return (method.upper(), path) in ALLOWED_ROUTES


def public_dict(record: Any) -> Dict[str, Any]:
    """Settings shape: whether it is on and when it was made, never the hash."""
    valid = _valid(record)
    return {
        "enabled": valid is not None,
        "created_at": valid["created_at"] if valid else "",
    }
