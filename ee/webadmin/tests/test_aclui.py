"""ACL section tests: view, editor (validation, CSRF, hot-reload), history/restore."""

import copy
import re

import pytest
import yaml
from fastmcp import FastMCP
from starlette.testclient import TestClient

import sediment_mcp.server as core
from sediment_mcp_ee_webadmin.aclstore import AclStore
from sediment_mcp_ee_webadmin.app import register

from test_pages import ACL_CONFIG, FakeQdrant


def _csrf(html: str) -> str:
    match = re.search(r'name="csrf" value="([^"]+)"', html)
    assert match, "no csrf field on page"
    return match.group(1)


@pytest.fixture
def acl_env(monkeypatch, tmp_path):
    config_file = tmp_path / "acl.yaml"
    config_file.write_text(yaml.safe_dump(ACL_CONFIG))
    monkeypatch.setenv("MCP_ADMIN_AUTH", "dev")
    monkeypatch.setenv("MCP_ADMIN_DEV_PRINCIPAL", "alice")
    monkeypatch.setenv("MCP_ACL_CONFIG", str(config_file))
    monkeypatch.setenv("MCP_ACL_DB", str(tmp_path / "acl.db"))
    # conftest sets MCP_ACL_DISABLE=1 globally; combining it with MCP_ACL_DB is
    # the contradictory configuration _load_store refuses to start with
    monkeypatch.delenv("MCP_ACL_DISABLE", raising=False)
    monkeypatch.setattr(core, "client", FakeQdrant())
    monkeypatch.setattr(core, "ACL", None)  # register() must load it from the seed
    return tmp_path


@pytest.fixture
def client(acl_env):
    mcp = FastMCP("test-admin")
    register(mcp)
    with TestClient(mcp.http_app()) as tc:
        yield tc


def test_empty_db_seeded_from_file_and_enforced(acl_env, client):
    resp = client.get("/admin/acl")
    assert resp.status_code == 200
    assert "DB version 1 by seed:MCP_ACL_CONFIG" in resp.text
    # register() swapped the None ACL for the seeded one
    assert core.ACL is not None
    assert "acme" in core.ACL.resolve("alice").collections
    seeded = AclStore(str(acl_env / "acl.db")).latest()
    assert seeded is not None
    assert seeded.version == 1


def test_view_renders_groups_and_grants(client):
    resp = client.get("/admin/acl")
    assert resp.status_code == 200
    assert "admins" in resp.text
    assert "infra-team" in resp.text
    assert "mm:chan1" in resp.text
    assert "name of mm:chan1" in resp.text  # resolved space name
    assert "unrestricted" in resp.text


def test_edit_save_new_version_hot_reloads(client):
    page = client.get("/admin/acl/edit")
    assert "admins" in page.text

    config = copy.deepcopy(ACL_CONFIG)
    config["grants"][1]["users"] = ["neo"]
    resp = client.post(
        "/admin/acl/edit",
        data={"yaml_text": yaml.safe_dump(config), "csrf": _csrf(page.text)},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    # enforcement changed without restart
    assert core.ACL is not None
    assert "acme" in core.ACL.resolve("neo").collections
    assert "DB version 2 by alice" in client.get("/admin/acl").text


def test_edit_rejects_invalid_config(client):
    page = client.get("/admin/acl/edit")
    resp = client.post(
        "/admin/acl/edit",
        data={"yaml_text": "grants: []", "csrf": _csrf(page.text)},
    )
    assert resp.status_code == 400
    assert "Rejected" in resp.text
    # nothing persisted, still on version 1
    assert "DB version 1" in client.get("/admin/acl").text


def test_edit_rejects_empty_config(client):
    page = client.get("/admin/acl/edit")
    resp = client.post(
        "/admin/acl/edit", data={"yaml_text": "  ", "csrf": _csrf(page.text)}
    )
    assert resp.status_code == 400
    assert "cannot be disabled" in resp.text


def test_edit_rejects_bad_csrf(client):
    resp = client.post(
        "/admin/acl/edit",
        data={"yaml_text": yaml.safe_dump(ACL_CONFIG), "csrf": "forged"},
    )
    assert resp.status_code == 403


def test_history_and_restore(client):
    page = client.get("/admin/acl/edit")
    config = copy.deepcopy(ACL_CONFIG)
    config["grants"][1]["users"] = ["neo"]
    client.post(
        "/admin/acl/edit",
        data={"yaml_text": yaml.safe_dump(config), "csrf": _csrf(page.text)},
    )

    history = client.get("/admin/acl/history", params={"v": 1})
    assert history.status_code == 200
    assert "Restore this version" in history.text

    resp = client.post(
        "/admin/acl/restore",
        data={"version": "1", "csrf": _csrf(history.text)},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    # v1 has no neo; restored as version 3
    assert core.ACL is not None
    assert core.ACL.resolve("neo").collections == frozenset()
    assert "restore of v1" in client.get("/admin/acl/history").text


def test_readonly_without_db(monkeypatch):
    monkeypatch.delenv("MCP_ACL_DB", raising=False)
    monkeypatch.delenv("MCP_ACL_CONFIG", raising=False)
    monkeypatch.setenv("MCP_ADMIN_AUTH", "dev")
    monkeypatch.setenv("MCP_ADMIN_DEV_PRINCIPAL", "alice")
    monkeypatch.setattr(core, "client", FakeQdrant())
    monkeypatch.setattr(core, "ACL", None)
    mcp = FastMCP("test-admin")
    register(mcp)
    with TestClient(mcp.http_app()) as tc:
        view = tc.get("/admin/acl")
        assert view.status_code == 200
        assert "editing disabled" in view.text
        assert "not enforced" in view.text
        assert tc.get("/admin/acl/edit").status_code == 409
        assert tc.get("/admin/acl/history").status_code == 409


def test_db_with_acl_disable_fails_fast(monkeypatch, tmp_path):
    # conftest's MCP_ACL_DISABLE=1 left in place on purpose: a store that would
    # re-enable ACL on an allow-all server is a fatal misconfiguration
    monkeypatch.setenv("MCP_ACL_DB", str(tmp_path / "acl.db"))
    monkeypatch.setenv("MCP_ADMIN_AUTH", "dev")
    monkeypatch.setenv("MCP_ADMIN_DEV_PRINCIPAL", "alice")
    monkeypatch.setattr(core, "client", FakeQdrant())
    monkeypatch.setattr(core, "ACL", None)
    with pytest.raises(RuntimeError, match="contradictory"):
        register(FastMCP("test-admin"))
