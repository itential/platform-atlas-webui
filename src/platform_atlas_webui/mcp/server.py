"""
Builds the Atlas MCP server instance and its standalone ASGI app.

v1 tool set is read-only queries only — see design/MCP/*.md (platform-atlas
repo) for the full phasing rationale. Action tools (``run_audit`` and
friends) are deliberately deferred: every prior research/spike doc
independently flagged them as needing an async job/poll model plus a
concurrency guard around Atlas's process-global context singletons, and
recommended tackling them last, after the read-only surface is proven live.
"""

from __future__ import annotations

import asyncio
import logging

from fastmcp import FastMCP
from starlette.applications import Starlette

from platform_atlas_webui.mcp import tools
from platform_atlas_webui.mcp.audit import ToolCallAuditMiddleware
from platform_atlas_webui.mcp.auth import McpBearerTokenVerifier

logger = logging.getLogger(__name__)

MCP_SERVER_NAME = "atlas"
MCP_SERVER_INSTRUCTIONS = (
    "Query Platform Atlas compliance audit data: environments, sessions, "
    "fleet-wide rule failures, individual rule rationale, and session-to-session "
    "diffs. Read-only — never modifies the audited Platform 6 deployment."
)


def _build_mcp(*, no_auth: bool = False) -> FastMCP:
    mcp = FastMCP(
        name=MCP_SERVER_NAME,
        instructions=MCP_SERVER_INSTRUCTIONS,
        auth=None if no_auth else McpBearerTokenVerifier(),
        middleware=[ToolCallAuditMiddleware()],
    )
    mcp.tool(tools.list_environments)
    mcp.tool(tools.list_sessions)
    mcp.tool(tools.get_compliance_summary)
    mcp.tool(tools.fleet_top_fix)
    mcp.tool(tools.explain_rule)
    mcp.tool(tools.diff_sessions)
    mcp.tool(tools.rule_fleet_distribution)
    mcp.tool(tools.fleet_severity_breakdown)
    mcp.tool(tools.rule_category_health)
    mcp.tool(tools.stale_environments)
    mcp.tool(tools.session_history_trend)
    mcp.tool(tools.flaky_rules)
    mcp.tool(tools.compare_environments)
    mcp.tool(tools.skip_reason_breakdown)
    mcp.tool(tools.fleet_tier_coverage)
    mcp.tool(tools.fleet_regressions_since_last_audit)
    return mcp


async def _apply_gateway5_schema_workaround(mcp: FastMCP) -> None:
    """Force ``required: []`` on every zero-required-arg tool's schema.

    Defensive, kept regardless of whether the currently pinned fastmcp
    version already generates a clean schema for zero-arg tools (confirmed
    it does on both 3.4.7 and 4.0.10 — no ``required`` key at all rather than the old
    ``"required": null``). The known failure was Gateway5's own HTTP-layer
    re-serialization of an omitted key as literal ``null``, which FlowAI's
    validator rejects (``"data/required must be array"``) — that happens on
    Gateway5's side, so it can't be verified from this process alone. Cheap
    insurance either way.
    """
    for tool in await mcp.list_tools():
        tool.parameters.setdefault("required", [])


def build_mcp_app(*, no_auth: bool = False) -> Starlette:
    """Build the Atlas MCP server and return its standalone ASGI app.

    The returned app owns its own lifespan (starts/stops the streamable-HTTP
    session manager) — run it directly under uvicorn, don't mount it inside
    another ASGI app's route table without also forwarding that lifespan.
    """
    mcp = _build_mcp(no_auth=no_auth)
    asyncio.run(_apply_gateway5_schema_workaround(mcp))
    return mcp.http_app(transport="streamable-http")
