"""OS-user binding — filesystem token, nonce generation/validation, session cookies."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import stat
import threading
import time
from pathlib import Path
from typing import Optional

from platform_atlas.core.paths import ATLAS_HOME

TOKEN_FILE = ATLAS_HOME / ".webui-token"
SECRET_FILE = ATLAS_HOME / ".webui-secret"
COOKIE_SECRET_FILE = ATLAS_HOME / ".webui-cookie-secret"

COOKIE_NAME = "atlas_session"
_NONCE_TTL = 60          # seconds
_COOKIE_MAX_AGE = 60 * 60 * 24 * 30  # 30 days

# Cookie-signing secret. Persisted to disk (mode 0600, owner-only) so
# session cookies survive process restarts — auto-restart by systemd,
# `service restart`, machine reboot, anything. Same threat model as
# .webui-token (and every other secret under ~/.atlas/): only readable
# by the OS user who owns this directory. Matches the on-disk-secret
# pattern used by Jupyter, code-server, Grafana, et al.
#
# A previous iteration kept this in process memory for additional
# defense-in-depth against hostile pip post-installs running as the
# same user. That extra hardening invalidated every browser session
# on every restart, which made the WebUI effectively unusable as a
# managed service — the same threat actor can already replace
# `platform-atlas-webui` on PATH, exfiltrate every other secret in
# ~/.atlas/, and tamper with shell rc files, so the marginal value
# wasn't worth the operational pain.

# In-memory set of consumed nonces: (nonce_str, expiry_epoch).
# We only keep entries for the TTL window; cleanup happens on each validation.
# Locked because middleware runs concurrent requests under the same loop and
# two parallel /auth?nonce=… racers must not both win.
_used_nonces: set[str] = set()
_nonce_expiry: dict[str, float] = {}
_nonce_lock = threading.Lock()


# ── Helpers ────────────────────────────────────────────────────────────────

def _enforce_600(path: Path) -> None:
    if os.name != "posix":
        return
    current = stat.S_IMODE(path.stat().st_mode)
    if current != 0o600:
        path.chmod(0o600)


def _write_secret(path: Path) -> bytes:
    """Generate 32 random bytes, write hex-encoded to path at 0600."""
    ATLAS_HOME.mkdir(mode=0o700, exist_ok=True)
    raw = secrets.token_bytes(32)
    path.write_text(raw.hex(), encoding="ascii")
    if os.name == "posix":
        path.chmod(0o600)
    return raw


def _load_secret(path: Path) -> bytes:
    """Read 32-byte hex secret from path, generating if missing."""
    if not path.is_file():
        return _write_secret(path)
    _enforce_600(path)
    try:
        return bytes.fromhex(path.read_text(encoding="ascii").strip())
    except (ValueError, OSError):
        return _write_secret(path)


# ── Public API ─────────────────────────────────────────────────────────────

def load_token() -> bytes:
    """Return the OS-user binding token, generating if missing."""
    return _load_secret(TOKEN_FILE)


def load_secret() -> bytes:
    """Return the HMAC seed used for CSRF and cookies, generating if missing."""
    return _load_secret(SECRET_FILE)


def load_cookie_secret() -> bytes:
    """Return the persistent cookie-signing secret, generating if missing."""
    return _load_secret(COOKIE_SECRET_FILE)


def reset_token() -> None:
    """Regenerate the binding token — invalidates all outstanding cookies.

    Touches only the binding token. Cookie keys are derived from
    ``token ⊕ cookie_secret`` so rotating the token alone is enough to
    void every issued cookie.
    """
    TOKEN_FILE.unlink(missing_ok=True)
    _write_secret(TOKEN_FILE)


def reset_cookie_secret() -> None:
    """Regenerate the cookie-signing secret — invalidates all outstanding cookies.

    Provided for completeness alongside ``reset_token``. Either rotation
    is sufficient to log out every browser; rotating both is paranoia.
    """
    COOKIE_SECRET_FILE.unlink(missing_ok=True)
    _write_secret(COOKIE_SECRET_FILE)


def generate_nonce() -> str:
    """Return a one-time login nonce valid for 60 seconds.

    Format: ``{hmac_hex}{message_hex}``
    where message = ``{timestamp}:{random_hex}`` and hmac = HMAC-SHA256(token, message).
    All hex, URL-safe, no separators needed (HMAC is always 64 hex chars).
    """
    token = load_token()
    ts = str(int(time.time()))
    rand = secrets.token_hex(16)
    message = f"{ts}:{rand}"
    mac = hmac.new(token, message.encode(), hashlib.sha256).hexdigest()
    return mac + message.encode().hex()


def validate_nonce(nonce: str) -> bool:
    """Validate a nonce from /auth?nonce=<...>.

    Returns True if the nonce is fresh, cryptographically valid, and not
    already used.  Marks it as used on success.
    """
    if len(nonce) < 65:          # 64 hex chars for HMAC + at least 1 char message
        return False

    mac_part = nonce[:64]
    try:
        message = bytes.fromhex(nonce[64:]).decode()
        ts_str, _rand = message.split(":", 1)
        ts = int(ts_str)
    except (ValueError, UnicodeDecodeError):
        return False

    now = time.time()
    if abs(now - ts) > _NONCE_TTL:
        return False

    token = load_token()
    expected = hmac.new(token, message.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, mac_part):
        return False

    # Lock the burn-and-check: two parallel requests with the same nonce
    # must not both succeed. Combined with HMAC verification done outside
    # the lock, the critical section stays small.
    with _nonce_lock:
        # Purge expired entries first so the set doesn't grow unbounded.
        expired = [n for n, exp in _nonce_expiry.items() if exp <= now]
        for n in expired:
            _used_nonces.discard(n)
            _nonce_expiry.pop(n, None)

        if nonce in _used_nonces:
            return False
        _used_nonces.add(nonce)
        _nonce_expiry[nonce] = ts + _NONCE_TTL
    return True


def _cookie_key() -> bytes:
    """HMAC key for session cookies = sha256(token ⊕ cookie_secret).

    Both inputs live under ``~/.atlas/`` at mode 0600 (owner-only). Cookies
    therefore remain valid for the full ``_COOKIE_MAX_AGE`` (30 days)
    regardless of how often the WebUI process restarts. Rotating either
    file invalidates every outstanding cookie immediately.
    """
    return hashlib.sha256(load_token() + load_cookie_secret()).digest()


def make_session_cookie_value(session_id: str) -> str:
    """Return the signed value to store in the session cookie.

    Format: ``{session_id}:{mac}``
    """
    mac = hmac.new(_cookie_key(), session_id.encode(), hashlib.sha256).hexdigest()
    return f"{session_id}:{mac}"


def validate_session_cookie(cookie_value: Optional[str]) -> bool:
    """Return True if the cookie was signed by the current process."""
    if not cookie_value:
        return False
    try:
        session_id, mac = cookie_value.rsplit(":", 1)
    except ValueError:
        return False
    expected = hmac.new(_cookie_key(), session_id.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, mac)
