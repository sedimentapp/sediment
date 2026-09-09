import hashlib
import sqlite3

import pytest
import yaml

from sediment_mcp.acl import AclConfigError
from sediment_mcp_ee_access.cli import migration_document
from sediment_mcp_ee_access.model import validate


def test_migration_reads_latest_and_reports_scope_narrowing_without_modifying_source(tmp_path):
    path = tmp_path / "legacy.db"
    policy = {"grants": [
        {"users": ["alice"], "collections": ["acme"], "spaces": ["*"], "unrestricted": True},
        {"users": ["alice"], "collections": ["globex"], "spaces": ["yt:ONE"]},
    ]}
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE acl_versions(version INTEGER, yaml_text TEXT)")
    conn.execute("INSERT INTO acl_versions VALUES(1, 'invalid old history')")
    conn.execute("INSERT INTO acl_versions VALUES(2, ?)", (yaml.safe_dump(policy),))
    conn.commit()
    conn.close()
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    doc, report = migration_document(path, {"github_identities": {"1001": "alice"}, "static_principals": ["alice"]})
    assert len(doc["users"]) == 1
    assert report["legacy_version"] == 2
    assert len(report["collection_scope_changes"]) == 1
    assert report["collection_scope_changes"][0]["collection"] == "globex"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    snap = validate(doc)
    assert snap.github_identities == {"1001": "alice"}
    with pytest.raises(AclConfigError, match="inventory"):
        migration_document(path, {"github_identities": {}, "static_principals": []})
