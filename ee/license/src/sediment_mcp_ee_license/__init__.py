"""ee license verification (flat).

A license is a JWT signed offline with an Ed25519 key (alg EdDSA); the
public keys are pinned in PUBLIC_KEY_HEX. The license is flat: any valid,
unexpired license enables every ee feature — there is no per-feature
gating. Required claims: sub (licensee), iat, exp, seats.

Seats: the license caps the number of distinct principals that can
authenticate — the union of static token names (MCP_AUTH_TOKEN_*), principals
from MCP_GITHUB_ALLOWED_IDENTITIES/MCP_ADMIN_IDENTITIES, and the explicit
webadmin dev principal, lowercased. The same name arriving via several paths
is the same human and occupies one seat (the same convention
sediment_mcp.auth uses for identity). More principals configured than the
license allows is a fatal startup error.

Delivery: MCP_LICENSE (the token itself) or MCP_LICENSE_FILE (path to a
file with the token); setting both is an error. Each ee entry point calls
require_ee(<feature>) first thing, so enabling an ee feature without a
valid license is a fatal startup error. When no ee feature is enabled the
license is never read — CE mode needs nothing.

Licenses are issued with tools/license_issue.py; the private key stays
offline and is never part of the image. There is deliberately no bypass
env var — for dev/tests sign a license with your own key (tests pin a
throwaway keypair via monkeypatch).
"""

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastmcp.utilities.logging import get_logger

from sediment_mcp.auth import parse_github_identities, static_token_map

# Raw 32-byte Ed25519 public keys, hex. A list to allow key rotation:
# a license is valid if any pinned key verifies it.
PUBLIC_KEY_HEX = [
    "2d58dc574796b47f0ee5c28073a8c5b3bcbfd7e2fac64aeb8be654dfc3012a07",  # signing key 2026-07
]

_ENV_TOKEN = "MCP_LICENSE"
_ENV_FILE = "MCP_LICENSE_FILE"
_IDENTITY_ENVS = ("MCP_GITHUB_ALLOWED_IDENTITIES", "MCP_ADMIN_IDENTITIES")
_EXPIRY_WARN_DAYS = 14

logger = get_logger(__name__)

_cached_claims: dict[str, Any] | None = None


def _load_token() -> str:
    token = os.environ.get(_ENV_TOKEN)
    path = os.environ.get(_ENV_FILE)
    if token and path:
        raise RuntimeError(
            f"Both {_ENV_TOKEN} and {_ENV_FILE} are set — remove one, "
            "ambiguous license source"
        )
    if path:
        license_file = Path(path)
        if not license_file.is_file():
            raise RuntimeError(f"{_ENV_FILE}={path}: file not found")
        return license_file.read_text().strip()
    if token:
        return token.strip()
    raise RuntimeError(f"no license configured: set {_ENV_TOKEN} or {_ENV_FILE}")


def verify_license(token: str) -> dict[str, Any]:
    """Verify signature and expiry against the pinned keys; return claims."""
    for key_hex in PUBLIC_KEY_HEX:
        key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(key_hex))
        try:
            claims = jwt.decode(
                token,
                key=key,
                algorithms=["EdDSA"],
                options={"require": ["sub", "iat", "exp", "seats"]},
            )
        except jwt.InvalidSignatureError:
            continue  # try the next pinned key
        except jwt.ExpiredSignatureError as exc:
            raise RuntimeError(f"license expired: {exc}") from exc
        except jwt.InvalidTokenError as exc:  # malformed token, missing claims
            raise RuntimeError(f"invalid license: {exc}") from exc
        seats = claims["seats"]
        if not isinstance(seats, int) or isinstance(seats, bool) or seats < 1:
            raise RuntimeError(
                f"invalid license: seats must be a positive integer, got {seats!r}"
            )
        return claims
    raise RuntimeError("license signature does not match any known signing key")


def configured_principals() -> set[str]:
    """Distinct principals that can authenticate, for seat accounting."""
    principals = set(static_token_map().values())
    for env in _IDENTITY_ENVS:
        raw = os.environ.get(env, "")
        if raw:
            principals.update(parse_github_identities(raw, env).values())
    dev_principal = os.environ.get("MCP_ADMIN_DEV_PRINCIPAL", "").strip().lower()
    if dev_principal:
        principals.add(dev_principal)
    return principals


def require_ee(feature: str) -> dict[str, Any]:
    """Fail fast unless a valid ee license is configured; return its claims.

    Flat model: the feature name is used only in error messages and logs —
    any valid license enables all ee features.
    """
    global _cached_claims
    if _cached_claims is None:
        try:
            claims = verify_license(_load_token())
        except RuntimeError as exc:
            raise RuntimeError(
                f"ee feature {feature!r} is enabled but the license check failed: {exc}"
            ) from exc
        seats: int = claims["seats"]
        used = sorted(configured_principals())
        if len(used) > seats:
            raise RuntimeError(
                f"ee license allows {seats} seat(s) but {len(used)} principals "
                f"are configured: {', '.join(used)} — remove principals or get "
                "a license with more seats"
            )
        expires = datetime.fromtimestamp(claims["exp"], tz=UTC)
        days_left = (expires - datetime.now(tz=UTC)).days
        logger.info(
            "ee license OK: licensee=%r, seats %d/%d, expires %s",
            claims["sub"],
            len(used),
            seats,
            expires.date(),
        )
        if days_left < _EXPIRY_WARN_DAYS:
            logger.warning(
                "ee license expires in %d day(s) (%s) — issue a new one "
                "(tools/license_issue.py); the next restart after expiry will fail",
                days_left,
                expires.date(),
            )
        _cached_claims = claims
    return _cached_claims
