"""
Bearer-token auth for the Atlas MCP server.

Static shared-secret model, not OAuth: Gateway5/FlowMCP Gateway registration
(``iagctl mcp server add ... --header "Authorization: Bearer {{ secret ... }}"``)
presents the same token on every request rather than performing a token
exchange, so ``TokenVerifier`` — fastmcp's resource-server-only base class,
with no client-registration/auth-code endpoints — is the right fit rather
than a full OAuth provider.

The token itself is `security/tokens.py`'s ``load_mcp_token()``: a 32-byte
secret persisted at ``~/.atlas/.mcp-token`` (mode 0600), same pattern as the
browser UI's own ``.webui-token``, rotatable independently via
``--reset-mcp-token`` without logging out any browser session.
"""

from __future__ import annotations

import hmac

from fastmcp.server.auth import AccessToken, TokenVerifier

from platform_atlas_webui.security.tokens import load_mcp_token


class McpBearerTokenVerifier(TokenVerifier):
    """Validates the single shared MCP bearer token via constant-time compare."""

    async def verify_token(self, token: str) -> AccessToken | None:
        expected = load_mcp_token().hex()
        if not hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8")):
            return None
        return AccessToken(token=token, client_id="atlas-mcp-client", scopes=[])
