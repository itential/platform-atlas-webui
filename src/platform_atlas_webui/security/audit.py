"""Append-only audit log for state-changing WebUI requests."""

from __future__ import annotations

import getpass
import hashlib
import json
import logging
import logging.handlers
import os
import stat
from pathlib import Path

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from platform_atlas.core.paths import ATLAS_HOME
from platform_atlas_webui.security.redact import redact
from platform_atlas_webui.security.tokens import COOKIE_NAME

AUDIT_LOG_FILE = ATLAS_HOME / "webui-audit.log"

_AUDIT_METHODS = frozenset({"POST", "PATCH", "PUT", "DELETE"})
_LOG_MAX_BYTES = 10 * 1024 * 1024   # 10 MB
_LOG_BACKUP_COUNT = 5

# Routes whose form body is interesting enough to capture as `changes`.
# Keyed by path prefix — matched with startswith.
_CAPTURE_BODY_PREFIXES = frozenset({
    "/config",
    "/tier",
    "/environments",
    "/setup",
    "/continuous",
    "/alerts",
})

_audit_logger: logging.Logger | None = None


def _get_audit_logger() -> logging.Logger:
    global _audit_logger
    if _audit_logger is not None:
        return _audit_logger

    ATLAS_HOME.mkdir(mode=0o700, exist_ok=True)

    # Touch the file at 0600 before the handler opens it.
    if not AUDIT_LOG_FILE.exists():
        AUDIT_LOG_FILE.touch(mode=0o600)
    elif os.name == "posix":
        current = stat.S_IMODE(AUDIT_LOG_FILE.stat().st_mode)
        if current != 0o600:
            AUDIT_LOG_FILE.chmod(0o600)

    handler = logging.handlers.RotatingFileHandler(
        str(AUDIT_LOG_FILE),
        maxBytes=_LOG_MAX_BYTES,
        backupCount=_LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(message)s"))

    lgr = logging.getLogger("platform_atlas_webui.audit")
    lgr.setLevel(logging.INFO)
    lgr.addHandler(handler)
    lgr.propagate = False   # keep audit lines out of the general log stream
    _audit_logger = lgr
    return lgr


def _os_user() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return os.environ.get("USER", os.environ.get("USERNAME", "unknown"))


def _session_hint(cookie_value: str | None) -> str:
    """Return the first 8 hex chars of SHA-256(cookie) as a session identifier."""
    if not cookie_value:
        return ""
    return hashlib.sha256(cookie_value.encode()).hexdigest()[:8]


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    client = request.client
    return client.host if client else ""


class AuditMiddleware(BaseHTTPMiddleware):
    """Write one JSON line per state-changing request to the audit log."""

    async def dispatch(self, request: Request, call_next) -> Response:
        if request.method not in _AUDIT_METHODS:
            return await call_next(request)

        # Opportunistically capture form body for config-touching routes.
        changes: dict | None = None
        path = request.url.path or "/"
        if any(path == p or path.startswith(p + "/") for p in _CAPTURE_BODY_PREFIXES):
            content_type = request.headers.get("content-type", "")
            if "application/x-www-form-urlencoded" in content_type:
                try:
                    body = await request.body()
                    from urllib.parse import parse_qs
                    raw = {
                        k: v[0] if len(v) == 1 else v
                        for k, v in parse_qs(body.decode(errors="replace")).items()
                        if k != "csrf_token"
                    }
                    changes = redact(raw) if raw else None
                except Exception:
                    pass

        response = await call_next(request)

        try:
            cookie_value = request.cookies.get(COOKIE_NAME)
            entry: dict = {
                "ts": __import__("datetime").datetime.now(
                    __import__("datetime").timezone.utc
                ).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
                "user": _os_user(),
                "method": request.method,
                "path": path,
                "status": response.status_code,
                "client": _client_ip(request),
                "session_id": _session_hint(cookie_value),
            }
            if changes is not None:
                entry["changes"] = changes
            _get_audit_logger().info(json.dumps(entry, ensure_ascii=False))
        except Exception:
            pass  # audit must never crash the request

        return response
