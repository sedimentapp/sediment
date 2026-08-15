"""Page tests: real FastMCP app + register(), fake QdrantClient, real Acl."""

from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from fastmcp import FastMCP
from qdrant_client.http.exceptions import UnexpectedResponse
from starlette.testclient import TestClient

import sediment_mcp.server as core
from sediment_mcp.acl import Acl
from sediment_mcp_ee_webadmin.app import register
from sediment_mcp_ee_webadmin.auth import Signer

ACL_CONFIG = {
    "user_groups": {"admins": ["alice"], "infra-team": ["carol"]},
    "space_groups": {"infra": ["mm:chan1", "yt:INF"]},
    "grants": [
        {
            "user_groups": ["admins"],
            "collections": ["acme", "globex"],
            "spaces": ["*"],
            "unrestricted": True,
            "write": True,
        },
        {"user_groups": ["infra-team"], "collections": ["acme"], "space_groups": ["infra"]},
    ],
}


class FakeQdrant:
    """Just enough of QdrantClient for the inventory queries."""

    def __init__(self, ts_indexed=True):
        self._ts_indexed = ts_indexed
        self.deleted = []

    def get_collections(self):
        return SimpleNamespace(
            collections=[SimpleNamespace(name="acme"), SimpleNamespace(name="globex")]
        )

    def get_collection(self, name):
        if name not in ("acme", "globex"):
            raise UnexpectedResponse(
                status_code=404, reason_phrase="Not Found", content=b"", headers=httpx.Headers()
            )
        schema = {"space": object(), "source": object()}
        if self._ts_indexed:
            schema["ts"] = object()
        return SimpleNamespace(
            status=SimpleNamespace(value="green"), points_count=10, payload_schema=schema
        )

    def facet(self, collection, key, limit, exact):
        if key == "source":
            hits = [
                SimpleNamespace(value="mattermost", count=6),
                SimpleNamespace(value="youtrack", count=3),
            ]
        else:  # space
            hits = [
                SimpleNamespace(value="mm:chan1", count=6),
                SimpleNamespace(value="yt:INF", count=3),
            ]
        return SimpleNamespace(hits=hits)

    def delete(self, collection, points_selector=None, wait=None):
        self.deleted.append((collection, points_selector))

    def retrieve(self, collection, ids, with_payload=None, with_vectors=None):
        if collection == "acme" and ids == ["pt-1"]:
            return [SimpleNamespace(id="pt-1", payload={"source": "manual"})]
        if collection == "acme" and ids == ["ingested-1"]:
            return [SimpleNamespace(id="ingested-1", payload={"source": "youtrack"})]
        return []

    def scroll(self, collection, scroll_filter=None, limit=1, offset=None,
               with_payload=None, order_by=None):
        assert self._ts_indexed or order_by is None, "order_by without a ts index"
        assert scroll_filter is not None
        condition = scroll_filter.must[0]
        if condition.key == "source":  # manual-entries listing
            if collection == "acme" and condition.match.value == "manual" and offset is None:
                point = SimpleNamespace(
                    id="pt-1",
                    payload={
                        "author": "alice", "visibility": "org", "file": "note.md",
                        "title": "Note", "text": "hello world", "ts": 1_752_000_000,
                    },
                )
                return [point], None
            return [], None
        space = condition.match.value
        if space == "yt:INF" and order_by is not None:
            # a space where no point carries ts: order_by excludes them all
            return [], None
        payload = {"space_name": f"name of {space}", "source": space.split(":")[0]}
        if order_by is not None:
            payload["ts"] = 1_752_000_000
        return [SimpleNamespace(payload=payload)], None


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("MCP_ACL_DB", raising=False)
    monkeypatch.delenv("MCP_ACL_CONFIG", raising=False)
    monkeypatch.delenv("MCP_AUDIT_DB", raising=False)
    monkeypatch.setenv("MCP_ADMIN_AUTH", "dev")
    monkeypatch.setenv("MCP_ADMIN_DEV_PRINCIPAL", "alice")
    monkeypatch.setattr(core, "client", FakeQdrant())
    monkeypatch.setattr(core, "ACL", Acl(ACL_CONFIG))
    mcp = FastMCP("test-admin")
    register(mcp)
    with TestClient(mcp.http_app()) as tc:
        yield tc


def test_overview_lists_collections(client):
    resp = client.get("/admin")
    assert resp.status_code == 200
    assert "acme" in resp.text
    assert "globex" in resp.text
    assert "mattermost: 6" in resp.text


def test_spaces_inventory(client):
    resp = client.get("/admin/collections/acme")
    assert resp.status_code == 200
    assert "mm:chan1" in resp.text
    assert "name of mm:chan1" in resp.text
    # the ts-less space still shows up, with unknown freshness
    assert "yt:INF" in resp.text
    assert "—" in resp.text
    # 10 points total, 9 in spaces -> 1 unspaced warning
    assert "1 points have no" in resp.text


def test_spaces_without_ts_index(monkeypatch):
    monkeypatch.setenv("MCP_ADMIN_AUTH", "dev")
    monkeypatch.setenv("MCP_ADMIN_DEV_PRINCIPAL", "alice")
    monkeypatch.setattr(core, "client", FakeQdrant(ts_indexed=False))
    monkeypatch.setattr(core, "ACL", Acl(ACL_CONFIG))
    mcp = FastMCP("test-admin")
    register(mcp)
    with TestClient(mcp.http_app()) as tc:
        resp = tc.get("/admin/collections/acme")
    assert resp.status_code == 200
    assert "freshness is unavailable" in resp.text
    assert "mm:chan1" in resp.text


def test_spaces_unknown_collection_404(client):
    resp = client.get("/admin/collections/nope")
    assert resp.status_code == 404


def test_access_resolves_principal(client):
    resp = client.get("/admin/access", params={"principal": "carol"})
    assert resp.status_code == 200
    assert "mm:chan1" in resp.text
    assert "manual:carol" in resp.text
    assert "unrestricted" not in resp.text.split("Space → who sees it")[0].split("Known principals")[1]


def test_access_unknown_principal_denied(client):
    resp = client.get("/admin/access", params={"principal": "mallory"})
    assert resp.status_code == 200
    assert "deny-by-default" in resp.text


def test_access_space_viewers(client):
    resp = client.get("/admin/access", params={"space": "mm:chan1"})
    assert resp.status_code == 200
    assert "carol" in resp.text
    assert "explicit grant" in resp.text
    assert "alice" in resp.text
    assert "unrestricted (*)" in resp.text


def test_access_acl_disabled(monkeypatch):
    monkeypatch.setenv("MCP_ADMIN_AUTH", "dev")
    monkeypatch.setenv("MCP_ADMIN_DEV_PRINCIPAL", "alice")
    monkeypatch.setattr(core, "client", FakeQdrant())
    monkeypatch.setattr(core, "ACL", None)
    mcp = FastMCP("test-admin")
    register(mcp)
    with TestClient(mcp.http_app()) as tc:
        resp = tc.get("/admin/access")
    assert resp.status_code == 200
    assert "ACL is explicitly disabled" in resp.text


@pytest.fixture
def github_client(monkeypatch):
    monkeypatch.delenv("MCP_ACL_DB", raising=False)
    monkeypatch.delenv("MCP_ACL_CONFIG", raising=False)
    monkeypatch.delenv("MCP_AUDIT_DB", raising=False)
    monkeypatch.setenv("MCP_ADMIN_AUTH", "github")
    monkeypatch.setenv("MCP_ADMIN_IDENTITIES", "alice:10001")
    monkeypatch.setenv("MCP_ADMIN_SESSION_SECRET", "test-secret")
    monkeypatch.setenv("MCP_BASE_URL", "https://mcp.example")
    monkeypatch.setenv("GITHUB_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("GITHUB_OAUTH_CLIENT_SECRET", "csec")
    monkeypatch.setattr(core, "client", FakeQdrant())
    monkeypatch.setattr(core, "ACL", Acl(ACL_CONFIG))
    mcp = FastMCP("test-admin")
    register(mcp)
    with TestClient(mcp.http_app()) as tc:
        yield tc


def _csrf(html: str) -> str:
    import re

    match = re.search(r'name="csrf" value="([^"]+)"', html)
    assert match, "no csrf field on page"
    return match.group(1)


def test_manual_entries_listed(client):
    resp = client.get("/admin/manual")
    assert resp.status_code == 200
    assert "note.md" in resp.text
    assert "alice" in resp.text
    assert "hello world" in resp.text
    assert "delete…" in resp.text


def test_manual_delete_two_step(client, monkeypatch):
    fake = cast(Any, core.client)  # the FakeQdrant injected by the fixture
    confirm_page = client.get("/admin/manual", params={"confirm": "acme:pt-1"})
    assert "Delete now" in confirm_page.text

    resp = client.post(
        "/admin/manual/delete",
        data={"collection": "acme", "point_id": "pt-1", "csrf": _csrf(confirm_page.text)},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert fake.deleted == [("acme", ["pt-1"])]


def test_manual_delete_bad_csrf(client):
    resp = client.post(
        "/admin/manual/delete",
        data={"collection": "acme", "point_id": "pt-1", "csrf": "forged"},
    )
    assert resp.status_code == 403


def test_manual_delete_rejects_non_manual_point(client):
    confirm_page = client.get("/admin/manual", params={"confirm": "acme:pt-1"})
    resp = client.post(
        "/admin/manual/delete",
        data={
            "collection": "acme",
            "point_id": "ingested-1",
            "csrf": _csrf(confirm_page.text),
        },
    )
    assert resp.status_code == 409
    assert "not a manual entry" in resp.text


def test_audit_page_disabled_without_db(client):
    resp = client.get("/admin/audit")
    assert resp.status_code == 200
    assert "Audit is disabled" in resp.text


def test_audit_page_lists_events(monkeypatch, tmp_path):
    from sediment_mcp_ee_webadmin.audit import AuditLog

    db = str(tmp_path / "audit.db")
    AuditLog(db).record("alice", "search", "acme", {"query": "привет qdrant"}, True, None, 12)

    monkeypatch.delenv("MCP_ACL_DB", raising=False)
    monkeypatch.delenv("MCP_ACL_CONFIG", raising=False)
    monkeypatch.setenv("MCP_AUDIT_DB", db)
    monkeypatch.setenv("MCP_ADMIN_AUTH", "dev")
    monkeypatch.setenv("MCP_ADMIN_DEV_PRINCIPAL", "alice")
    monkeypatch.setattr(core, "client", FakeQdrant())
    monkeypatch.setattr(core, "ACL", Acl(ACL_CONFIG))
    mcp = FastMCP("test-admin")
    register(mcp)
    with TestClient(mcp.http_app()) as tc:
        resp = tc.get("/admin/audit")
        assert resp.status_code == 200
        assert "search" in resp.text
        # cyrillic must render as-is, not as \u04xx escapes
        assert "привет qdrant" in resp.text
        assert "\\u04" not in resp.text
        filtered = tc.get("/admin/audit", params={"principal": "nobody"})
        assert "Nothing matches" in filtered.text
        assert tc.get("/admin/audit", params={"limit": "abc"}).status_code == 400

        csv_resp = tc.get("/admin/audit.csv")
        assert csv_resp.status_code == 200
        assert csv_resp.headers["content-type"].startswith("text/csv")
        assert "attachment" in csv_resp.headers["content-disposition"]
        lines = csv_resp.text.strip().splitlines()
        assert lines[0].startswith("time_utc,principal,tool,")
        assert len(lines) == 2  # header + one event
        assert "привет qdrant" in lines[1]

        empty_csv = tc.get("/admin/audit.csv", params={"principal": "nobody"})
        assert len(empty_csv.text.strip().splitlines()) == 1  # header only


def test_audit_csv_neutralizes_spreadsheet_formulas(monkeypatch, tmp_path):
    from sediment_mcp_ee_webadmin.audit import AuditLog

    db = str(tmp_path / "audit.db")
    AuditLog(db).record(
        "alice", "search", '=HYPERLINK("https://evil.example")', {}, False, "+error", 1
    )
    monkeypatch.delenv("MCP_ACL_DB", raising=False)
    monkeypatch.delenv("MCP_ACL_CONFIG", raising=False)
    monkeypatch.setenv("MCP_AUDIT_DB", db)
    monkeypatch.setenv("MCP_ADMIN_AUTH", "dev")
    monkeypatch.setenv("MCP_ADMIN_DEV_PRINCIPAL", "alice")
    monkeypatch.setattr(core, "client", FakeQdrant())
    monkeypatch.setattr(core, "ACL", Acl(ACL_CONFIG))
    mcp = FastMCP("test-admin")
    register(mcp)
    with TestClient(mcp.http_app()) as tc:
        body = tc.get("/admin/audit.csv").text
    assert "'=HYPERLINK" in body
    assert "'+error" in body


def test_github_unauthenticated_redirects_to_login(github_client):
    resp = github_client.get("/admin", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/admin/login"


def test_github_login_redirects_to_github(github_client):
    resp = github_client.get("/admin/login", follow_redirects=False)
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert location.startswith("https://github.com/login/oauth/authorize?")
    assert "mcp.example%2Fauth%2Fcallback%2Fweb" in location
    state_cookie = resp.cookies.get("__Host-sediment_mcp_admin_oauth_state")
    assert state_cookie is not None


def test_github_valid_cookie_gets_page(github_client):
    cookie = Signer("test-secret").sign("alice", ttl=60)
    resp = github_client.get(
        "/admin", cookies={"__Host-sediment_mcp_admin_session": cookie}, follow_redirects=False
    )
    assert resp.status_code == 200
    assert "acme" in resp.text


def test_github_forged_cookie_redirects(github_client):
    cookie = Signer("wrong-secret").sign("alice", ttl=60)
    resp = github_client.get(
        "/admin", cookies={"__Host-sediment_mcp_admin_session": cookie}, follow_redirects=False
    )
    assert resp.status_code == 302
