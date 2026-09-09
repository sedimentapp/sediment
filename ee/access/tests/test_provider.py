import pytest

from sediment_mcp.access import load_access
import sediment_mcp_ee_license as license_module


def test_managed_missing_license_does_not_create_database(monkeypatch, tmp_path):
    monkeypatch.setattr(license_module, "_cached_claims", None)
    for key in ["MCP_LICENSE", "MCP_LICENSE_FILE", "MCP_ACL_DISABLE"]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MCP_ACCESS_MODE", "database")
    path = tmp_path / "access.db"
    monkeypatch.setenv("MCP_ACCESS_DB", str(path))
    with pytest.raises(RuntimeError, match="license check failed"):
        load_access()
    assert not path.exists()
