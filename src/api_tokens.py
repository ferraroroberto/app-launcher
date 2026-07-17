"""Scoped API bearer tokens (issue #72).

Reduces the blast radius of the single ``auth_token``: a *scoped* token
can fire only its allowed jobs (``POST /api/jobs/<id>/run``) and nothing
else, so the URL baked into a Stream Deck button no longer unlocks the
whole SPA if it leaks. Records live in ``webapp_config.api_tokens`` as
plain dicts:

    {"id": ..., "label": ..., "salt": ..., "hash": ..., "scope": ...,
     "created_at": ..., "last_used_at": ...}

Only the salted SHA-256 hash is stored — the raw token is returned once
at mint time and never persisted. ``scope`` is ``"*"`` (everything the
legacy ``auth_token`` can do) or ``{"jobs": [ids]}``.

Deliberately a leaf module (stdlib only) — imported by the auth
middleware, the tokens router, and tests without cycles.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

# Raw tokens are 32 random bytes, urlsafe-encoded (~43 chars) — the same
# strength class as scripts/gen_token.py's auth_token.
_TOKEN_BYTES = 32
_SALT_BYTES = 16

# The one path shape a job-scoped token may call. Method must be POST.
_RUN_PATH_RE = re.compile(r"^/api/jobs/([^/]+)/run$")


@dataclass
class ApiToken:
    """One parsed token record (see module docstring for the dict shape)."""

    id: str
    label: str
    salt: str
    hash: str
    scope: Any
    created_at: str = ""
    last_used_at: str = ""

    def public_dict(self) -> Dict[str, Any]:
        """API/UI shape — everything except the salt + hash."""
        return {
            "id": self.id,
            "label": self.label,
            "scope": self.scope,
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
        }


def _hash_token(raw: str, salt_hex: str) -> str:
    return hashlib.sha256(bytes.fromhex(salt_hex) + raw.encode("utf-8")).hexdigest()


def mint_token(label: str, scope: Any) -> Tuple[Dict[str, Any], str]:
    """Create a fresh token record. Returns ``(record_dict, raw_token)``.

    The raw token exists only in the return value — show it once, then
    drop it. ``scope`` must already be validated by the caller (``"*"``
    or ``{"jobs": [ids]}``).
    """
    raw = secrets.token_urlsafe(_TOKEN_BYTES)
    salt = secrets.token_hex(_SALT_BYTES)
    record = {
        "id": "tok-" + secrets.token_hex(6),
        "label": label,
        "salt": salt,
        "hash": _hash_token(raw, salt),
        "scope": scope,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "last_used_at": "",
    }
    return record, raw


def parse_tokens(raw_list: Any) -> List[ApiToken]:
    """Parse the config's ``api_tokens`` list, skipping malformed rows.

    Defensive like the rest of the config loaders — a hand-edited bad row
    must not take the auth middleware (and with it the whole app) down.
    """
    tokens: List[ApiToken] = []
    for row in raw_list or []:
        if not isinstance(row, dict):
            continue
        token_id = str(row.get("id") or "")
        salt = str(row.get("salt") or "")
        digest = str(row.get("hash") or "")
        scope = row.get("scope")
        if not token_id or not salt or not digest:
            continue
        if scope != "*" and not (
            isinstance(scope, dict) and isinstance(scope.get("jobs"), list)
        ):
            continue
        try:
            bytes.fromhex(salt)
        except ValueError:
            continue
        tokens.append(
            ApiToken(
                id=token_id,
                label=str(row.get("label") or ""),
                salt=salt,
                hash=digest,
                scope=scope,
                created_at=str(row.get("created_at") or ""),
                last_used_at=str(row.get("last_used_at") or ""),
            )
        )
    return tokens


def find_match(presented: str, raw_list: Any) -> Optional[ApiToken]:
    """Constant-time match of a presented bearer against the token list."""
    if not presented:
        return None
    for token in parse_tokens(raw_list):
        if hmac.compare_digest(_hash_token(presented, token.salt), token.hash):
            return token
    return None


def scoped_job_ids(token: ApiToken) -> Optional[List[str]]:
    """``None`` for a full-scope (``"*"``) token, else the allowed job ids."""
    if token.scope == "*":
        return None
    return [str(j) for j in token.scope.get("jobs", [])]


def scope_rejection(token: ApiToken, method: str, path: str) -> Optional[str]:
    """Why this token may NOT make this request — ``None`` when allowed.

    A job-scoped token is valid only for ``POST /api/jobs/<id>/run`` on
    one of its allowed jobs; the returned message distinguishes "wrong
    endpoint" from "wrong job" so a misconfigured Stream Deck button is
    diagnosable from the 403 body alone.
    """
    allowed = scoped_job_ids(token)
    if allowed is None:
        return None
    match = _RUN_PATH_RE.match(path)
    if match is None or method.upper() != "POST":
        return (
            f"token {token.label!r} is job-scoped and can only call "
            "POST /api/jobs/<id>/run"
        )
    job_id = match.group(1)
    if job_id not in allowed:
        return f"token {token.label!r} is not scoped for job {job_id!r}"
    return None


def touch_last_used(raw_list: Any, token_id: str) -> bool:
    """Stamp ``last_used_at`` on the matching record, in place.

    Mutates the config's live list (dicts) so the UI shows freshness
    immediately; persistence is opportunistic (the next mint/revoke save
    writes it out). Returns True when a row was updated.
    """
    for row in raw_list or []:
        if isinstance(row, dict) and row.get("id") == token_id:
            row["last_used_at"] = datetime.now().isoformat(timespec="seconds")
            return True
    return False
