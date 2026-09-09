import re

from fastmcp import FastMCP
import pytest
from starlette.testclient import TestClient
import yaml

from sediment_mcp import server
from sediment_mcp_ee_access.store import AccessStore
from sediment_mcp_ee_webadmin.app import register
from sediment_mcp_ee_webadmin import aclui


@pytest.fixture
def managed(monkeypatch, tmp_path):
    monkeypatch.setenv("MCP_ADMIN_AUTH", "dev")
    monkeypatch.setenv("MCP_ADMIN_DEV_PRINCIPAL", "admin")
    store = AccessStore(str(tmp_path / "access.db"), create=True)
    monkeypatch.setattr(server, "ACCESS", store)
    monkeypatch.delenv("MCP_AUDIT_DB", raising=False)
    mcp = FastMCP("managed-ui")
    register(mcp)
    with TestClient(mcp.http_app()) as client:
        yield client, store


def guards(client, path="/admin/acl"):
    page = client.get(path)
    assert page.status_code == 200
    values = {}
    for key in ["csrf", "revision", "base_version"]:
        match = re.search(fr'name="{key}" value="([^"]*)"', page.text)
        assert match is not None
        values[key] = match.group(1)
    return values


def add_user(client, principal="alice"):
    response = client.post("/admin/acl/draft", data={**guards(client), "operation": "user_add", "principal": principal})
    assert response.status_code == 200, response.text


def add_grant(client):
    response = client.post("/admin/acl/draft", data={**guards(client), "operation": "grant_save", "users": "alice",
                                                   "collection": "acme", "spaces": "yt:ONE", "write": "on"})
    assert response.status_code == 200, response.text


def test_setup_forms_preview_apply_and_export(managed):
    client, store = managed
    assert "Initial setup" in client.get("/admin/acl").text
    add_user(client)
    add_grant(client)
    assert not store.snapshot().is_active("alice")
    page = client.get("/admin/acl/preview")
    assert "Proposed access" in page.text and "yt:ONE" in page.text
    response = client.post("/admin/acl/apply", data=guards(client, "/admin/acl/preview"))
    assert response.status_code == 200
    assert store.snapshot().is_active("alice")
    export = yaml.safe_load(client.get("/admin/acl/export").text)
    assert export == store.latest().document
    assert "Version 2" in client.get("/admin/acl/history").text
    assert client.post("/admin/acl/edit", data={"yaml_text": "grants: []"}).status_code == 409


def test_csrf_conflict_and_invalid_import_preserve_active_and_draft(managed):
    client, store = managed
    old = guards(client)
    assert client.post("/admin/acl/draft", data={**old, "csrf": "bad"}).status_code == 403
    add_user(client)
    before = store.draft()
    response = client.post("/admin/acl/draft", data={**old, "operation": "user_add", "principal": "bob"})
    assert response.status_code == 409 and "bob" in response.text
    assert store.draft() == before
    response = client.post("/admin/acl/import", data={**guards(client), "document": "grants: []"})
    assert response.status_code == 400
    assert store.latest().version == 1
    assert store.draft() == before


def test_disable_and_restore_only_changes_draft(managed):
    client, store = managed
    add_user(client)
    add_grant(client)
    client.post("/admin/acl/apply", data=guards(client, "/admin/acl/preview"))
    response = client.post("/admin/acl/draft", data={**guards(client), "operation": "user_toggle", "principal": "alice"})
    assert response.status_code == 200
    assert store.snapshot().is_active("alice")
    client.post("/admin/acl/apply", data=guards(client, "/admin/acl/preview"))
    assert not store.snapshot().is_active("alice")
    response = client.post("/admin/acl/restore", data={**guards(client), "version": "2"})
    assert response.status_code == 200
    assert not store.snapshot().is_active("alice")
    assert store.draft().document["users"][0]["enabled"] is True


def test_github_confirmation_uses_numeric_id(managed, monkeypatch):
    client, store = managed

    async def profile(login):
        return {"github_id": "1001", "github_login": login}

    monkeypatch.setattr(aclui, "github_profile", profile)
    response = client.post("/admin/acl/github", data={**guards(client), "github_login": "Alice"})
    assert response.status_code == 200 and "1001" in response.text
    assert store.draft().document["users"] == []
    response = client.post("/admin/acl/draft", data={**guards(client), "operation": "user_add", "principal": "alice",
                                                   "github_login": "Alice", "github_id": "999"})
    assert response.status_code == 400
    response = client.post("/admin/acl/draft", data={**guards(client), "operation": "user_add", "principal": "alice",
                                                   "github_login": "Alice", "github_id": "1001"})
    assert response.status_code == 200
    assert store.draft().document["users"][0]["github_id"] == "1001"
