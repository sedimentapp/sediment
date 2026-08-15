"""GitHub OAuth-proxy auth provider.

Registered as "github" in the sediment_mcp.auth_providers entry-point group.
The server acts as an OAuth facade on its own origin (fastmcp OAuthProxy):
one pre-registered OAuth App on GitHub, clients get FastMCP-issued JWTs,
the upstream GitHub token never leaves the server.

Environment:
    MCP_BASE_URL                 public HTTPS origin of this server
    GITHUB_OAUTH_CLIENT_ID       GitHub OAuth App client id
    GITHUB_OAUTH_CLIENT_SECRET   GitHub OAuth App client secret
    MCP_GITHUB_ALLOWED_IDENTITIES comma-separated principal:GitHub-id entries
    MCP_ALLOWED_CLIENT_REDIRECT_URIS comma-separated MCP OAuth callback patterns
    MCP_AUTH_TOKEN_<PRINCIPAL>   optional named static bearer tokens accepted
                                 alongside OAuth (dev/emergency path); the
                                 suffix is the principal for ACL/attribution

The GitHub OAuth App must have its callback URL set to
<MCP_BASE_URL>/auth/callback.
"""

import os

from fastmcp.server.auth import AccessToken, AuthProvider, MultiAuth
from fastmcp.server.auth.providers.github import GitHubProvider
from fastmcp.utilities.logging import get_logger

from sediment_mcp.auth import StaticTokenVerifier, parse_github_identities, static_token_map
from sediment_mcp_ee_license import require_ee

logger = get_logger(__name__)


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is required for the github auth provider")
    return value


class AllowlistGitHubProvider(GitHubProvider):
    """GitHubProvider that accepts immutable ids and stamps a stable principal.

    Anyone with a GitHub account can complete the OAuth flow against our
    OAuth App, so possession of a valid upstream identity is not enough —
    the numeric GitHub id must also be allowlisted.
    """

    def __init__(self, *, allowed_identities: dict[str, str], **kwargs) -> None:
        super().__init__(**kwargs)
        self._allowed_identities = allowed_identities

    async def verify_token(self, token: str) -> AccessToken | None:
        access = await super().verify_token(token)
        if access is None:
            return None
        claims = access.claims or {}
        user_id = claims.get("sub")
        principal = self._allowed_identities.get(str(user_id))
        if principal is None:
            logger.warning(
                "Rejected GitHub user id %r (login=%r): not in MCP_GITHUB_ALLOWED_IDENTITIES",
                user_id,
                claims.get("login"),
            )
            return None
        return access.model_copy(update={"claims": {**claims, "principal": principal}})


def provider() -> AuthProvider:
    require_ee("auth-github")
    base_url = _require_env("MCP_BASE_URL")
    client_id = _require_env("GITHUB_OAUTH_CLIENT_ID")
    client_secret = _require_env("GITHUB_OAUTH_CLIENT_SECRET")
    allowed_identities = parse_github_identities(
        _require_env("MCP_GITHUB_ALLOWED_IDENTITIES"),
        "MCP_GITHUB_ALLOWED_IDENTITIES",
    )
    allowed_redirect_uris = [
        uri.strip()
        for uri in _require_env("MCP_ALLOWED_CLIENT_REDIRECT_URIS").split(",")
        if uri.strip()
    ]
    if not allowed_redirect_uris:
        raise RuntimeError("MCP_ALLOWED_CLIENT_REDIRECT_URIS must contain at least one URI pattern")

    github = AllowlistGitHubProvider(
        allowed_identities=allowed_identities,
        allowed_client_redirect_uris=allowed_redirect_uris,
        client_id=client_id,
        client_secret=client_secret,
        base_url=base_url,
    )

    static_tokens = static_token_map()
    if not static_tokens:
        return github

    logger.info("Static MCP_AUTH_TOKEN_* fallback enabled alongside GitHub OAuth")
    # required_scopes=[]: the middleware would otherwise demand the GitHub
    # "user" scope from every token, rejecting static ones (403). Scope-based
    # authz is not used here — the login allowlist is the access control.
    return MultiAuth(
        server=github,
        verifiers=[StaticTokenVerifier(static_tokens)],
        required_scopes=[],
    )
