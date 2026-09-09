"""Portable managed-access documents and effective permission comparison."""

import copy
import json
import re

from sediment_mcp.access import AccessSnapshot
from sediment_mcp.acl import Acl, AclConfigError

FORMAT_VERSION = 1
_PRINCIPAL = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def empty_document() -> dict:
    return {"format_version": FORMAT_VERSION, "users": [],
            "policy": {"user_groups": {}, "space_groups": {}, "grants": []}}


def encode(document: dict) -> str:
    return json.dumps(document, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def validate(document: dict, revision: int = 0) -> AccessSnapshot:
    if not isinstance(document, dict) or set(document) != {"format_version", "users", "policy"}:
        raise AclConfigError("Access document requires exactly format_version, users and policy")
    if type(document["format_version"]) is not int or document["format_version"] != FORMAT_VERSION:
        raise AclConfigError("Unsupported access document format_version")
    if len(encode(document).encode()) > 1_048_576:
        raise AclConfigError("Access document exceeds 1 MiB")
    if not isinstance(document["users"], list):
        raise AclConfigError("users must be a list")
    users: set[str] = set()
    ids: set[str] = set()
    active: set[str] = set()
    identities: dict[str, str] = {}
    for user in document["users"]:
        if not isinstance(user, dict) or set(user) != {"principal", "github_id", "github_login", "enabled"}:
            raise AclConfigError("User requires principal, github_id, github_login and enabled")
        name = user["principal"]
        if not isinstance(name, str) or not _PRINCIPAL.fullmatch(name) or name in users:
            raise AclConfigError(f"Invalid or duplicate stable principal: {name!r}")
        users.add(name)
        if type(user["enabled"]) is not bool:
            raise AclConfigError(f"User {name}: enabled must be boolean")
        gid, login = user["github_id"], user["github_login"]
        if gid is not None:
            if not isinstance(gid, str) or not re.fullmatch(r"[1-9][0-9]*", gid) or gid in ids:
                raise AclConfigError(f"User {name}: invalid or duplicate GitHub ID")
            if login is not None and (not isinstance(login, str) or not login.strip()):
                raise AclConfigError(f"User {name}: GitHub login must be a non-empty string or null")
            ids.add(gid)
            if user["enabled"]:
                identities[gid] = name
        elif login is not None:
            raise AclConfigError(f"User {name}: GitHub login requires an ID")
        if user["enabled"]:
            active.add(name)
    acl = Acl(document["policy"], allow_empty=True)
    unknown = acl.principals() - users
    if unknown:
        raise AclConfigError(f"Policy references unregistered users: {sorted(unknown)}")
    return AccessSnapshot(revision, acl, frozenset(active), identities)


def collections(document: dict) -> set[str]:
    return {c for grant in document["policy"]["grants"] for c in grant["collections"]}


def permission_summary(snapshot: AccessSnapshot, principal: str, collection: str) -> dict:
    grant = snapshot.resolve(principal, collection)
    assert grant is not None
    return {"read": collection in grant.collections,
            "spaces": None if grant.spaces is None else sorted(grant.spaces),
            "add_knowledge": collection in grant.write_collections,
            "publish_org": collection in grant.unrestricted_write_collections}


def changes(before: dict, after: dict) -> list[dict]:
    old, new = validate(before), validate(after)
    old_users = {u["principal"]: u for u in before["users"]}
    new_users = {u["principal"]: u for u in after["users"]}
    result = []
    for principal in sorted(old_users.keys() | new_users.keys()):
        if old_users.get(principal) != new_users.get(principal):
            result.append({"kind": "account", "principal": principal, "collection": "account",
                           "before": old_users.get(principal), "after": new_users.get(principal)})
        for collection in sorted(collections(before) | collections(after)):
            a = permission_summary(old, principal, collection)
            b = permission_summary(new, principal, collection)
            if a != b:
                result.append({"kind": "grant", "principal": principal, "collection": collection, "before": a, "after": b})
    return result


def restore_document(current: dict, historical: dict) -> dict:
    """Keep subsequently registered identities, disabled, when restoring old policy."""
    restored = copy.deepcopy(historical)
    names = {u["principal"] for u in restored["users"]}
    for user in current["users"]:
        if user["principal"] not in names:
            restored["users"].append({**user, "enabled": False})
    validate(restored)
    return restored
