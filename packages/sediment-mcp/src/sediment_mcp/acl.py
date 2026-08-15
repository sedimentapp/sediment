"""Config-driven ACL: principal -> allowed collections and spaces.

MCP_ACL_CONFIG points to a YAML file. When unset, startup fails unless
MCP_ACL_DISABLE=1 explicitly opts into allow-all (every authenticated principal
sees everything, with a prominent warning). When set, the config is loaded once at startup and any problem
(missing file, invalid YAML, schema violation) is a fatal startup error:
the server must never come up half-protected.

Config shape — user groups and space groups are defined once and bound
together by grants, so channel-id lists are never repeated per user:

    user_groups:
      admins: [alice, bob]           # principals (static token names / github logins)
      infra-team: [carol, dave]

    space_groups:
      infra:
        - "mm:<channel_id>"
        - "yt:INF"
        - "yt:SysDev"

    grants:
      - user_groups: [admins]
        collections: [acme, globex]
        spaces: ["*"]                  # literal "*" = every space, incl. DMs/personal
        unrestricted: true             # mandatory opt-in for "*"
        write: true                    # allows add_knowledge into the collections
      - user_groups: [infra-team]
        users: [charlie]               # optional direct principals, additive
        collections: [acme]
        space_groups: [infra]
        spaces: ["yt:ONEOFF"]          # optional inline extras, additive

Semantics: a principal's access is the union of all grants that reach it
(via a user group or a direct `users` entry); unknown principals get nothing
(deny-by-default). Space values are exact — no globbing (Qdrant has no glob
filter condition; expansion against a live space inventory is phase-2
territory). Every principal with a concrete space list additionally sees
`manual:<principal>` (their own add_knowledge notes) and manual entries
stamped visibility=org.
"""

import os
import re
from dataclasses import dataclass

import yaml
from fastmcp.utilities.logging import get_logger
from knowledge_schema import SPACE_PREFIXES
from qdrant_client.models import FieldCondition, Filter, MatchAny, MatchValue

logger = get_logger(__name__)

WILDCARD = "*"
_SPACE_RE = re.compile(rf"^({'|'.join(re.escape(p) for p in SPACE_PREFIXES.values())}):.+$")

_TOP_KEYS = {"user_groups", "space_groups", "grants"}
_GRANT_KEYS = {"user_groups", "users", "collections", "space_groups", "spaces", "unrestricted", "write"}


class AclConfigError(Exception):
    """The ACL config is invalid; the server must not start."""


@dataclass(frozen=True)
class Grant:
    """Resolved access for one principal. spaces=None means unrestricted."""

    collections: frozenset[str]
    spaces: frozenset[str] | None
    write_collections: frozenset[str]
    # per-collection (unlike global `spaces`): collections reached by an
    # unrestricted write grant; org-visible writes gate on this
    unrestricted_write_collections: frozenset[str]

    def space_condition(self) -> Filter | None:
        """OR-condition to nest inside the outer `must` list (fail-closed:
        points without `space` and without visibility=org match neither branch)."""
        if self.spaces is None:
            return None
        return Filter(
            should=[
                FieldCondition(key="space", match=MatchAny(any=sorted(self.spaces))),
                FieldCondition(key="visibility", match=MatchValue(value="org")),
            ]
        )


EMPTY_GRANT = Grant(
    collections=frozenset(),
    spaces=frozenset(),
    write_collections=frozenset(),
    unrestricted_write_collections=frozenset(),
)


def _require(cond: bool, path: str, msg: str) -> None:
    if not cond:
        raise AclConfigError(f"{path}: {msg}")


def _str_list(value: object, path: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise AclConfigError(f"{path}: must be a non-empty list")
    for i, item in enumerate(value):
        _require(isinstance(item, str) and item.strip() != "", f"{path}[{i}]", "must be a non-empty string")
    return value


def _space_format_msg(space: str) -> str:
    return (
        f"{space!r} does not match <prefix>:<key> "
        f"(prefixes: {', '.join(sorted(set(SPACE_PREFIXES.values())))})"
    )


class Acl:
    def __init__(self, config: dict) -> None:
        _require(isinstance(config, dict), "<root>", "must be a mapping")
        unknown_top = set(config) - _TOP_KEYS
        _require(not unknown_top, "<root>", f"unknown keys: {sorted(unknown_top)}")

        user_groups = config.get("user_groups") or {}
        _require(isinstance(user_groups, dict), "user_groups", "must be a mapping")
        self._user_groups: dict[str, frozenset[str]] = {
            name: frozenset(m.lower() for m in _str_list(members, f"user_groups.{name}"))
            for name, members in user_groups.items()
        }

        space_groups = config.get("space_groups") or {}
        _require(isinstance(space_groups, dict), "space_groups", "must be a mapping")
        self._space_groups: dict[str, frozenset[str]] = {}
        for name, spaces in space_groups.items():
            path = f"space_groups.{name}"
            values = _str_list(spaces, path)
            for i, space in enumerate(values):
                _require(
                    bool(_SPACE_RE.fullmatch(space)), f"{path}[{i}]",
                    _space_format_msg(space) + '; "*" is only allowed inline in a grant',
                )
            self._space_groups[name] = frozenset(values)

        grants = config.get("grants")
        if not isinstance(grants, list) or not grants:
            raise AclConfigError("grants: must be a non-empty list")
        self._grants: list[dict] = []
        for i, grant in enumerate(grants):
            path = f"grants[{i}]"
            _require(isinstance(grant, dict), path, "must be a mapping")
            unknown = set(grant) - _GRANT_KEYS
            _require(not unknown, path, f"unknown keys: {sorted(unknown)} (typo?)")

            group_names = (
                _str_list(grant["user_groups"], f"{path}.user_groups") if "user_groups" in grant else []
            )
            for g in group_names:
                _require(g in self._user_groups, f"{path}.user_groups", f"unknown user group {g!r}")
            users = [
                u.lower()
                for u in (_str_list(grant["users"], f"{path}.users") if "users" in grant else [])
            ]
            _require(bool(group_names or users), path, "needs user_groups and/or users")

            collections = _str_list(grant.get("collections"), f"{path}.collections")

            unrestricted = grant.get("unrestricted", False)
            _require(isinstance(unrestricted, bool), f"{path}.unrestricted", "must be a boolean")
            write = grant.get("write", False)
            _require(isinstance(write, bool), f"{path}.write", "must be a boolean")

            sg_names = (
                _str_list(grant["space_groups"], f"{path}.space_groups") if "space_groups" in grant else []
            )
            for g in sg_names:
                _require(g in self._space_groups, f"{path}.space_groups", f"unknown space group {g!r}")
            inline = _str_list(grant["spaces"], f"{path}.spaces") if "spaces" in grant else []
            for j, space in enumerate(inline):
                if space == WILDCARD:
                    _require(
                        unrestricted, f"{path}.spaces[{j}]",
                        '"*" grants every space including DMs/personal ones; '
                        "set `unrestricted: true` on the grant to confirm",
                    )
                else:
                    _require(bool(_SPACE_RE.fullmatch(space)), f"{path}.spaces[{j}]", _space_format_msg(space))

            if WILDCARD in inline:
                spaces: frozenset[str] | None = None
            else:
                combined = frozenset(inline).union(*(self._space_groups[g] for g in sg_names)) \
                    if sg_names else frozenset(inline)
                _require(bool(combined), path, "needs space_groups and/or spaces")
                spaces = combined

            self._grants.append({
                "principals": frozenset(users),
                "user_groups": tuple(group_names),
                "collections": frozenset(collections),
                "spaces": spaces,
                "write": write,
            })

    def principals(self) -> frozenset[str]:
        """Every principal named in the config — user-group members plus direct
        `users` entries. For admin tooling (reverse lookups); not enforcement."""
        named: set[str] = set()
        for members in self._user_groups.values():
            named |= members
        for grant in self._grants:
            named |= grant["principals"]
        return frozenset(named)

    def _covers(self, grant: dict, principal: str) -> bool:
        return principal in grant["principals"] or any(
            principal in self._user_groups[g] for g in grant["user_groups"]
        )

    def resolve(self, principal: str) -> Grant:
        """Union of all grants reaching the principal; unknown principal -> deny all."""
        principal = principal.lower()
        collections: set[str] = set()
        spaces: set[str] | None = set()
        write_collections: set[str] = set()
        unrestricted_write_collections: set[str] = set()
        matched = False

        for grant in self._grants:
            if not self._covers(grant, principal):
                continue
            matched = True
            collections |= grant["collections"]
            if grant["write"]:
                write_collections |= grant["collections"]
                if grant["spaces"] is None:
                    unrestricted_write_collections |= grant["collections"]
            if grant["spaces"] is None:
                spaces = None
            elif spaces is not None:
                spaces |= grant["spaces"]

        if not matched:
            return EMPTY_GRANT
        if spaces is not None:
            spaces.add(f"{SPACE_PREFIXES['manual']}:{principal}")
        return Grant(
            collections=frozenset(collections),
            spaces=None if spaces is None else frozenset(spaces),
            write_collections=frozenset(write_collections),
            unrestricted_write_collections=frozenset(unrestricted_write_collections),
        )


def load_acl() -> Acl | None:
    """Build the ACL; allow-all requires an explicit MCP_ACL_DISABLE=1 opt-in."""
    path = os.environ.get("MCP_ACL_CONFIG")
    disabled = os.environ.get("MCP_ACL_DISABLE") == "1"
    if path and disabled:
        raise RuntimeError("MCP_ACL_CONFIG and MCP_ACL_DISABLE=1 are mutually exclusive")
    if not path:
        if not disabled:
            raise RuntimeError(
                "MCP_ACL_CONFIG is required; set MCP_ACL_DISABLE=1 only for explicit allow-all"
            )
        logger.warning(
            "MCP_ACL_DISABLE=1 — ACL DISABLED, every authenticated "
            "principal can read and write every collection"
        )
        return None
    with open(path) as f:
        config = yaml.safe_load(f)
    acl = Acl(config)
    logger.info("ACL enabled from %s (%d grants)", path, len(acl._grants))
    return acl
