"""Versioned ACL storage: the whole config as one YAML document in SQLite (ee-only).

Core keeps its file-based mechanism (MCP_ACL_CONFIG + load_acl()) untouched;
this store exists only inside the webadmin extension. When MCP_ACL_DB is set,
register() loads the latest version from here over the file-based ACL; when
the DB is empty, the MCP_ACL_CONFIG file seeds version 1, so enabling the DB
never changes effective grants by itself.

Not a relational schema by design: every save persists the full YAML text as
a new version, validated by the Acl() constructor *before* the insert —
an invalid config can never become a version. History doubles as the audit
log and the rollback source.
"""

import sqlite3
import time
from dataclasses import dataclass

import yaml

from sediment_mcp.acl import Acl

_SCHEMA = """
CREATE TABLE IF NOT EXISTS acl_versions (
    version    INTEGER PRIMARY KEY AUTOINCREMENT,
    yaml_text  TEXT NOT NULL,
    author     TEXT NOT NULL,
    created_at INTEGER NOT NULL
)
"""


@dataclass(frozen=True)
class AclVersion:
    version: int
    yaml_text: str
    author: str
    created_at: int  # epoch seconds


def parse_and_validate(yaml_text: str) -> Acl:
    """YAML text -> Acl; raises yaml.YAMLError / AclConfigError on any problem."""
    return Acl(yaml.safe_load(yaml_text))


def _row_to_version(row) -> AclVersion:
    return AclVersion(version=row[0], yaml_text=row[1], author=row[2], created_at=row[3])


class AclStore:
    def __init__(self, path: str) -> None:
        self._path = path
        conn = self._connect()
        try:
            with conn:
                conn.execute(_SCHEMA)
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path, timeout=10)

    def _fetch_one(self, sql: str, params: tuple = ()) -> AclVersion | None:
        conn = self._connect()
        try:
            row = conn.execute(sql, params).fetchone()
        finally:
            conn.close()
        return _row_to_version(row) if row else None

    def latest(self) -> AclVersion | None:
        return self._fetch_one(
            "SELECT version, yaml_text, author, created_at FROM acl_versions "
            "ORDER BY version DESC LIMIT 1"
        )

    def get(self, version: int) -> AclVersion | None:
        return self._fetch_one(
            "SELECT version, yaml_text, author, created_at FROM acl_versions "
            "WHERE version = ?",
            (version,),
        )

    def history(self) -> list[AclVersion]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT version, yaml_text, author, created_at FROM acl_versions "
                "ORDER BY version DESC"
            ).fetchall()
        finally:
            conn.close()
        return [_row_to_version(r) for r in rows]

    def save(self, yaml_text: str, author: str) -> tuple[int, Acl]:
        """Validate, then persist as a new version. Returns (version, acl)."""
        acl = parse_and_validate(yaml_text)
        conn = self._connect()
        try:
            with conn:
                cursor = conn.execute(
                    "INSERT INTO acl_versions (yaml_text, author, created_at) "
                    "VALUES (?, ?, ?)",
                    (yaml_text, author, int(time.time())),
                )
                version = cursor.lastrowid
        finally:
            conn.close()
        assert version is not None
        return version, acl
