import copy
import sqlite3

import pytest

from sediment_mcp.acl import AclConfigError
from sediment_mcp_ee_access.model import empty_document, restore_document
from sediment_mcp_ee_access.store import AccessStore, ConflictError


def document():
    doc = empty_document()
    doc["users"] = [{"principal": "alice", "github_id": "1001", "github_login": "alice", "enabled": True}]
    doc["policy"]["grants"] = [
        {"users": ["alice"], "collections": ["acme"], "spaces": ["*"], "unrestricted": True},
        {"users": ["alice"], "collections": ["globex"], "spaces": ["yt:ONE"]},
    ]
    return doc


def apply(store, doc):
    draft = store.draft()
    saved = store.save_draft(doc, author="admin", revision=draft.revision, base_version=draft.base_version)
    return store.apply(author="admin", revision=saved.revision, base_version=saved.base_version, check_seats=lambda _: None)


def test_draft_not_active_and_restart_and_collection_isolation(tmp_path):
    path = str(tmp_path / "access.db")
    store = AccessStore(path, create=True)
    assert not store.snapshot().is_active("alice")
    saved = store.save_draft(document(), author="admin", revision=0, base_version=1)
    assert not store.snapshot().is_active("alice")
    other = AccessStore(path)
    assert other.draft() == saved
    other.apply(author="admin", revision=saved.revision, base_version=1, check_seats=lambda _: None)
    snap = store.snapshot()
    assert snap.is_active("alice")
    a, b, missing = [snap.resolve("alice", c) for c in ["acme", "globex", "absent"]]
    assert a is not None and b is not None and missing is not None
    assert a.spaces is None
    assert b.spaces == {"yt:ONE", "manual:alice"}
    assert not missing.collections
    assert snap.github_identities == {"1001": "alice"}


def test_stale_writers_and_failed_license_do_not_apply(tmp_path):
    store = AccessStore(str(tmp_path / "access.db"), create=True)
    store.save_draft(document(), author="a", revision=0, base_version=1)
    with pytest.raises(ConflictError):
        store.save_draft(document(), author="b", revision=0, base_version=1)
    with pytest.raises(ConflictError):
        store.apply(author="b", revision=0, base_version=1, check_seats=lambda _: None)

    def reject(_):
        raise ValueError("License limit")

    with pytest.raises(ValueError, match="License"):
        store.apply(author="a", revision=1, base_version=1, check_seats=reject)
    assert store.latest().version == 1
    assert len(store.history()) == 1


def test_disabled_users_and_immutable_identity(tmp_path):
    store = AccessStore(str(tmp_path / "access.db"), create=True)
    doc = document()
    apply(store, doc)
    doc["users"][0]["enabled"] = False
    apply(store, doc)
    assert not store.snapshot().is_active("alice")
    assert store.snapshot().github_identities == {}
    denied = store.snapshot().resolve("alice", "acme")
    assert denied is not None and not denied.collections
    bad = copy.deepcopy(doc)
    bad["users"][0]["github_id"] = "1002"
    with pytest.raises(AclConfigError, match="immutable"):
        apply(store, bad)
    with pytest.raises(AclConfigError, match="cannot be removed"):
        apply(store, empty_document())
    restored = restore_document(doc, empty_document())
    assert restored["users"][0]["enabled"] is False
    assert restored["policy"]["grants"] == []


def test_missing_or_corrupt_database_never_recreates_or_uses_cached_access(tmp_path):
    path = tmp_path / "access.db"
    store = AccessStore(str(path), create=True)
    apply(store, document())
    path.unlink()
    with pytest.raises(sqlite3.OperationalError):
        store.snapshot()
    assert not path.exists()
    path.write_bytes(b"corrupt")
    with pytest.raises(sqlite3.DatabaseError):
        AccessStore(str(path))
