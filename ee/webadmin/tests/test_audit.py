"""Audit store + middleware: recording, filtering, retention, failures."""

import asyncio
import sqlite3
import time

import pytest
from fastmcp import Client, FastMCP
from fastmcp.server.auth import AccessToken

import sediment_mcp_ee_webadmin.audit as audit_mod
from sediment_mcp_ee_webadmin.audit import (
    RETENTION_DAYS,
    AuditLog,
    AuditMiddleware,
    request_principal,
    summarize_args,
)


@pytest.fixture
def log(tmp_path):
    return AuditLog(str(tmp_path / "audit.db"))


def test_record_and_query(log):
    log.record("alice", "search", "acme", {"query": "x"}, True, None, 12)
    log.record("carol", "add_knowledge", "acme", {"file": "n", "text_len": 5}, True, None, 30)
    log.record("alice", "search", "globex", {"query": "y"}, False, "Boom: nope", 7)

    events = log.query()
    assert [e.principal for e in events] == ["alice", "carol", "alice"]  # newest first
    assert events[0].error == "Boom: nope"
    assert events[0].ok is False
    assert events[2].detail == {"query": "x"}

    assert [e.collection for e in log.query(principal="carol")] == ["acme"]
    assert len(log.query(tool="search")) == 2
    assert len(log.query(principal="alice", tool="search")) == 2
    assert log.query(principal="nobody") == []


def test_stats_last_days(log):
    for _ in range(3):
        log.record("alice", "search", "acme", {}, True, None, 1)
    log.record("carol", "search", "acme", {}, True, None, 1)
    assert log.stats()[0] == ("alice", "search", 3)


def test_prune_drops_old_events(tmp_path):
    path = str(tmp_path / "audit.db")
    log = AuditLog(path)
    log.record("alice", "search", "acme", {}, True, None, 1)
    old_ts = int(time.time()) - (RETENTION_DAYS + 1) * 86400
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO events (ts, principal, tool, detail, ok, duration_ms) "
            "VALUES (?, 'old', 'search', '{}', 1, 1)",
            (old_ts,),
        )
    assert len(AuditLog(path).query()) == 1  # init prunes; only the fresh one left


def test_retention_is_configurable(tmp_path):
    path = str(tmp_path / "audit.db")
    log = AuditLog(path)
    log.record("alice", "search", "acme", {}, True, None, 1)
    week_old = int(time.time()) - 8 * 86400
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO events (ts, principal, tool, detail, ok, duration_ms) "
            "VALUES (?, 'old', 'search', '{}', 1, 1)",
            (week_old,),
        )
    # default 90d keeps the 8-day-old event, retention_days=7 drops it
    assert len(AuditLog(path).query()) == 2
    assert len(AuditLog(path, retention_days=7).query()) == 1
    with pytest.raises(ValueError):
        AuditLog(path, retention_days=0)


def test_query_limit_and_unlimited(log):
    for i in range(5):
        log.record("alice", "search", "acme", {"i": i}, True, None, 1)
    assert len(log.query(limit=2)) == 2
    assert len(log.query(limit=None)) == 5


def test_summarize_args_hides_text():
    detail = summarize_args({"collection": "acme", "text": "secret content", "title": "", "file": "f"})
    assert detail == {"collection": "acme", "file": "f", "text_len": 14}


def _mcp_with_audit(log):
    mcp = FastMCP("audited")

    @mcp.tool()
    def echo(collection: str, query: str = "") -> str:
        if query == "boom":
            raise ValueError("kaboom")
        return f"{collection}:{query}"

    mcp.add_middleware(AuditMiddleware(log))
    return mcp


def test_middleware_records_tool_calls(log):
    mcp = _mcp_with_audit(log)

    async def run():
        async with Client(mcp) as client:
            await client.call_tool("echo", {"collection": "acme", "query": "hi"})

    asyncio.run(run())
    events = log.query()
    assert len(events) == 1
    event = events[0]
    assert event.tool == "echo"
    assert event.collection == "acme"
    assert event.detail == {"collection": "acme", "query": "hi"}
    assert event.ok is True
    assert event.principal == "-"  # in-process client carries no identity


def test_middleware_records_failures_and_propagates(log):
    mcp = _mcp_with_audit(log)

    async def run():
        async with Client(mcp) as client:
            await client.call_tool("echo", {"collection": "acme", "query": "boom"})

    with pytest.raises(Exception):
        asyncio.run(run())
    events = log.query()
    assert len(events) == 1
    assert events[0].ok is False


def _set_access(monkeypatch, access):
    monkeypatch.setattr(audit_mod, "get_access_token", lambda: access)


def test_request_principal_prefers_stable_principal_claim(monkeypatch):
    # OAuth tokens carry the allowlist principal; after a GitHub rename the
    # audit identity must stay the ACL identity, not the mutable login.
    _set_access(monkeypatch, AccessToken(
        token="t", client_id="10001", scopes=[],
        claims={"principal": "Alice", "login": "renamed-user", "sub": "10001"},
    ))
    assert request_principal() == "alice"


def test_request_principal_falls_back_to_login_then_client_id(monkeypatch):
    _set_access(monkeypatch, AccessToken(
        token="t", client_id="oauth-client-uuid", scopes=[], claims={"login": "Alice"}))
    assert request_principal() == "alice"
    _set_access(monkeypatch, AccessToken(token="t", client_id="BOB", scopes=[]))
    assert request_principal() == "bob"


def test_request_principal_without_auth_context(monkeypatch):
    _set_access(monkeypatch, None)
    assert request_principal() == "-"
