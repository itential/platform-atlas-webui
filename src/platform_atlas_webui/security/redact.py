"""Credential redaction helpers for log output."""

from __future__ import annotations

import logging
import re
from typing import Any

SENSITIVE_KEYS: frozenset[str] = frozenset({
    "vault_token",
    "token",
    "wrapping_token",
    "platform_client_secret",
    "platform_secret",
    "gateway4_password",
    "iag5_password",
    "secret_id",
    "role_id",
    "csrf_token",
    "ssh_passphrase",
    "ssh_password",
    "saas_ssh_passphrase",
    "saas_ssh_password",
    "saas_gw4_password",
    "vault_secret_id",
    "vault_role_id",
    "vault_wrapping_token",
})

# Pattern-based matching (SEC-05) so future fields are covered without editing
# the list. Field names are normalized (camelCase -> snake_case, lowercased,
# separators collapsed) and matched against these substrings.
_SENSITIVE_SUBSTRINGS: tuple[str, ...] = (
    "password", "passwd", "passphrase", "secret", "token", "apikey", "api_key",
    "private_key", "credential", "requirepass", "masterauth", "role_id",
)
# Names that merely *point at* a secret (a file path, a store selector) are not secrets.
_SAFE_SUFFIXES: tuple[str, ...] = ("_path", "_file", "_store", "_backend", "_method", "_name")

_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def is_sensitive_key(key: Any) -> bool:
    """True when a field name looks like it carries a secret."""
    if not isinstance(key, str):
        return False
    if key in SENSITIVE_KEYS:
        return True
    norm = re.sub(r"[^a-z0-9]+", "_", _CAMEL_RE.sub("_", key).lower()).strip("_")
    if norm in SENSITIVE_KEYS:
        return True
    if norm.endswith(_SAFE_SUFFIXES):
        return False
    return any(sub in norm for sub in _SENSITIVE_SUBSTRINGS)

_REDACTED = "***REDACTED***"


def redact(payload: Any) -> Any:
    """Return a deep copy of *payload* with sensitive keys replaced by ``***REDACTED***``.

    Works on dicts (recursively), lists, and scalars. The redacted value is a
    non-empty string so debug logs can still see that the field was present.
    """
    if isinstance(payload, dict):
        return {
            k: _REDACTED if is_sensitive_key(k) else redact(v)
            for k, v in payload.items()
        }
    if isinstance(payload, list):
        return [redact(item) for item in payload]
    return payload


class _StripQueryStringFilter(logging.Filter):
    """Drop query strings from uvicorn access log records.

    Uvicorn passes the full request line (e.g. ``"GET /path?q=secret HTTP/1.1"``)
    as the third element of ``record.args``. We strip everything after ``?``
    so credentials that accidentally end up in a query parameter never reach
    the log file.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and len(record.args) >= 3:
            path = record.args[2]
            if isinstance(path, str) and "?" in path:
                record.args = (
                    record.args[0],
                    record.args[1],
                    path.split("?", 1)[0],
                    *record.args[3:],
                )
        return True


def install_uvicorn_access_filter() -> None:
    """Attach the query-string-stripping filter to the uvicorn access logger."""
    logging.getLogger("uvicorn.access").addFilter(_StripQueryStringFilter())
