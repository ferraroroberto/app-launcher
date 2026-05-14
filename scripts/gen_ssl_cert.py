"""Generate a self-signed CA + leaf cert for the webapp's HTTPS endpoint.

Output goes to ``webapp/certificates/``:

    ca.pem                              local CA certificate
    ca.key                              local CA private key
    cert.pem                            server cert signed by the CA
    key.pem                             server private key (uvicorn reads this)

Used only for the local + tailnet HTTPS endpoint. Remote access goes
through Cloudflare which provides its own public TLS — phones and
remote PCs never see this cert.

The leaf cert's SAN list includes 127.0.0.1, ::1, localhost, the
machine's hostname, the tailscale hostname (when ``tailscale`` is on
PATH), and any IPv4 addresses bound on local interfaces. Re-run this
script if those change and restart the webapp.

On Windows the script also installs ``ca.pem`` into the user's
``CurrentUser\\Root`` trust store via ``certutil`` so Edge/Chrome on
this PC trust it without admin rights.

Usage:
    python scripts/gen_ssl_cert.py
    python scripts/gen_ssl_cert.py --skip-install   # don't touch trust store
"""

from __future__ import annotations

import argparse
import base64
import ipaddress
import logging
import platform
import socket
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Set

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

logger = logging.getLogger("gen_ssl_cert")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CERT_DIR = PROJECT_ROOT / "webapp" / "certificates"
STATIC_DIR = PROJECT_ROOT / "app" / "webapp" / "static"
MOBILECONFIG_FILENAME = "launcher-ca.mobileconfig"

CA_COMMON_NAME = "Launcher Local CA"
CA_ORG = "Launcher"
CERT_VALIDITY_YEARS = 10


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-install",
        action="store_true",
        help="Skip installing the CA into the Windows user trust store",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=CERT_DIR,
        help="Where to write ca.pem / cert.pem / key.pem (default: webapp/certificates/)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    hostnames = _local_hostnames()
    ip_addresses = _local_ip_addresses()
    logger.info(f"🔎 SAN hostnames: {sorted(hostnames)}")
    logger.info(f"🔎 SAN IPs      : {sorted(str(a) for a in ip_addresses)}")

    ca_key, ca_cert = _build_ca()
    leaf_key, leaf_cert = _build_leaf(ca_key, ca_cert, hostnames, ip_addresses)

    _write_pem(out_dir / "ca.pem", ca_cert.public_bytes(serialization.Encoding.PEM))
    _write_pem(
        out_dir / "ca.key",
        ca_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        ),
    )
    _write_pem(out_dir / "cert.pem", leaf_cert.public_bytes(serialization.Encoding.PEM))
    _write_pem(
        out_dir / "key.pem",
        leaf_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        ),
    )

    profile_bytes = _build_mobileconfig(ca_cert)
    profile_path = out_dir / MOBILECONFIG_FILENAME
    profile_path.write_bytes(profile_bytes)
    logger.info(f"📱 wrote {profile_path}")

    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    static_profile = STATIC_DIR / MOBILECONFIG_FILENAME
    static_profile.write_bytes(profile_bytes)
    static_ca = STATIC_DIR / "ca.crt"
    static_ca.write_bytes(ca_cert.public_bytes(serialization.Encoding.DER))
    logger.info(f"📱 mirrored profile → {static_profile}")
    logger.info(f"🤖 wrote Android-friendly DER → {static_ca}")

    if not args.skip_install and platform.system() == "Windows":
        _install_windows_trust(out_dir / "ca.pem")

    logger.info("")
    logger.info("✅ Done. Next steps:")
    logger.info("   • Restart webapp.bat / tray.bat — uvicorn will pick up the new cert.")
    logger.info("   • iOS: open  https://<host>:8445/install-ca")
    logger.info("     then Settings → General → VPN & Device Management → install profile,")
    logger.info("     then Settings → General → About → Certificate Trust Settings → enable.")

    return 0


# ------------------------------------------------------ host discovery


def _local_hostnames() -> Set[str]:
    names: Set[str] = {"localhost"}
    try:
        names.add(socket.gethostname())
    except OSError:
        pass
    try:
        names.add(socket.getfqdn())
    except OSError:
        pass
    try:
        result = subprocess.run(
            ["tailscale", "status", "--self=true", "--peers=false", "--json"],
            capture_output=True,
            text=True,
            timeout=4,
            check=False,
        )
        if result.returncode == 0:
            import json
            data = json.loads(result.stdout)
            self_node = data.get("Self") or {}
            dns = self_node.get("DNSName") or ""
            if dns:
                names.add(dns.rstrip("."))
                short = dns.split(".")[0]
                if short:
                    names.add(short)
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError, OSError):
        pass
    return {n for n in names if n}


def _local_ip_addresses() -> Set[ipaddress.IPv4Address]:
    addrs: Set[ipaddress.IPv4Address] = {
        ipaddress.IPv4Address("127.0.0.1"),
    }
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            family, _, _, _, sockaddr = info
            if family == socket.AF_INET:
                ip = sockaddr[0]
                try:
                    addrs.add(ipaddress.IPv4Address(ip))
                except ValueError:
                    continue
    except (socket.gaierror, OSError):
        pass

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(0.5)
            s.connect(("8.8.8.8", 80))
            addrs.add(ipaddress.IPv4Address(s.getsockname()[0]))
    except OSError:
        pass

    try:
        result = subprocess.run(
            ["tailscale", "ip", "-4"],
            capture_output=True,
            text=True,
            timeout=4,
            check=False,
        )
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                addrs.add(ipaddress.IPv4Address(line))
            except ValueError:
                continue
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass

    return addrs


# ------------------------------------------------------ cert builders


def _build_ca():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, CA_COMMON_NAME),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, CA_ORG),
    ])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=365 * CERT_VALIDITY_YEARS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    return key, cert


def _build_leaf(ca_key, ca_cert, hostnames: Set[str], ips: Set[ipaddress.IPv4Address]):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "launcher.local"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, CA_ORG),
    ])
    san_entries: List[x509.GeneralName] = []
    for h in hostnames:
        san_entries.append(x509.DNSName(h))
    for a in ips:
        san_entries.append(x509.IPAddress(a))

    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=365 * CERT_VALIDITY_YEARS))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName(san_entries), critical=False)
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    return key, cert


def _write_pem(path: Path, blob: bytes) -> None:
    path.write_bytes(blob)
    logger.info(f"💾 wrote {path}")


# ------------------------------------------------------ mobileconfig


def _build_mobileconfig(ca_cert) -> bytes:
    der = ca_cert.public_bytes(serialization.Encoding.DER)
    cert_b64 = base64.b64encode(der).decode("ascii")
    cert_b64_chunks = "\n".join(
        cert_b64[i : i + 64] for i in range(0, len(cert_b64), 64)
    )

    payload_uuid = str(uuid.uuid4()).upper()
    profile_uuid = str(uuid.uuid4()).upper()

    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>PayloadContent</key>
    <array>
        <dict>
            <key>PayloadCertificateFileName</key>
            <string>launcher-ca.cer</string>
            <key>PayloadContent</key>
            <data>
{cert_b64_chunks}
            </data>
            <key>PayloadDescription</key>
            <string>Adds the Launcher local CA to the iOS trust store.</string>
            <key>PayloadDisplayName</key>
            <string>{CA_COMMON_NAME}</string>
            <key>PayloadIdentifier</key>
            <string>com.launcher.localca.cert.{payload_uuid}</string>
            <key>PayloadType</key>
            <string>com.apple.security.root</string>
            <key>PayloadUUID</key>
            <string>{payload_uuid}</string>
            <key>PayloadVersion</key>
            <integer>1</integer>
        </dict>
    </array>
    <key>PayloadDescription</key>
    <string>Trust profile for self-signed Launcher webapp on this LAN.</string>
    <key>PayloadDisplayName</key>
    <string>Launcher Trust</string>
    <key>PayloadIdentifier</key>
    <string>com.launcher.localca.profile.{profile_uuid}</string>
    <key>PayloadOrganization</key>
    <string>{CA_ORG}</string>
    <key>PayloadRemovalDisallowed</key>
    <false/>
    <key>PayloadType</key>
    <string>Configuration</string>
    <key>PayloadUUID</key>
    <string>{profile_uuid}</string>
    <key>PayloadVersion</key>
    <integer>1</integer>
</dict>
</plist>
"""
    return plist.encode("utf-8")


# ------------------------------------------------------ trust store


def _install_windows_trust(ca_pem: Path) -> None:
    try:
        result = subprocess.run(
            ["certutil", "-user", "-addstore", "Root", str(ca_pem)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            logger.info(f"🛡️  Installed CA into Windows CurrentUser\\Root")
        else:
            logger.warning(
                f"⚠️  certutil exit {result.returncode}: "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
    except FileNotFoundError:
        logger.warning("⚠️  certutil not found on PATH — skipping Windows trust install")


if __name__ == "__main__":
    sys.exit(main())
