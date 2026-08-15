"""Usage audit (ee-only): who called which MCP tool, with what, and how it went.

Core stays untouched: the webadmin extension attaches AuditMiddleware to the
FastMCP app when MCP_AUDIT_DB points at a SQLite file (on the PVC in prod).
Admin actions of the web UI itself (e.g. deleting a manual entry) are recorded
into the same log with an "admin:" tool prefix.

The `text` argument of add_knowledge is never stored — only its length; the
content itself lives in Qdrant and is reachable from the manual-entries page.
"""

import json
import sqlite3
import time
from dataclasses import dataclass

import anyio.to_thread
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.utilities.logging import get_logger

logger = get_logger(__name__)

RETENTION_DAYS = 90
PRUNE_INTERVAL = 24 * 3600
DEFAULT_QUERY_LIMIT = 200

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    principal   TEXT NOT NULL,
    tool        TEXT NOT NULL,
    collection  TEXT,
    detail      TEXT NOT NULL,
    ok          INTEGER NOT NULL,
    error       TEXT,
    duration_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS events_ts ON events (ts);
CREATE INDEX IF NOT EXISTS events_principal ON events (principal);
"""


@dataclass(frozen=True)
class AuditEvent:
    id: int
    ts: int
    principal: str
    tool: str
    collection: str | None
    detail: dict
    ok: bool
    error: str | None
    duration_ms: int


def _row_to_event(row) -> AuditEvent:
    return AuditEvent(
        id=row[0], ts=row[1], principal=row[2], tool=row[3], collection=row[4],
        detail=json.loads(row[5]), ok=bool(row[6]), error=row[7], duration_ms=row[8],
    )


class AuditLog:
    def __init__(self, path: str, retention_days: int = RETENTION_DAYS) -> None:
        if retention_days < 1:
            raise ValueError(f"retention_days must be >= 1, got {retention_days}")
        self._path = path
        self._retention_days = retention_days
        self._last_prune = 0.0
        conn = self._connect()
        try:
            with conn:
                conn.executescript(_SCHEMA)
        finally:
            conn.close()
        self._prune()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path, timeout=10)

    def record(
        self,
        principal: str,
        tool: str,
        collection: str | None,
        detail: dict,
        ok: bool,
        error: str | None,
        duration_ms: int,
    ) -> None:
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "INSERT INTO events (ts, principal, tool, collection, detail, ok, error, duration_ms) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (int(time.time()), principal, tool, collection,
                     json.dumps(detail, ensure_ascii=False), int(ok), error, duration_ms),
                )
        finally:
            conn.close()
        if time.monotonic() - self._last_prune > PRUNE_INTERVAL:
            self._prune()

    def _prune(self) -> None:
        cutoff = int(time.time()) - self._retention_days * 86400
        conn = self._connect()
        try:
            with conn:
                dropped = conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,)).rowcount
        finally:
            conn.close()
        self._last_prune = time.monotonic()
        if dropped:
            logger.info(
                "audit: pruned %d events older than %d days", dropped, self._retention_days
            )

    def query(
        self,
        principal: str | None = None,
        tool: str | None = None,
        limit: int | None = DEFAULT_QUERY_LIMIT,
    ) -> list[AuditEvent]:
        """Newest first; limit=None returns everything matching (CSV export)."""
        conditions, params = [], []
        if principal:
            conditions.append("principal = ?")
            params.append(principal)
        if tool:
            conditions.append("tool = ?")
            params.append(tool)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        tail = "LIMIT ?" if limit is not None else ""
        if limit is not None:
            params.append(limit)
        conn = self._connect()
        try:
            rows = conn.execute(
                f"SELECT id, ts, principal, tool, collection, detail, ok, error, duration_ms "
                f"FROM events {where} ORDER BY id DESC {tail}",
                params,
            ).fetchall()
        finally:
            conn.close()
        return [_row_to_event(r) for r in rows]

    def stats(self, days: int = 7) -> list[tuple[str, str, int]]:
        """(principal, tool, calls) over the last `days`, busiest first."""
        cutoff = int(time.time()) - days * 86400
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT principal, tool, COUNT(*) FROM events WHERE ts >= ? "
                "GROUP BY principal, tool ORDER BY COUNT(*) DESC",
                (cutoff,),
            ).fetchall()
        finally:
            conn.close()
        return [(r[0], r[1], r[2]) for r in rows]


def request_principal() -> str:
    """Principal of the current MCP request; "-" when the transport carries
    no identity (e.g. in-process test client). Mirrors sediment_mcp.auth logic
    without raising — the audit path must never fail a tool call."""
    access = get_access_token()
    if access is None:
        return "-"
    claims = getattr(access, "claims", None) or {}
    principal = claims.get("principal")
    if isinstance(principal, str) and principal:
        return principal.lower()
    login = claims.get("login")
    if isinstance(login, str) and login:
        return login.lower()
    return access.client_id.lower()


def summarize_args(arguments: dict) -> dict:
    """Audit-safe view of tool arguments: `text` content is replaced by its length."""
    detail = {k: v for k, v in arguments.items() if k != "text" and v not in (None, "", [])}
    if isinstance(arguments.get("text"), str):
        detail["text_len"] = len(arguments["text"])
    return detail


class AuditMiddleware(Middleware):
    def __init__(self, log: AuditLog) -> None:
        self._log = log

    async def on_call_tool(self, context: MiddlewareContext, call_next: CallNext):
        message = context.message
        arguments = message.arguments or {}
        started = time.monotonic()

        def finish(ok: bool, error: str | None) -> tuple:
            collection = arguments.get("collection")
            return (
                request_principal(), message.name,
                collection if isinstance(collection, str) else None,
                summarize_args(arguments), ok, error,
                int((time.monotonic() - started) * 1000),
            )

        try:
            result = await call_next(context)
        except Exception as e:
            # audit trail for failures, then propagate unchanged
            await anyio.to_thread.run_sync(self._log.record, *finish(False, f"{type(e).__name__}: {e}"))
            raise
        await anyio.to_thread.run_sync(self._log.record, *finish(True, None))
        return result
