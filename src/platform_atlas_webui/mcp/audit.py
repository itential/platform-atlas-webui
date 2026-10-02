"""
Tool-call audit log for the Atlas MCP server.

Same append-only JSON-lines pattern as `security/audit.py` (rotating file at
mode 0600), scoped to MCP tool invocations rather than HTTP routes. There's
no session cookie or form body in this protocol, so the shape differs
enough that reusing that middleware's `dispatch()` directly isn't a fit —
but the file/rotation mechanics are copied straight from it.
"""

from __future__ import annotations

import datetime
import getpass
import json
import logging
import logging.handlers
import os
import stat

import mcp.types as mt
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.base import ToolResult

from platform_atlas.core.paths import ATLAS_HOME
from platform_atlas_webui.security.redact import redact

MCP_AUDIT_LOG_FILE = ATLAS_HOME / "mcp-audit.log"

_LOG_MAX_BYTES = 10 * 1024 * 1024   # 10 MB
_LOG_BACKUP_COUNT = 5

_audit_logger: logging.Logger | None = None


def _get_audit_logger() -> logging.Logger:
    global _audit_logger
    if _audit_logger is not None:
        return _audit_logger

    ATLAS_HOME.mkdir(mode=0o700, exist_ok=True)
    if not MCP_AUDIT_LOG_FILE.exists():
        MCP_AUDIT_LOG_FILE.touch(mode=0o600)
    elif os.name == "posix":
        current = stat.S_IMODE(MCP_AUDIT_LOG_FILE.stat().st_mode)
        if current != 0o600:
            MCP_AUDIT_LOG_FILE.chmod(0o600)

    handler = logging.handlers.RotatingFileHandler(
        str(MCP_AUDIT_LOG_FILE),
        maxBytes=_LOG_MAX_BYTES,
        backupCount=_LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(message)s"))

    lgr = logging.getLogger("platform_atlas_webui.mcp_audit")
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


class ToolCallAuditMiddleware(Middleware):
    """Write one JSON line per MCP tool call: who, what, when, outcome."""

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        status = "ok"
        try:
            return await call_next(context)
        except Exception:
            status = "error"
            raise
        finally:
            try:
                entry = {
                    "ts": datetime.datetime.now(datetime.timezone.utc)
                        .strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
                    "user": _os_user(),
                    "tool": context.message.name,
                    "arguments": redact(dict(context.message.arguments or {})),
                    "status": status,
                }
                _get_audit_logger().info(json.dumps(entry, ensure_ascii=False))
            except Exception:
                pass  # audit must never crash a tool call
