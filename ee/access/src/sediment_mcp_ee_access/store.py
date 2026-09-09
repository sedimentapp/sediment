"""Transactional active versions, shared draft and permanent identity bindings."""

from contextlib import contextmanager
from dataclasses import dataclass
import json
import logging
from pathlib import Path
import sqlite3
import time
from typing import Callable

from sediment_mcp.acl import AclConfigError
from sediment_mcp_ee_access.model import empty_document, encode, validate

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE access_meta (schema_version INTEGER NOT NULL CHECK(schema_version=1));
INSERT INTO access_meta VALUES (1);
CREATE TABLE access_versions (
 version INTEGER PRIMARY KEY AUTOINCREMENT, document TEXT NOT NULL,
 author TEXT NOT NULL, created_at INTEGER NOT NULL, note TEXT NOT NULL
);
CREATE TABLE access_draft (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), revision INTEGER NOT NULL,
 base_version INTEGER NOT NULL REFERENCES access_versions(version),
 document TEXT NOT NULL, author TEXT NOT NULL, created_at INTEGER NOT NULL
);
CREATE TABLE access_bindings (principal TEXT PRIMARY KEY, github_id TEXT UNIQUE);
"""


class ConflictError(Exception):
    """The caller edited a stale active version or shared draft."""


@dataclass(frozen=True)
class Version:
    version: int
    document: dict
    author: str
    created_at: int
    note: str


@dataclass(frozen=True)
class Draft:
    revision: int
    base_version: int
    document: dict
    author: str


class AccessStore:
    def __init__(self, path: str, *, create: bool = False):
        self.path = str(Path(path).resolve())
        if create:
            # Exclusive creation distinguishes setup from corrupt/existing stores.
            with open(self.path, "xb"):
                pass
            with self._connection() as conn:
                conn.executescript(_SCHEMA)
                initial = encode(empty_document())
                conn.execute("INSERT INTO access_versions(document,author,created_at,note) VALUES (?,?,?,?)",
                             (initial, "system", int(time.time()), "Initial setup: no data access"))
                conn.execute("INSERT INTO access_draft VALUES (1,0,1,?,?,?)",
                             (initial, "system", int(time.time())))
        with self._connection() as conn:
            if conn.execute("SELECT schema_version FROM access_meta").fetchall() != [(1,)]:
                raise RuntimeError("Unsupported access database schema")
            if conn.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise RuntimeError("Access database integrity check failed")
        self.snapshot()
        self.draft()

    @contextmanager
    def _connection(self):
        try:
            conn = sqlite3.connect(Path(self.path).as_uri() + "?mode=rw", uri=True, timeout=10)
        except sqlite3.Error:
            logger.exception("operation=access_database_open path=%s", self.path)
            raise
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            with conn:
                yield conn
        except sqlite3.Error:
            logger.exception("operation=access_database path=%s", self.path)
            raise
        finally:
            conn.close()

    @staticmethod
    def _latest(conn) -> Version:
        row = conn.execute("SELECT version,document,author,created_at,note FROM access_versions ORDER BY version DESC LIMIT 1").fetchone()
        if row is None:
            raise RuntimeError("Access database has no active version")
        return Version(row[0], json.loads(row[1]), row[2], row[3], row[4])

    def latest(self) -> Version:
        with self._connection() as conn:
            return self._latest(conn)

    def snapshot(self):
        try:
            active = self.latest()
            return validate(active.document, active.version)
        except (json.JSONDecodeError, AclConfigError):
            logger.exception("operation=access_snapshot_validate path=%s", self.path)
            raise

    @staticmethod
    def _draft(conn) -> Draft:
        row = conn.execute("SELECT revision,base_version,document,author FROM access_draft WHERE singleton=1").fetchone()
        if row is None:
            raise RuntimeError("Access database has no draft record")
        return Draft(row[0], row[1], json.loads(row[2]), row[3])

    def draft(self) -> Draft:
        with self._connection() as conn:
            return self._draft(conn)

    @staticmethod
    def _bindings(conn, document: dict) -> None:
        existing = dict(conn.execute("SELECT principal,github_id FROM access_bindings"))
        incoming = {u["principal"]: u["github_id"] for u in document["users"]}
        if existing.keys() - incoming.keys():
            raise AclConfigError("Registered users cannot be removed; disable them instead")
        for name, gid in existing.items():
            if incoming[name] != gid:
                raise AclConfigError(f"Identity binding for {name!r} is immutable")

    def save_draft(self, document: dict, *, author: str, revision: int, base_version: int) -> Draft:
        validate(document)
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = self._draft(conn)
            if (current.revision, current.base_version) != (revision, base_version):
                raise ConflictError("Shared draft changed; reload before saving")
            if self._latest(conn).version != base_version:
                raise ConflictError("Active access version changed; reload before saving")
            self._bindings(conn, document)
            conn.execute("UPDATE access_draft SET revision=revision+1,document=?,author=?,created_at=? WHERE singleton=1",
                         (encode(document), author, int(time.time())))
            result = self._draft(conn)
        logger.info("operation=access_draft_save author=%s revision=%s", author, result.revision)
        return result

    def apply(self, *, author: str, revision: int, base_version: int,
              check_seats: Callable[[frozenset[str]], None], note: str = "Applied draft") -> Version:
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            draft = self._draft(conn)
            active = self._latest(conn)
            if (draft.revision, draft.base_version, active.version) != (revision, base_version, base_version):
                raise ConflictError("Draft or active version changed; preview again before applying")
            snapshot = validate(draft.document)
            assert snapshot.active_principals is not None
            check_seats(snapshot.active_principals)
            self._bindings(conn, draft.document)
            if encode(active.document) == encode(draft.document):
                raise ConflictError("The draft has no changes")
            for user in draft.document["users"]:
                conn.execute("INSERT OR IGNORE INTO access_bindings VALUES (?,?)", (user["principal"], user["github_id"]))
            conn.execute("INSERT INTO access_versions(document,author,created_at,note) VALUES (?,?,?,?)",
                         (encode(draft.document), author, int(time.time()), note))
            applied = self._latest(conn)
            conn.execute("UPDATE access_draft SET revision=revision+1,base_version=?,author=?,created_at=? WHERE singleton=1",
                         (applied.version, author, int(time.time())))
        logger.info("operation=access_apply author=%s version=%s", author, applied.version)
        return applied

    def history(self) -> list[Version]:
        with self._connection() as conn:
            return [Version(r[0], json.loads(r[1]), r[2], r[3], r[4]) for r in conn.execute(
                "SELECT version,document,author,created_at,note FROM access_versions ORDER BY version DESC")]

    def get(self, version: int) -> Version:
        with self._connection() as conn:
            row = conn.execute("SELECT version,document,author,created_at,note FROM access_versions WHERE version=?", (version,)).fetchone()
            if row is None:
                raise KeyError(version)
            return Version(row[0], json.loads(row[1]), row[2], row[3], row[4])
