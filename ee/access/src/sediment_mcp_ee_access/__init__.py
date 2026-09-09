"""EE database access provider, independent of webadmin registration."""

import os
from pathlib import Path

from sediment_mcp.auth import parse_github_identities
from sediment_mcp_ee_license import require_ee, verify_license, _load_token
from sediment_mcp_ee_access.store import AccessStore


def check_seats(principals: frozenset[str]) -> None:
    claims = verify_license(_load_token())
    admins = os.environ.get("MCP_ADMIN_IDENTITIES")
    combined = set(principals)
    if admins:
        combined.update(parse_github_identities(admins, "MCP_ADMIN_IDENTITIES").values())
    dev = os.environ.get("MCP_ADMIN_DEV_PRINCIPAL")
    if dev:
        combined.add(dev.lower())
    if len(combined) > claims["seats"]:
        raise ValueError(f"License allows {claims['seats']} seats; proposed active users and administrators require {len(combined)}")


def provider() -> AccessStore:
    require_ee("managed-access")
    path = Path(os.environ["MCP_ACCESS_DB"])
    initialized = os.environ.get("MCP_ACCESS_REQUIRE_INITIALIZED", "0")
    if initialized not in {"0", "1"}:
        raise RuntimeError("MCP_ACCESS_REQUIRE_INITIALIZED must be 0 or 1")
    if initialized == "1" and not path.is_file():
        raise RuntimeError("Access database must be explicitly migrated or restored before startup")
    store = AccessStore(str(path), create=not path.exists())
    snapshot = store.snapshot()
    assert snapshot.active_principals is not None
    check_seats(snapshot.active_principals)
    return store
