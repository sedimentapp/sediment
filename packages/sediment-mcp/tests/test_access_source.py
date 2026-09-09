import pytest
import yaml

from sediment_mcp.access import FileAccess, load_access
from sediment_mcp.acl import Acl


def test_file_snapshot_is_scoped_and_loaded_once(monkeypatch, tmp_path):
    path = tmp_path / "acl.yaml"
    path.write_text('grants:\n- users: [alice]\n  collections: [acme]\n  spaces: ["yt:ONE"]\n')
    monkeypatch.setenv("MCP_ACL_CONFIG", str(path))
    monkeypatch.delenv("MCP_ACL_DISABLE", raising=False)
    source = load_access()
    assert isinstance(source, FileAccess)
    path.write_text("invalid: [")
    grant = source.snapshot().resolve("alice", "acme")
    assert grant is not None and grant.spaces == {"yt:ONE", "manual:alice"}
    with pytest.raises(yaml.YAMLError):
        load_access()


@pytest.mark.parametrize("env", [
    {"MCP_ACCESS_DB": "/unused"},
    {"MCP_ACL_DB": "/unused"},
    {"MCP_ACCESS_MODE": "unknown"},
    {"MCP_ACCESS_MODE": "database", "MCP_ACL_CONFIG": "/unused", "MCP_ACCESS_DB": "/unused"},
    {"MCP_ACCESS_MODE": "database", "MCP_ACL_DISABLE": "1", "MCP_ACCESS_DB": "/unused"},
    {"MCP_ACCESS_MODE": "database"},
])
def test_invalid_source_configuration_fails(monkeypatch, env):
    for name in ["MCP_ACL_DB", "MCP_ACCESS_DB", "MCP_ACCESS_MODE", "MCP_ACL_CONFIG", "MCP_ACL_DISABLE"]:
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(RuntimeError):
        load_access()


def test_wildcard_does_not_escape_its_collection():
    source = FileAccess(Acl({"grants": [
        {"users": ["alice"], "collections": ["acme"], "spaces": ["*"], "unrestricted": True},
        {"users": ["alice"], "collections": ["globex"], "spaces": ["yt:ONE"]},
    ]}))
    grant = source.snapshot().resolve("alice", "globex")
    assert grant is not None and grant.space_condition() is not None
    assert grant.spaces == {"yt:ONE", "manual:alice"}


def test_managed_missing_package_never_falls_back(monkeypatch, tmp_path):
    import sediment_mcp.access as module
    monkeypatch.setenv("MCP_ACCESS_MODE", "database")
    monkeypatch.setenv("MCP_ACCESS_DB", str(tmp_path / "access.db"))
    monkeypatch.delenv("MCP_ACL_DISABLE", raising=False)
    monkeypatch.setattr(module, "entry_points", lambda **kwargs: [])
    with pytest.raises(RuntimeError, match="EE database access provider"):
        load_access()
