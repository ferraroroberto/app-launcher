"""Unit tests for cross-router helpers.

Focus: ``should_mirror_to_pc`` — the decision (issue #20 / #241) of whether
a PTY launch opens the dedicated PC mirror window. Both a phone launch
(non-loopback, no ``desktop`` flag) and a desktop-browser launch
(``desktop: true``, loopback or tunnel) open it; only a non-desktop loopback
launch skips it and renders in-page.

Plus ``tsnet_host_from_cert`` / ``mirror_url`` (issue #356) — which URL that
window opens: loopback (auth-bypass) on a self-signed cert, the ts.net URL
with explicit ``?token=`` / ``?tt=`` credentials on a Tailscale LE cert.
All cert fixtures are minted in-test: the repo's live cert.pem must never
decide a test's outcome (it legitimately flips self-signed → LE).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

from app.webapp.routers import _helpers
from app.webapp.routers._helpers import (
    mirror_url,
    should_mirror_to_pc,
    tsnet_host_from_cert,
)
from src.webauthn_gate import WebAuthnGate


def _request(host: str) -> SimpleNamespace:
    """Minimal stand-in for a Starlette Request: only `.client.host` is read."""
    return SimpleNamespace(client=SimpleNamespace(host=host))


def test_phone_launch_opens_mirror() -> None:
    # Non-loopback (a phone over the tunnel/tailnet), no desktop flag → mirror.
    assert should_mirror_to_pc(True, _request("100.64.0.5"), {}) is True


def test_desktop_flag_opens_mirror_over_tunnel() -> None:
    # Issue #241: a desktop browser gets a dedicated PC Edge window, not an
    # in-page terminal — over the tunnel (non-loopback) it mirrors.
    assert (
        should_mirror_to_pc(True, _request("100.64.0.5"), {"desktop": True})
        is True
    )


def test_desktop_flag_opens_mirror_over_loopback() -> None:
    # Issue #241, the user's exact scenario: desktop Chrome on the PC itself
    # (loopback) must still get its own Edge window so Stop & Close never
    # tears down the controlling browser. The desktop flag wins over the IP.
    for host in ("127.0.0.1", "::1", "localhost"):
        assert (
            should_mirror_to_pc(True, _request(host), {"desktop": True}) is True
        )


def test_loopback_launch_without_desktop_flag_skips_mirror() -> None:
    # A loopback client that did NOT flag itself a desktop (the rare coarse-
    # pointer PC browser) still renders in-page — harmless now that an in-page
    # loopback terminal is no longer mis-classified as a mirror (issue #241).
    for host in ("127.0.0.1", "::1", "localhost"):
        assert should_mirror_to_pc(True, _request(host), {}) is False


def test_disabled_flag_never_mirrors() -> None:
    # claude_show_local_window off → never open the mirror, even for a phone.
    assert should_mirror_to_pc(False, _request("100.64.0.5"), {}) is False


# --------------------------------------------------- tsnet_host_from_cert


def _make_cert(path: Path, issuer_org: str, san_names: list) -> None:
    """Mint a throwaway cert whose issuer org + SAN we control."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test")])
    issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, "test-issuer"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, issuer_org),
        ]
    )
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(n) for n in san_names]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    from cryptography.hazmat.primitives import serialization

    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def test_tsnet_host_ignores_self_signed_cert_with_tsnet_san(tmp_path) -> None:
    # The self-signed leaf (gen_ssl_cert.py) carries the ts.net SAN too —
    # issuer keying is what stops it being treated as a Tailscale cert (#354).
    p = tmp_path / "cert.pem"
    _make_cert(p, "Launcher", ["localhost", "tower.tail1121fd.ts.net"])
    assert tsnet_host_from_cert(p) is None


def test_tsnet_host_detects_lets_encrypt_cert(tmp_path) -> None:
    p = tmp_path / "cert.pem"
    _make_cert(p, "Let's Encrypt", ["tower.tail1121fd.ts.net"])
    assert tsnet_host_from_cert(p) == "tower.tail1121fd.ts.net"


def test_tsnet_host_missing_cert_is_none(tmp_path) -> None:
    assert tsnet_host_from_cert(tmp_path / "nope.pem") is None


# --------------------------------------------------------------- mirror_url


def _cfg(**over) -> SimpleNamespace:
    base = dict(
        port=8445, auth_token="", webauthn_rp_id="", webauthn_rp_name="",
        webauthn_origin="",
    )
    base.update(over)
    return SimpleNamespace(**base)


def _request_with_gate(gate) -> SimpleNamespace:
    return SimpleNamespace(
        client=SimpleNamespace(host="100.64.0.5"),
        app=SimpleNamespace(state=SimpleNamespace(webauthn_gate=gate)),
    )


def test_mirror_url_loopback_on_self_signed(monkeypatch) -> None:
    # No Tailscale cert → today's behavior, byte-identical: loopback rides
    # the auth bypass, no credentials in the URL.
    monkeypatch.setattr(_helpers, "tsnet_host_from_cert", lambda: None)
    monkeypatch.setattr(_helpers, "cert_present", lambda: True)
    url = mirror_url(_request_with_gate(None), _cfg(auth_token="sekrit"), "abc")
    assert url == "https://127.0.0.1:8445/?terminal=abc"


def test_mirror_url_tsnet_with_token(monkeypatch) -> None:
    monkeypatch.setattr(
        _helpers, "tsnet_host_from_cert", lambda: "tower.tail1121fd.ts.net"
    )
    url = mirror_url(_request_with_gate(None), _cfg(auth_token="sekrit"), "abc")
    parsed = urlparse(url)
    q = parse_qs(parsed.query)
    assert parsed.scheme == "https"
    assert parsed.netloc == "tower.tail1121fd.ts.net:8445"
    assert q["terminal"] == ["abc"]
    assert q["token"] == ["sekrit"]
    assert "tt" not in q  # passkey gate unconfigured → no terminal token


def test_mirror_url_tsnet_mints_terminal_token_when_gate_configured(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(
        _helpers, "tsnet_host_from_cert", lambda: "tower.tail1121fd.ts.net"
    )
    gate = WebAuthnGate(devices_path=tmp_path / "devices.json")
    cfg = _cfg(
        auth_token="sekrit",
        webauthn_rp_id="tower.tail1121fd.ts.net",
        webauthn_origin="https://tower.tail1121fd.ts.net:8445",
    )
    url = mirror_url(_request_with_gate(gate), cfg, "abc")
    q = parse_qs(urlparse(url).query)
    # The minted token is real: the gate itself must validate it, so the
    # mirror window's terminal WS passes the passkey leg without a ceremony.
    assert gate.valid_terminal_token(q["tt"][0]) is True
