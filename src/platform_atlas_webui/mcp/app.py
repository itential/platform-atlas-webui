"""
Entry point for MCP mode's ASGI app — the ``--mcp-server`` counterpart to
``platform_atlas_webui.app.create_app()``.

Deliberately minimal compared to the browser app: no Jinja templates, no
session-cookie auth/CSRF, no static files, none of the 20 browser routers.
Auth (bearer token) and tool-call audit logging are wired into the FastMCP
instance itself in ``mcp/server.py`` — this module's only remaining job is
process-wide log hygiene shared with the browser app.
"""

from __future__ import annotations

from starlette.applications import Starlette

from platform_atlas_webui.mcp.server import build_mcp_app
from platform_atlas_webui.security.redact import install_uvicorn_access_filter


def create_mcp_app(*, no_auth: bool = False) -> Starlette:
    """Build the Atlas MCP server's standalone ASGI app.

    Run the result directly under uvicorn (``uvicorn.run(create_mcp_app())``)
    — it owns its own lifespan, so don't mount it inside another ASGI app
    without also forwarding that lifespan (see ``mcp/server.py``).

    ``no_auth`` disables the bearer-token verifier entirely (``--no-auth``)
    — every request is accepted unauthenticated.
    """
    install_uvicorn_access_filter()
    return build_mcp_app(no_auth=no_auth)
