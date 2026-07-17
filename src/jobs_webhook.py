"""Webhook-target jobs — signature verification + payload mapping (issue #73).

Lets an external service (GitHub, Stripe, or a generic POST from
IFTTT/Zapier/…) fire a job over ``POST /api/jobs/<id>/hook`` instead of the
bearer-gated ``POST /api/jobs/<id>/run``. The hook route trusts **only** the
provider-specific signature computed here — never the SPA bearer token — so
the hook URL itself is safe to hand to a third party in plaintext.

Deliberately a leaf module — no import of :mod:`src.jobs_config` — so
``jobs_config`` can import :class:`WebhookConfig` from here without a cycle
(same pattern as ``src.jobs_kinds.base``).

Secrets: a job's ``webhook.secret`` is either a literal string or a
``$secret:<key>`` reference resolved against ``webapp_config.secrets`` at
fire time (via :mod:`src.jobs_secrets`, generalized by issue #72 from the
in-line mechanism this module introduced) — so a rotated secret lives in
one gitignored place instead of ``jobs.json``.
"""

from __future__ import annotations

import hmac
import re
import time
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any, Dict, List, Optional

from src.jobs_secrets import resolve_secret_value

PROVIDERS = frozenset({"github", "stripe", "generic"})

# Stripe's own SDKs default to a 5-minute replay tolerance window.
DEFAULT_STRIPE_TOLERANCE_SECONDS = 300


@dataclass
class WebhookConfig:
    """One job's webhook trigger configuration.

    ``mapping`` keys must match the job's declared ``Param.name`` entries —
    resolution just produces the same ``{name: value}`` shape
    ``src.jobs_argv.compose_argv`` already turns into argv/env for a manual
    run, so no new argv-composition logic is needed for a webhook fire.
    """

    provider: str
    secret: str
    mapping: Dict[str, str] = field(default_factory=dict)
    # GitHub-only: X-GitHub-Event allowlist. Empty = accept every event.
    events: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"provider": self.provider, "secret": self.secret}
        if self.mapping:
            payload["mapping"] = dict(self.mapping)
        if self.events:
            payload["events"] = list(self.events)
        return payload


def webhook_from_dict(raw: Any) -> Optional[WebhookConfig]:
    """Parse a job's ``webhook`` block. ``None`` / missing → ``None``.

    Raises ``ValueError`` on a malformed shape — the same contract as
    ``schedule_from_dict`` / ``param_from_dict`` in ``src.jobs_config``.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"webhook must be an object, got {type(raw).__name__}")

    provider = str(raw.get("provider") or "").strip()
    if provider not in PROVIDERS:
        raise ValueError(
            f"webhook.provider must be one of {sorted(PROVIDERS)}, got {provider!r}"
        )

    secret = str(raw.get("secret") or "").strip()
    if not secret:
        raise ValueError("webhook.secret is required")

    mapping_raw = raw.get("mapping")
    mapping: Dict[str, str] = {}
    if mapping_raw is not None:
        if not isinstance(mapping_raw, dict):
            raise ValueError(
                f"webhook.mapping must be an object, got {type(mapping_raw).__name__}"
            )
        for key, path in mapping_raw.items():
            if not isinstance(key, str) or not key:
                raise ValueError("webhook.mapping keys must be non-empty strings")
            if not isinstance(path, str) or not path:
                raise ValueError(
                    f"webhook.mapping[{key!r}] must be a non-empty JSONPath string"
                )
            mapping[key] = path

    events_raw = raw.get("events")
    events: List[str] = []
    if events_raw is not None:
        if not isinstance(events_raw, list):
            raise ValueError(
                f"webhook.events must be a list, got {type(events_raw).__name__}"
            )
        for entry in events_raw:
            if not isinstance(entry, str) or not entry:
                raise ValueError("webhook.events entries must be non-empty strings")
            events.append(entry)

    return WebhookConfig(provider=provider, secret=secret, mapping=mapping, events=events)


# --------------------------------------------------------------- secrets


def resolve_secret(secret_ref: str, webapp_config: Any) -> str:
    """Resolve ``webhook.secret`` to its literal value.

    ``$secret:<key>`` is looked up in ``webapp_config.secrets`` (raises
    ``ValueError`` when the key is unknown); anything else is used as-is.
    Thin wrapper around :func:`src.jobs_secrets.resolve_secret_value` so
    webhook callers keep passing the whole config object.
    """
    return resolve_secret_value(
        secret_ref, getattr(webapp_config, "secrets", {}) or {}
    )


# ------------------------------------------------------------- verifiers


def verify_github(secret: str, body: bytes, header: Optional[str]) -> bool:
    """``X-Hub-Signature-256: sha256=<hex>`` — HMAC-SHA256 over the raw body."""
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode("utf-8"), body, sha256).hexdigest()
    return hmac.compare_digest(header[len("sha256="):], expected)


def _parse_stripe_header(header: str) -> Optional[Dict[str, str]]:
    parts: Dict[str, str] = {}
    for item in header.split(","):
        if "=" not in item:
            continue
        k, _, v = item.partition("=")
        parts[k.strip()] = v.strip()
    if "t" not in parts or "v1" not in parts:
        return None
    return parts


def verify_stripe(
    secret: str,
    body: bytes,
    header: Optional[str],
    *,
    tolerance_seconds: int = DEFAULT_STRIPE_TOLERANCE_SECONDS,
    now: Optional[float] = None,
) -> bool:
    """``Stripe-Signature: t=<epoch>,v1=<hex>[,v0=...]``.

    HMAC-SHA256(secret, f"{t}.{body}") compared to ``v1``; ``t`` must be
    within ``tolerance_seconds`` of ``now`` (defaults to Stripe's own SDK
    default of 5 minutes) to reject replays of an old, otherwise-valid
    signature.
    """
    if not header:
        return False
    parsed = _parse_stripe_header(header)
    if parsed is None:
        return False
    try:
        timestamp = int(parsed["t"])
    except ValueError:
        return False
    current = time.time() if now is None else now
    if abs(current - timestamp) > tolerance_seconds:
        return False
    signed_payload = f"{timestamp}.".encode("utf-8") + body
    expected = hmac.new(secret.encode("utf-8"), signed_payload, sha256).hexdigest()
    return hmac.compare_digest(parsed["v1"], expected)


def verify_generic(secret: str, header: Optional[str]) -> bool:
    """``X-Webhook-Token: <secret>`` — plain constant-time compare."""
    if not header:
        return False
    return hmac.compare_digest(header, secret)


def verify_webhook(
    webhook: WebhookConfig, resolved_secret: str, body: bytes, headers: Any
) -> bool:
    """Dispatch to the provider-specific verifier."""
    if webhook.provider == "github":
        return verify_github(resolved_secret, body, headers.get("x-hub-signature-256"))
    if webhook.provider == "stripe":
        return verify_stripe(resolved_secret, body, headers.get("stripe-signature"))
    if webhook.provider == "generic":
        return verify_generic(resolved_secret, headers.get("x-webhook-token"))
    return False


def event_allowed(webhook: WebhookConfig, event_header: Optional[str]) -> bool:
    """GitHub event-allowlist check. Empty allowlist = accept everything."""
    if not webhook.events:
        return True
    return bool(event_header) and event_header in webhook.events


# --------------------------------------------------------------- mapping

_PATH_SEGMENT_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)((?:\[\d+\])*)$")


def _walk_path(payload: Any, path: str) -> Any:
    """Resolve one JSONPath-lite string against ``payload``.

    Supports ``$.a.b.c`` and list indices ``$.a[0].b``. Raises ``KeyError``/
    ``IndexError``/``TypeError`` (caught by the caller) when the path
    doesn't resolve against this particular payload shape.
    """
    if not path.startswith("$."):
        raise ValueError(f"mapping path must start with '$.': {path!r}")
    node = payload
    for segment in path[2:].split("."):
        m = _PATH_SEGMENT_RE.match(segment)
        if m is None:
            raise ValueError(f"malformed path segment {segment!r} in {path!r}")
        name, indices = m.group(1), m.group(2)
        node = node[name]
        for idx in re.findall(r"\[(\d+)\]", indices):
            node = node[int(idx)]
    return node


def resolve_mapping(payload: Any, mapping: Dict[str, str]) -> Dict[str, str]:
    """Turn a webhook payload into a ``{param_name: value}`` dict.

    Each ``mapping`` entry is a dot-path (``$.repository.full_name``,
    optionally with list indices, ``$.a[0].b``) evaluated against
    ``payload``. A path that doesn't resolve against this particular event's
    shape is silently omitted — a job's mapping can legitimately list more
    fields than a given event type carries. Values are stringified so they
    compose the same way a manual run's typed-parameter payload does.
    """
    if not isinstance(payload, (dict, list)):
        return {}
    resolved: Dict[str, str] = {}
    for name, path in mapping.items():
        try:
            value = _walk_path(payload, path)
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        if value is None:
            continue
        resolved[name] = value if isinstance(value, str) else str(value)
    return resolved


__all__ = [
    "PROVIDERS",
    "WebhookConfig",
    "webhook_from_dict",
    "resolve_secret",
    "verify_github",
    "verify_stripe",
    "verify_generic",
    "verify_webhook",
    "event_allowed",
    "resolve_mapping",
]
