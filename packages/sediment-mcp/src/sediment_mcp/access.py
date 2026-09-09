"""Immutable request snapshots and explicitly selected ACL sources."""

import os
from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import Protocol

from sediment_mcp.acl import Acl, EMPTY_GRANT, Grant, load_acl


@dataclass(frozen=True)
class AccessSnapshot:
    revision: int
    acl: Acl | None
    active_principals: frozenset[str] | None = None
    github_identities: dict[str, str] | None = None

    def is_active(self, principal: str) -> bool:
        return self.active_principals is None or principal.lower() in self.active_principals

    def resolve(self, principal: str, collection: str) -> Grant | None:
        if not self.is_active(principal):
            return EMPTY_GRANT
        if self.acl is None:
            return None
        return self.acl.resolve(principal, collection)


class AccessSource(Protocol):
    def snapshot(self) -> AccessSnapshot: ...


class FileAccess:
    def __init__(self, acl: Acl | None):
        self._snapshot = AccessSnapshot(0, acl)

    def snapshot(self) -> AccessSnapshot:
        return self._snapshot


def load_access() -> AccessSource:
    mode = os.environ.get("MCP_ACCESS_MODE", "file")
    if os.environ.get("MCP_ACL_DB"):
        raise RuntimeError("MCP_ACL_DB is obsolete: migrate explicitly and configure MCP_ACCESS_MODE=database and MCP_ACCESS_DB")
    if mode == "file":
        if os.environ.get("MCP_ACCESS_DB"):
            raise RuntimeError("MCP_ACCESS_DB requires MCP_ACCESS_MODE=database")
        return FileAccess(load_acl())
    if mode != "database":
        raise RuntimeError(f"Unknown MCP_ACCESS_MODE: {mode!r}")
    if os.environ.get("MCP_ACL_CONFIG") or os.environ.get("MCP_ACL_DISABLE"):
        raise RuntimeError("Database access cannot be combined with MCP_ACL_CONFIG or MCP_ACL_DISABLE")
    if not os.environ.get("MCP_ACCESS_DB"):
        raise RuntimeError("MCP_ACCESS_DB is required in database mode")
    if os.environ.get("MCP_GITHUB_ALLOWED_IDENTITIES"):
        raise RuntimeError("Database mode owns user identities; migrate MCP_GITHUB_ALLOWED_IDENTITIES into the access database")
    matches = list(entry_points(group="sediment_mcp.access_providers", name="database"))
    if len(matches) != 1:
        raise RuntimeError("Database access requires exactly one installed EE database access provider")
    return matches[0].load()()
