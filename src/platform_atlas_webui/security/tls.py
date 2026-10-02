"""TLS certificate management — generate, persist, load, and fingerprint."""

from __future__ import annotations

import datetime
import hashlib
import ipaddress
import os
import socket
import stat
from pathlib import Path

from platform_atlas.core.paths import ATLAS_HOME

CERT_FILE = ATLAS_HOME / ".webui-cert.pem"
KEY_FILE = ATLAS_HOME / ".webui-key.pem"

_600 = stat.S_IRUSR | stat.S_IWUSR


def _enforce_600(path: Path) -> None:
    """Tighten permissions to 0600 if they are looser (POSIX only)."""
    if os.name != "posix":
        return
    current = stat.S_IMODE(path.stat().st_mode)
    if current != 0o600:
        path.chmod(0o600)


def _generate_cert(extra_sans: set[str] | None = None) -> None:
    """Generate a self-signed RSA-2048 cert+key and write them to ~/.atlas/.

    ``extra_sans`` adds hostnames/IPs beyond the always-included loopback
    set — needed because the same cert is shared between the browser UI
    (loopback by default) and the MCP server (routinely bound to a LAN IP
    for Gateway5 to reach). Each value is tried as an IP literal first,
    falling back to a DNS name.
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "localhost"),
    ])

    san_entries: list[x509.GeneralName] = [
        x509.DNSName("localhost"),
        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
        x509.IPAddress(ipaddress.ip_address("::1")),
    ]
    # Callers may pass a superset that already includes the base loopback
    # set (e.g. ensure_cert() unions in whatever the current cert already
    # covers) — skip those here so the cert doesn't carry duplicate entries.
    already_covered = {"localhost", "127.0.0.1", "::1"}
    for value in sorted(extra_sans or ()):
        if value in already_covered:
            continue
        try:
            san_entries.append(x509.IPAddress(ipaddress.ip_address(value)))
        except ValueError:
            san_entries.append(x509.DNSName(value))

    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=365))
        .add_extension(x509.SubjectAlternativeName(san_entries), critical=False)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_encipherment=True,
                content_commitment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )

    ATLAS_HOME.mkdir(mode=0o700, exist_ok=True)

    # Write key first (most sensitive)
    KEY_FILE.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    if os.name == "posix":
        KEY_FILE.chmod(0o600)

    CERT_FILE.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    if os.name == "posix":
        CERT_FILE.chmod(0o600)


def _cert_expiry() -> datetime.datetime | None:
    """Return the notAfter datetime of the on-disk cert, or None on error."""
    try:
        from cryptography import x509
        cert = x509.load_pem_x509_certificate(CERT_FILE.read_bytes())
        return cert.not_valid_after_utc
    except Exception:
        return None


def _existing_sans() -> set[str]:
    """Return the SAN entries (as plain strings) baked into the on-disk cert."""
    try:
        from cryptography import x509
        cert = x509.load_pem_x509_certificate(CERT_FILE.read_bytes())
        san_ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        values: set[str] = set()
        for entry in san_ext:
            if isinstance(entry, x509.DNSName):
                values.add(entry.value)
            elif isinstance(entry, x509.IPAddress):
                values.add(str(entry.value))
        return values
    except Exception:
        return set()


def _fingerprint() -> str:
    """Return the SHA-256 fingerprint of the on-disk cert as AA:BB:CC... hex."""
    der = None
    try:
        from cryptography import x509
        cert = x509.load_pem_x509_certificate(CERT_FILE.read_bytes())
        der = cert.public_bytes(__import__("cryptography.hazmat.primitives.serialization", fromlist=["Encoding"]).Encoding.DER)
    except Exception:
        # Fallback: hash the raw PEM bytes
        der = CERT_FILE.read_bytes()
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i:i+2] for i in range(0, len(digest), 2))


def ensure_cert(*, reset: bool = False, extra_sans: set[str] | None = None) -> tuple[str, bool]:
    """Ensure a valid cert+key exist on disk, covering ``extra_sans``.

    If *reset* is True, regenerate unconditionally. Otherwise, also
    regenerates (preserving any SANs already on the cert, unioned with
    ``extra_sans``) whenever a requested SAN isn't already covered — e.g.
    the first time ``--mcp-server --host 192.168.2.104`` is used, so a
    LAN-bound server doesn't require a separate manual ``--reset-tls`` step
    to pick up its own bind address.

    Returns ``(fingerprint, newly_generated)`` where ``newly_generated`` is
    True when a cert was just created (triggers the verbose console block).
    """
    requested = {s.strip() for s in (extra_sans or ()) if s and s.strip()}
    # Always already covered — no need to trigger a regen for these alone.
    requested -= {"127.0.0.1", "::1", "localhost"}

    if reset:
        CERT_FILE.unlink(missing_ok=True)
        KEY_FILE.unlink(missing_ok=True)

    newly_generated = False

    if not CERT_FILE.is_file() or not KEY_FILE.is_file():
        _generate_cert(requested)
        newly_generated = True
    else:
        # Tighten perms if needed, then check expiry and SAN coverage.
        _enforce_600(CERT_FILE)
        _enforce_600(KEY_FILE)
        expiry = _cert_expiry()
        existing = _existing_sans()
        expired = expiry is None or expiry <= datetime.datetime.now(datetime.timezone.utc)
        if expired or (requested - existing):
            _generate_cert(existing | requested)
            newly_generated = True

    return _fingerprint(), newly_generated


def expiry_date() -> str:
    """Return the cert expiry as a human-readable date string, or '?'."""
    expiry = _cert_expiry()
    if expiry is None:
        return "?"
    return expiry.strftime("%Y-%m-%d")
