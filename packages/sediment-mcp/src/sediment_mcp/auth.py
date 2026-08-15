"""Auth provider selection and request identity.

Core ships only the static bearer-token provider. Extension packages
(ee/auth) register additional providers via the "sediment_mcp.auth_providers"
entry-point group; each entry point is a zero-argument factory returning a
fastmcp AuthProvider and reading its own configuration from the environment.
MCP_AUTH_PROVIDER selects the provider by name (default: "static").

Static tokens are named: MCP_AUTH_TOKEN_<NAME> maps the token to principal
"<name>" (lowercased). The bare MCP_AUTH_TOKEN form is rejected — every token
must carry a principal so ACL decisions and add_knowledge attribution never
fall back to an anonymous identity. A static token name that equals a GitHub
login (ee/auth github provider) is intentionally the same principal: same
human, same grants.
"""

import hmac
import os
import re
from importlib.metadata import entry_points

from fastmcp.server.auth import AccessToken, AuthProvider, TokenVerifier
from fastmcp.server.dependencies import get_access_token
from fastmcp.utilities.logging import get_logger

ENTRY_POINT_GROUP = "sediment_mcp.auth_providers"
STATIC_TOKEN_PREFIX = "MCP_AUTH_TOKEN_"

logger = get_logger(__name__)
_GITHUB_LOGIN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$")


def parse_github_identities(raw: str, env_name: str) -> dict[str, str]:
    """Parse `principal:numeric_id` entries into GitHub id -> stable principal."""
    identities: dict[str, str] = {}
    ids_by_principal: dict[str, str] = {}
    for entry in (part.strip() for part in raw.split(",")):
        if not entry:
            continue
        principal, sep, user_id = entry.partition(":")
        principal = principal.strip().lower()
        user_id = user_id.strip()
        if not sep or not _GITHUB_LOGIN_RE.fullmatch(principal) or not user_id.isdigit():
            raise RuntimeError(
                f"{env_name}: expected comma-separated principal:numeric_github_id entries, "
                f"got {entry!r}"
            )
        previous_principal = identities.get(user_id)
        if previous_principal is not None and previous_principal != principal:
            raise RuntimeError(
                f"{env_name}: GitHub id {user_id} is assigned to both "
                f"{previous_principal!r} and {principal!r}"
            )
        previous_id = ids_by_principal.get(principal)
        if previous_id is not None and previous_id != user_id:
            raise RuntimeError(
                f"{env_name}: principal {principal!r} is assigned to both "
                f"GitHub ids {previous_id} and {user_id}"
            )
        identities[user_id] = principal
        ids_by_principal[principal] = user_id
    if not identities:
        raise RuntimeError(f"{env_name} must contain at least one principal:numeric_github_id entry")
    return identities


def static_token_map() -> dict[str, str]:
    """token -> principal from MCP_AUTH_TOKEN_<NAME> env vars."""
    if os.environ.get("MCP_AUTH_TOKEN"):
        raise RuntimeError(
            "Bare MCP_AUTH_TOKEN is no longer supported; "
            "rename it to MCP_AUTH_TOKEN_<PRINCIPAL> (e.g. MCP_AUTH_TOKEN_ALICE)"
        )
    tokens: dict[str, str] = {}
    for key, val in sorted(os.environ.items()):
        if not key.startswith(STATIC_TOKEN_PREFIX) or not val:
            continue
        principal = key.removeprefix(STATIC_TOKEN_PREFIX).lower()
        if not principal:
            raise RuntimeError(f"{key}: empty principal suffix")
        if val in tokens and tokens[val] != principal:
            raise RuntimeError(
                f"Same token value set for principals {tokens[val]!r} and {principal!r} — "
                "ambiguous identity"
            )
        tokens[val] = principal
    return tokens


class StaticTokenVerifier(TokenVerifier):
    """Verifies bearer tokens against named static tokens (token -> principal)."""

    def __init__(self, tokens: dict[str, str]) -> None:
        super().__init__()
        self._tokens = tokens

    async def verify_token(self, token: str) -> AccessToken | None:
        for known, principal in self._tokens.items():
            if hmac.compare_digest(token, known):
                return AccessToken(token=token, client_id=principal, scopes=[])
        return None


def current_principal() -> str:
    """Identity of the authenticated request, for ACL and attribution.

    GitHub OAuth tokens carry a stable allowlist principal in claims; static
    tokens carry the principal as client_id. Never a parameter of any tool —
    the client must not be able to impersonate.
    """
    access = get_access_token()
    if access is None:
        raise RuntimeError("No authenticated request context — cannot resolve principal")
    claims = getattr(access, "claims", None) or {}
    principal = claims.get("principal")
    if isinstance(principal, str) and principal:
        return principal.lower()
    login = claims.get("login")
    if isinstance(login, str) and login:
        return login.lower()
    return access.client_id.lower()


def _static_provider() -> AuthProvider:
    tokens = static_token_map()
    if not tokens:
        raise RuntimeError("No MCP_AUTH_TOKEN_<PRINCIPAL> environment variables set")
    return StaticTokenVerifier(tokens)


BUILTIN_PROVIDERS = {"static": _static_provider}


def build_auth_provider() -> AuthProvider:
    name = os.environ.get("MCP_AUTH_PROVIDER", "static")

    factory = BUILTIN_PROVIDERS.get(name)
    if factory is None:
        matches = entry_points(group=ENTRY_POINT_GROUP, name=name)
        if not matches:
            available = sorted(
                {*BUILTIN_PROVIDERS, *(ep.name for ep in entry_points(group=ENTRY_POINT_GROUP))}
            )
            raise RuntimeError(
                f"Unknown auth provider {name!r} (MCP_AUTH_PROVIDER). "
                f"Available: {', '.join(available)}"
            )
        entry_point = next(iter(matches))
        factory = entry_point.load()
        logger.info("Loaded auth provider %r from %s", name, entry_point.value)

    provider = factory()
    if not isinstance(provider, AuthProvider):
        raise TypeError(
            f"Auth provider {name!r} returned {type(provider).__name__}, "
            "expected a fastmcp AuthProvider"
        )
    logger.info("Using auth provider: %s", name)
    return provider
