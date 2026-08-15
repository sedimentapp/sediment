import anyio
import pytest
from starlette.requests import Request

from sediment_mcp import server
from sediment_mcp.acl import Acl
from sediment_mcp.limits import SlidingWindowRateLimiter, rate_limit_per_minute


def _probe_request(headers: dict[str, str]) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    return Request({"type": "http", "method": "GET", "path": "/health", "headers": raw})


def test_probe_endpoints_only_answer_in_cluster(monkeypatch):
    external = _probe_request({"x-forwarded-for": "203.0.113.7"})
    internal = _probe_request({})

    assert server._probe_via_gateway(external) is True
    assert server._probe_via_gateway(internal) is False

    # gateway-forwarded probes get a 404 (and /ready 404s before any embed call)
    assert anyio.run(server.health, external).status_code == 404
    assert anyio.run(server.ready, external).status_code == 404

    # in-cluster kubelet (no forwarding header) gets a real answer
    assert anyio.run(server.health, internal).status_code == 200
    monkeypatch.setattr(server, "_readiness", lambda: True)
    assert anyio.run(server.ready, internal).status_code == 200


@pytest.mark.parametrize("limit", [0, -1, 101, 10_000])
def test_search_rejects_out_of_range_limit(limit):
    assert "between 1 and 100" in server.search("acme", query="test", limit=limit)


def test_search_rejects_oversized_inputs():
    assert "Query is too long" in server.search("acme", query="q" * 4_001)
    assert "Too many keywords" in server.search("acme", keywords=["x"] * 21)
    assert "Each keyword" in server.search("acme", keywords=["x" * 501])
    assert "Filename filter" in server.search("acme", filename="x" * 501)


def test_add_knowledge_rejects_oversized_inputs_before_auth():
    assert "Text must contain" in server.add_knowledge("acme", "", "note.md")
    assert "Text must contain" in server.add_knowledge("acme", "x" * 24_001, "note.md")
    assert "File must contain" in server.add_knowledge("acme", "text", "")
    assert "Title is too long" in server.add_knowledge("acme", "text", "note.md", "x" * 501)


@pytest.mark.parametrize("collection", ["", "../acme", "acme/other", "=formula"])
def test_tools_reject_invalid_collection_names(collection):
    assert "Invalid collection" in server.search(collection, query="test")
    assert "Invalid collection" in server.add_knowledge(collection, "text", "note.md")


def test_sliding_window_rate_limiter():
    limiter = SlidingWindowRateLimiter(2)
    assert limiter.allow("carol", now=0)
    assert limiter.allow("carol", now=1)
    assert not limiter.allow("carol", now=2)
    assert limiter.allow("other", now=2)
    assert limiter.allow("carol", now=61)


def test_rate_limit_config(monkeypatch):
    monkeypatch.setenv("MCP_RATE_LIMIT_PER_MINUTE", "120")
    assert rate_limit_per_minute() == 120
    monkeypatch.setenv("MCP_RATE_LIMIT_PER_MINUTE", "0")
    with pytest.raises(RuntimeError, match="between"):
        rate_limit_per_minute()


def test_server_masks_unhandled_tool_errors():
    assert server.mcp._mask_error_details is True


def test_dependency_readiness_checks_qdrant_and_embedding(monkeypatch):
    called = []
    monkeypatch.setattr(
        server.client,
        "get_collections",
        lambda: called.append("qdrant"),
    )
    monkeypatch.setattr(
        server,
        "embed",
        lambda *args, **kwargs: [[0.1, 0.2]],
    )

    server._check_dependencies()

    assert called == ["qdrant"]


def test_org_visibility_requires_unrestricted_writer(monkeypatch):
    acl = Acl(
        {
            "grants": [
                {
                    "users": ["carol"],
                    "collections": ["acme"],
                    "spaces": ["yt:INF"],
                    "write": True,
                }
            ]
        }
    )
    monkeypatch.setattr(server, "ACL", acl)
    monkeypatch.setattr(server, "current_principal", lambda: "carol")

    result = server.add_knowledge("acme", "text", "note.md", visibility="org")

    assert "requires unrestricted write access" in result
