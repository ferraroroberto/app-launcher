"""WebAuthn passkey gate for the interactive terminal.

The terminal endpoints are Tailscale-only *and*, when a relying party is
configured, gated behind a **platform passkey** (Face ID on the enrolled
iPhone). This module owns:

- the enrolled-credential store (``config/webauthn_devices.json``),
- the registration / authentication ceremonies (py_webauthn),
- a one-time enrollment window (opened from the tray) so a new device can
  only be added deliberately,
- short-lived **terminal tokens** minted by a successful passkey assertion
  and required by the terminal input / resize / image / WS endpoints.

Single-user by design: one logical user, a small whitelist of devices.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from webauthn import (
    base64url_to_bytes,
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import bytes_to_base64url
from webauthn.helpers.structs import (
    AuthenticatorAttachment,
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DEVICES_PATH = PROJECT_ROOT / "config" / "webauthn_devices.json"

# Fixed user handle — this hub has exactly one logical user.
_USER_ID = b"launcher-terminal-user"
_USER_NAME = "launcher"

_CHALLENGE_TTL = 300.0          # 5 min to complete a ceremony
_TERMINAL_TOKEN_TTL = 12 * 3600.0  # a passkey unlock is good for 12 h
_ENROLL_WINDOW_DEFAULT = 300.0  # tray "enroll device" window length


@dataclass
class _Challenge:
    value: bytes
    label: str
    created_at: float


class WebAuthnGate:
    """Stateful holder for ceremonies, the device whitelist, and tokens."""

    def __init__(self, devices_path: Optional[Path] = None) -> None:
        self._devices_path = devices_path or DEFAULT_DEVICES_PATH
        self._lock = threading.Lock()
        self._reg_challenge: Optional[_Challenge] = None
        self._auth_challenge: Optional[_Challenge] = None
        self._terminal_tokens: Dict[str, float] = {}
        self._enroll_until = 0.0

    # ----------------------------------------------------------- config
    @staticmethod
    def configured(cfg) -> bool:
        """True when a relying party is set — i.e. the passkey gate is live."""
        return bool(
            getattr(cfg, "webauthn_rp_id", "")
            and getattr(cfg, "webauthn_origin", "")
        )

    # ------------------------------------------------- enrollment window
    def open_enrollment_window(
        self, seconds: float = _ENROLL_WINDOW_DEFAULT
    ) -> float:
        """Open a one-time window during which a new passkey may register."""
        with self._lock:
            self._enroll_until = time.time() + seconds
        logger.info(f"🔐 Passkey enrollment window open for {int(seconds)}s")
        return self._enroll_until

    def enrollment_open(self) -> bool:
        with self._lock:
            return time.time() < self._enroll_until

    def enrollment_seconds_left(self) -> int:
        with self._lock:
            return max(0, int(self._enroll_until - time.time()))

    # ------------------------------------------------------ device store
    def load_devices(self) -> List[Dict[str, Any]]:
        if not self._devices_path.exists():
            return []
        try:
            raw = json.loads(self._devices_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(f"⚠️  Could not read {self._devices_path}: {exc}")
            return []
        return list(raw.get("devices") or [])

    def _save_devices(self, devices: List[Dict[str, Any]]) -> None:
        self._devices_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._devices_path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps({"devices": devices}, indent=2), encoding="utf-8"
        )
        os.replace(tmp, self._devices_path)

    def list_devices(self) -> List[Dict[str, Any]]:
        """Public view of enrolled devices (no key material)."""
        return [
            {
                "id": d.get("id"),
                "label": d.get("label"),
                "added_at": d.get("added_at"),
                "last_used": d.get("last_used"),
            }
            for d in self.load_devices()
        ]

    def remove_device(self, device_id: str) -> bool:
        with self._lock:
            devices = self.load_devices()
            kept = [d for d in devices if d.get("id") != device_id]
            if len(kept) == len(devices):
                return False
            self._save_devices(kept)
        logger.info(f"🗑️  Removed enrolled passkey {device_id}")
        return True

    # ----------------------------------------------------- registration
    def begin_registration(self, cfg, label: str) -> Dict[str, Any]:
        """Build registration options for a new platform passkey.

        Only allowed while the enrollment window is open.
        """
        if not self.enrollment_open():
            raise PermissionError("enrollment window is closed")
        existing = self.load_devices()
        exclude = [
            PublicKeyCredentialDescriptor(
                id=base64url_to_bytes(d["credential_id"])
            )
            for d in existing
            if d.get("credential_id")
        ]
        options = generate_registration_options(
            rp_id=cfg.webauthn_rp_id,
            rp_name=cfg.webauthn_rp_name or "Launcher",
            user_id=_USER_ID,
            user_name=_USER_NAME,
            user_display_name=label or "Launcher device",
            authenticator_selection=AuthenticatorSelectionCriteria(
                authenticator_attachment=AuthenticatorAttachment.PLATFORM,
                resident_key=ResidentKeyRequirement.PREFERRED,
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
            exclude_credentials=exclude or None,
        )
        with self._lock:
            self._reg_challenge = _Challenge(
                value=options.challenge,
                label=label or "device",
                created_at=time.time(),
            )
        return json.loads(options_to_json(options))

    def finish_registration(self, cfg, credential: Any) -> Dict[str, Any]:
        """Verify a registration response and persist the new passkey."""
        with self._lock:
            challenge = self._reg_challenge
            self._reg_challenge = None
        if challenge is None or time.time() - challenge.created_at > _CHALLENGE_TTL:
            raise PermissionError("registration challenge expired — retry")
        if not self.enrollment_open():
            raise PermissionError("enrollment window closed before finish")
        verification = verify_registration_response(
            credential=credential,
            expected_challenge=challenge.value,
            expected_rp_id=cfg.webauthn_rp_id,
            expected_origin=cfg.webauthn_origin,
            require_user_verification=True,
        )
        device = {
            "id": secrets.token_hex(8),
            "label": challenge.label,
            "credential_id": bytes_to_base64url(verification.credential_id),
            "public_key": bytes_to_base64url(
                verification.credential_public_key
            ),
            "sign_count": verification.sign_count,
            "added_at": datetime.now().isoformat(timespec="seconds"),
            "last_used": None,
        }
        with self._lock:
            devices = self.load_devices()
            devices.append(device)
            self._save_devices(devices)
            self._enroll_until = 0.0  # one device per opened window
        logger.info(f"✅ Enrolled passkey '{device['label']}' ({device['id']})")
        return {"id": device["id"], "label": device["label"]}

    # --------------------------------------------------- authentication
    def begin_authentication(self, cfg) -> Dict[str, Any]:
        """Build an assertion challenge restricted to enrolled passkeys."""
        devices = self.load_devices()
        if not devices:
            raise PermissionError("no passkey enrolled — open the tray window")
        allow = [
            PublicKeyCredentialDescriptor(
                id=base64url_to_bytes(d["credential_id"])
            )
            for d in devices
            if d.get("credential_id")
        ]
        options = generate_authentication_options(
            rp_id=cfg.webauthn_rp_id,
            allow_credentials=allow,
            user_verification=UserVerificationRequirement.REQUIRED,
        )
        with self._lock:
            self._auth_challenge = _Challenge(
                value=options.challenge, label="", created_at=time.time()
            )
        return json.loads(options_to_json(options))

    def finish_authentication(self, cfg, credential: Any) -> str:
        """Verify an assertion against the whitelist and mint a terminal token."""
        with self._lock:
            challenge = self._auth_challenge
            self._auth_challenge = None
        if challenge is None or time.time() - challenge.created_at > _CHALLENGE_TTL:
            raise PermissionError("authentication challenge expired — retry")

        raw_id = _credential_id_of(credential)
        with self._lock:
            devices = self.load_devices()
            match = next(
                (d for d in devices if d.get("credential_id") == raw_id), None
            )
            if match is None:
                raise PermissionError("passkey is not on the whitelist")
            verification = verify_authentication_response(
                credential=credential,
                expected_challenge=challenge.value,
                expected_rp_id=cfg.webauthn_rp_id,
                expected_origin=cfg.webauthn_origin,
                credential_public_key=base64url_to_bytes(match["public_key"]),
                credential_current_sign_count=int(match.get("sign_count") or 0),
                require_user_verification=True,
            )
            match["sign_count"] = verification.new_sign_count
            match["last_used"] = datetime.now().isoformat(timespec="seconds")
            self._save_devices(devices)
            token = self._mint_token_locked()
        logger.info(f"🔓 Passkey unlock by '{match.get('label')}'")
        return token

    # ------------------------------------------------- terminal tokens
    def _mint_token_locked(self) -> str:
        now = time.time()
        self._terminal_tokens = {
            t: exp for t, exp in self._terminal_tokens.items() if exp > now
        }
        token = secrets.token_urlsafe(32)
        self._terminal_tokens[token] = now + _TERMINAL_TOKEN_TTL
        return token

    def valid_terminal_token(self, token: str) -> bool:
        if not token:
            return False
        with self._lock:
            exp = self._terminal_tokens.get(token)
            if exp is None:
                return False
            if exp <= time.time():
                self._terminal_tokens.pop(token, None)
                return False
            return True

    def revoke_terminal_tokens(self) -> None:
        with self._lock:
            self._terminal_tokens.clear()


def _credential_id_of(credential: Any) -> str:
    """Pull the base64url credential id out of a browser assertion payload."""
    if isinstance(credential, str):
        try:
            credential = json.loads(credential)
        except (ValueError, TypeError):
            return ""
    if isinstance(credential, dict):
        return str(credential.get("id") or credential.get("rawId") or "")
    return ""
