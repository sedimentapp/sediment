import pytest
import yaml

from sediment_mcp.acl import AclConfigError
from sediment_mcp_ee_webadmin.aclstore import AclStore

VALID = yaml.safe_dump({
    "user_groups": {"admins": ["alice"]},
    "grants": [
        {"user_groups": ["admins"], "collections": ["acme"],
         "spaces": ["*"], "unrestricted": True, "write": True},
    ],
})


@pytest.fixture
def store(tmp_path):
    return AclStore(str(tmp_path / "acl.db"))


def test_empty_store(store):
    assert store.latest() is None
    assert store.history() == []


def test_save_and_read_back(store):
    version, acl = store.save(VALID, author="alice")
    assert version == 1
    assert "acme" in acl.resolve("alice").collections

    latest = store.latest()
    assert latest.version == 1
    assert latest.yaml_text == VALID
    assert latest.author == "alice"
    assert latest.created_at > 0
    assert store.get(1) == latest
    assert store.get(99) is None


def test_versions_accumulate_newest_first(store):
    store.save(VALID, author="a")
    store.save(VALID, author="b")
    assert [v.version for v in store.history()] == [2, 1]
    assert store.latest().author == "b"


def test_invalid_config_not_persisted(store):
    with pytest.raises(AclConfigError):
        store.save("grants: []", author="alice")
    with pytest.raises(yaml.YAMLError):
        store.save("{unclosed", author="alice")
    assert store.latest() is None


def test_survives_reopen(tmp_path):
    path = str(tmp_path / "acl.db")
    AclStore(path).save(VALID, author="alice")
    reopened = AclStore(path).latest()
    assert reopened is not None
    assert reopened.version == 1
