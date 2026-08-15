"""Browser auth for the admin pages.

Custom Starlette routes are outside fastmcp's bearer-auth middleware (same
as /health), so the extension enforces its own browser auth.

MCP_ADMIN_AUTH selects the mode (required, no default):

    dev     every request acts as MCP_ADMIN_DEV_PRINCIPAL — no login, no
            cookie. Local development only; never deploy with this.

    github  cookie session over the same GitHub OAuth App the `github`
            auth provider (ee/auth) uses. GitHub OAuth Apps accept a
            redirect_uri in a subdirectory of the registered callback, so
            the browser flow uses <MCP_BASE_URL>/auth/callback/web next to
            fastmcp's own /auth/callback. Only ids from MCP_ADMIN_IDENTITIES
            get a session.

Environment (github mode):
    MCP_BASE_URL                  public HTTPS origin of this server
    GITHUB_OAUTH_CLIENT_ID        GitHub OAuth App client id
    GITHUB_OAUTH_CLIENT_SECRET    GitHub OAuth App client secret
    MCP_ADMIN_IDENTITIES          comma-separated principal:GitHub-id entries
    MCP_ADMIN_SESSION_SECRET      HMAC key for session cookies / OAuth state
"""

import base64
import hashlib
import hmac
import os
import secrets
import time
from urllib.parse import urlencode

import httpx
from fastmcp.utilities.logging import get_logger
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response

from sediment_mcp.auth import parse_github_identities

logger = get_logger(__name__)

# __Host- prefix: browser enforces Secure + Path=/ + no Domain (blocks subdomain overwrite)
SESSION_COOKIE = "__Host-sediment_mcp_admin_session"
OAUTH_STATE_COOKIE = "__Host-sediment_mcp_admin_oauth_state"
SESSION_TTL = 7 * 24 * 3600
STATE_TTL = 10 * 60
CSRF_TTL = 4 * 3600

LOGIN_PATH = "/admin/login"
LOGOUT_PATH = "/admin/logout"
WEB_CALLBACK_PATH = "/auth/callback/web"

GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_USER_URL = "https://api.github.com/user"


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is required for the webadmin extension")
    return value


class Signer:
    """HMAC-signed strings with an expiry (session cookie, OAuth state)."""

    def __init__(self, secret: str) -> None:
        self._key = secret.encode()

    def _mac(self, payload: str, expires: str) -> str:
        return hmac.new(self._key, f"{payload}.{expires}".encode(), hashlib.sha256).hexdigest()

    def sign(self, value: str, ttl: int) -> str:
        expires = str(int(time.time()) + ttl)
        payload = base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")
        return f"{payload}.{expires}.{self._mac(payload, expires)}"

    def verify(self, token: str) -> str | None:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        payload, expires, mac = parts
        if not hmac.compare_digest(mac, self._mac(payload, expires)):
            return None
        if not expires.isdigit() or int(expires) < time.time():
            return None
        return base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode()


class CsrfMixin:
    """Signed anti-CSRF tokens for the mutating admin forms.

    The session cookie is SameSite=Lax, but dev mode has no cookie at all —
    the token is what stops a drive-by cross-site POST in both modes.
    """

    _signer: Signer

    def csrf_token(self, principal: str) -> str:
        return self._signer.sign(f"csrf:{principal}", CSRF_TTL)

    def csrf_verify(self, principal: str, token: str) -> bool:
        return self._signer.verify(token) == f"csrf:{principal}"


class DevAdminAuth(CsrfMixin):
    """Fixed principal, no login flow. Local development only."""

    login_routes = False

    def __init__(self, principal: str) -> None:
        self._principal = principal
        # ephemeral per-process key: dev sessions don't survive restarts anyway
        self._signer = Signer(secrets.token_hex(32))

    def principal(self, request: Request) -> str | None:
        return self._principal


class GitHubAdminAuth(CsrfMixin):
    """Signed-cookie session obtained through the GitHub OAuth App."""

    login_routes = True

    def __init__(
        self,
        *,
        base_url: str,
        client_id: str,
        client_secret: str,
        allowed_identities: dict[str, str],
        signer: Signer,
    ) -> None:
        self._base_url = base_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._allowed_by_id = allowed_identities
        self._principals = frozenset(allowed_identities.values())
        self._signer = signer

    def principal(self, request: Request) -> str | None:
        token = request.cookies.get(SESSION_COOKIE)
        if not token:
            return None
        principal = self._signer.verify(token)
        if principal is None or principal.lower() not in self._principals:
            # Allowlist is re-checked on every request: removing an identity
            # invalidates its session at the next restart.
            return None
        return principal.lower()

    def login_redirect(self, request: Request) -> Response:
        nonce = secrets.token_urlsafe(32)
        params = urlencode({
            "client_id": self._client_id,
            "redirect_uri": f"{self._base_url}{WEB_CALLBACK_PATH}",
            "state": self._signer.sign(f"login:{nonce}", STATE_TTL),
            "allow_signup": "false",
        })
        response = RedirectResponse(f"{GITHUB_AUTHORIZE_URL}?{params}", status_code=302)
        response.set_cookie(
            OAUTH_STATE_COOKIE,
            self._signer.sign(nonce, STATE_TTL),
            max_age=STATE_TTL,
            httponly=True,
            secure=True,
            samesite="lax",
            path="/",
        )
        return response

    def valid_oauth_state(self, request: Request) -> bool:
        state = self._signer.verify(request.query_params.get("state", ""))
        cookie_nonce = self._signer.verify(request.cookies.get(OAUTH_STATE_COOKIE, ""))
        if state is None or cookie_nonce is None or not state.startswith("login:"):
            return False
        return hmac.compare_digest(state.removeprefix("login:"), cookie_nonce)

    @staticmethod
    def _consume_oauth_state(response: Response) -> Response:
        response.delete_cookie(OAUTH_STATE_COOKIE, path="/")
        return response

    async def handle_callback(self, request: Request) -> Response:
        if not self.valid_oauth_state(request):
            return self._consume_oauth_state(
                PlainTextResponse("Invalid or expired OAuth state", status_code=400)
            )
        code = request.query_params.get("code")
        if not code:
            return self._consume_oauth_state(PlainTextResponse("Missing ?code", status_code=400))

        async with httpx.AsyncClient(timeout=15) as http:
            token_resp = await http.post(
                GITHUB_TOKEN_URL,
                data={
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "code": code,
                    "redirect_uri": f"{self._base_url}{WEB_CALLBACK_PATH}",
                },
                headers={"Accept": "application/json"},
            )
            token_resp.raise_for_status()
            token_json = token_resp.json()
            access_token = token_json.get("access_token")
            if not access_token:
                logger.error("GitHub token exchange failed: %s", token_json)
                return self._consume_oauth_state(
                    PlainTextResponse("GitHub token exchange failed", status_code=502)
                )
            user_resp = await http.get(
                GITHUB_USER_URL,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/vnd.github+json",
                },
            )
            user_resp.raise_for_status()
            user_data = user_resp.json()
            login = user_data.get("login")
            user_id = user_data.get("id")

        if not isinstance(login, str) or not login or not isinstance(user_id, int):
            return self._consume_oauth_state(
                PlainTextResponse("GitHub /user returned invalid identity", status_code=502)
            )
        principal = self._allowed_by_id.get(str(user_id))
        if principal is None:
            logger.warning(
                "Rejected admin GitHub id %r (login=%r): not in MCP_ADMIN_IDENTITIES",
                user_id,
                login,
            )
            return self._consume_oauth_state(
                PlainTextResponse("GitHub user is not allowed", status_code=403)
            )

        response = RedirectResponse("/admin", status_code=302)
        response.set_cookie(
            SESSION_COOKIE,
            self._signer.sign(principal, SESSION_TTL),
            max_age=SESSION_TTL,
            httponly=True,
            secure=True,
            samesite="lax",
            path="/",
        )
        return self._consume_oauth_state(response)

    def logout(self) -> Response:
        response = RedirectResponse("/admin", status_code=302)
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response


AdminAuth = DevAdminAuth | GitHubAdminAuth


def build_admin_auth() -> AdminAuth:
    mode = os.environ.get("MCP_ADMIN_AUTH")
    if mode == "dev":
        principal = _require_env("MCP_ADMIN_DEV_PRINCIPAL")
        logger.warning(
            "webadmin: DEV auth — every request acts as %r without login; "
            "local use only, never deploy with MCP_ADMIN_AUTH=dev",
            principal,
        )
        return DevAdminAuth(principal.lower())
    if mode == "github":
        allowed_identities = parse_github_identities(
            _require_env("MCP_ADMIN_IDENTITIES"), "MCP_ADMIN_IDENTITIES"
        )
        return GitHubAdminAuth(
            base_url=_require_env("MCP_BASE_URL").rstrip("/"),
            client_id=_require_env("GITHUB_OAUTH_CLIENT_ID"),
            client_secret=_require_env("GITHUB_OAUTH_CLIENT_SECRET"),
            allowed_identities=allowed_identities,
            signer=Signer(_require_env("MCP_ADMIN_SESSION_SECRET")),
        )
    raise RuntimeError(
        'MCP_ADMIN_AUTH must be "dev" or "github" when the webadmin extension is enabled'
    )
