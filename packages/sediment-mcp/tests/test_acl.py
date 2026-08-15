import pytest

from sediment_mcp.acl import Acl, AclConfigError, load_acl

CONFIG = {
    "user_groups": {
        "admins": ["Alice", "bob"],
        "infra-team": ["carol"],
        "backend-team": ["carol", "dave"],
    },
    "space_groups": {
        "infra": ["mm:3tgwj6wrx3yu5rrm9mkkwth93h", "yt:INF", "yt:SysDev"],
        "backend": ["yt:Bcknd"],
    },
    "grants": [
        {
            "user_groups": ["admins"],
            "collections": ["acme", "globex"],
            "spaces": ["*"],
            "unrestricted": True,
            "write": True,
        },
        {
            "user_groups": ["infra-team"],
            "collections": ["acme"],
            "space_groups": ["infra"],
        },
        {
            "user_groups": ["backend-team"],
            "users": ["charlie"],
            "collections": ["acme"],
            "space_groups": ["backend"],
            "spaces": ["yt:ONEOFF"],
            "write": True,
        },
    ],
}


def test_unknown_principal_denied_everything():
    grant = Acl(CONFIG).resolve("mallory")
    assert grant.collections == frozenset()
    assert grant.spaces == frozenset()
    assert grant.write_collections == frozenset()


def test_grant_union_across_user_groups():
    grant = Acl(CONFIG).resolve("carol")
    assert grant.collections == {"acme"}
    assert grant.spaces == {
        "mm:3tgwj6wrx3yu5rrm9mkkwth93h",
        "yt:INF",
        "yt:SysDev",
        "yt:Bcknd",
        "yt:ONEOFF",
        "manual:carol",
    }
    # write comes only from the backend grant
    assert grant.write_collections == {"acme"}


def test_direct_user_in_grant():
    grant = Acl(CONFIG).resolve("charlie")
    assert grant.spaces == {"yt:Bcknd", "yt:ONEOFF", "manual:charlie"}


def test_manual_space_auto_granted():
    spaces = Acl(CONFIG).resolve("dave").spaces
    assert spaces is not None
    assert "manual:dave" in spaces


def test_wildcard_gives_unrestricted():
    grant = Acl(CONFIG).resolve("bob")
    assert grant.spaces is None
    assert grant.collections == {"acme", "globex"}
    assert grant.write_collections == {"acme", "globex"}


def test_principal_match_case_insensitive():
    assert Acl(CONFIG).resolve("alice").spaces is None


def test_unrestricted_write_is_per_collection():
    # unrestricted-write on acme but space-restricted-write on globex: globex must
    # be excluded (regression for the cross-collection org-write bypass)
    config = {
        "grants": [
            {"users": ["mix"], "collections": ["acme"], "spaces": ["*"],
             "unrestricted": True, "write": True},
            {"users": ["mix"], "collections": ["globex"], "spaces": ["yt:X"], "write": True},
        ],
    }
    grant = Acl(config).resolve("mix")
    assert grant.spaces is None  # global spaces collapse to unrestricted (read)
    assert grant.write_collections == {"acme", "globex"}
    assert grant.unrestricted_write_collections == {"acme"}


def _one_grant(grant: dict, **top) -> dict:
    return {"grants": [grant], **top}


def test_wildcard_requires_unrestricted_flag():
    config = _one_grant({"users": ["a"], "collections": ["acme"], "spaces": ["*"]})
    with pytest.raises(AclConfigError, match="unrestricted"):
        Acl(config)


def test_unknown_grant_key_rejected():
    config = _one_grant({"users": ["a"], "collections": ["acme"], "spaces": ["yt:X"], "wirte": True})
    with pytest.raises(AclConfigError, match="wirte"):
        Acl(config)


def test_unknown_top_level_key_rejected():
    with pytest.raises(AclConfigError, match="groups'"):
        Acl({**CONFIG, "groups": {}})


def test_unknown_user_group_ref_rejected():
    config = _one_grant({"user_groups": ["ghosts"], "collections": ["acme"], "spaces": ["yt:X"]})
    with pytest.raises(AclConfigError, match="ghosts"):
        Acl(config)


def test_unknown_space_group_ref_rejected():
    config = _one_grant({"users": ["a"], "collections": ["acme"], "space_groups": ["nope"]})
    with pytest.raises(AclConfigError, match="nope"):
        Acl(config)


def test_grant_without_principals_rejected():
    config = _one_grant({"collections": ["acme"], "spaces": ["yt:X"]})
    with pytest.raises(AclConfigError, match="user_groups and/or users"):
        Acl(config)


def test_grant_without_spaces_rejected():
    config = _one_grant({"users": ["a"], "collections": ["acme"]})
    with pytest.raises(AclConfigError, match="space_groups and/or spaces"):
        Acl(config)


def test_wildcard_inside_space_group_rejected():
    config = _one_grant(
        {"users": ["a"], "collections": ["acme"], "space_groups": ["g"]},
        space_groups={"g": ["*"]},
    )
    with pytest.raises(AclConfigError, match="only allowed inline"):
        Acl(config)


def test_empty_user_group_rejected():
    config = _one_grant(
        {"user_groups": ["g"], "collections": ["acme"], "spaces": ["yt:X"]},
        user_groups={"g": []},
    )
    with pytest.raises(AclConfigError, match="user_groups.g"):
        Acl(config)


def test_bad_space_prefix_rejected():
    config = _one_grant({"users": ["a"], "collections": ["acme"], "spaces": ["slack:general"]})
    with pytest.raises(AclConfigError, match="slack:general"):
        Acl(config)


def test_missing_grants_rejected():
    with pytest.raises(AclConfigError, match="grants"):
        Acl({})


def test_principals_lists_group_members_and_direct_users():
    assert Acl(CONFIG).principals() == {"alice", "bob", "carol", "dave", "charlie"}


def test_missing_acl_requires_explicit_disable(monkeypatch):
    monkeypatch.delenv("MCP_ACL_CONFIG", raising=False)
    monkeypatch.delenv("MCP_ACL_DISABLE", raising=False)
    with pytest.raises(RuntimeError, match="MCP_ACL_CONFIG is required"):
        load_acl()


def test_acl_can_be_explicitly_disabled(monkeypatch):
    monkeypatch.delenv("MCP_ACL_CONFIG", raising=False)
    monkeypatch.setenv("MCP_ACL_DISABLE", "1")
    assert load_acl() is None


def test_acl_config_and_disable_are_mutually_exclusive(monkeypatch, tmp_path):
    path = tmp_path / "acl.yaml"
    path.write_text("grants: []\n")
    monkeypatch.setenv("MCP_ACL_CONFIG", str(path))
    monkeypatch.setenv("MCP_ACL_DISABLE", "1")
    with pytest.raises(RuntimeError, match="mutually exclusive"):
        load_acl()
