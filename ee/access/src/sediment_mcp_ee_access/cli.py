"""Explicit migration and portable managed-access export/import."""

import argparse
import json
import logging
from pathlib import Path
import sqlite3

import yaml

from sediment_mcp.acl import Acl, AclConfigError
from sediment_mcp_ee_access import check_seats
from sediment_mcp_ee_access.model import permission_summary, validate
from sediment_mcp_ee_access.store import AccessStore


def migration_document(legacy_db: Path, identities: dict) -> tuple[dict, dict]:
    if set(identities) != {"github_identities", "static_principals"}:
        raise ValueError("Identity inventory requires github_identities (ID to principal) and static_principals")
    if not isinstance(identities["github_identities"], dict) or not isinstance(identities["static_principals"], list):
        raise ValueError("Invalid identity inventory types")
    conn = sqlite3.connect(legacy_db.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        row = conn.execute("SELECT version,yaml_text FROM acl_versions ORDER BY version DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    if row is None:
        raise ValueError("Legacy ACL database has no active version")
    policy = yaml.safe_load(row[1])
    old = Acl(policy)
    users = {}
    for gid, principal in identities["github_identities"].items():
        if principal in users:
            raise ValueError(f"Ambiguous GitHub binding for {principal!r}")
        users[principal] = {"principal": principal, "github_id": str(gid),
                            "github_login": None, "enabled": True}
    for principal in identities["static_principals"]:
        if principal not in users:
            users[principal] = {"principal": principal, "github_id": None,
                                "github_login": None, "enabled": True}
    unknown = old.principals() - users.keys()
    if unknown:
        raise AclConfigError(f"Legacy policy principals missing from explicit identity inventory: {sorted(unknown)}")
    document = {"format_version": 1, "users": list(users.values()), "policy": policy}
    snapshot = validate(document)
    differences = []
    for principal in sorted(users):
        legacy = old.resolve(principal)
        for collection in sorted(legacy.collections):
            before = {"read": True, "spaces": None if legacy.spaces is None else sorted(legacy.spaces),
                      "add_knowledge": collection in legacy.write_collections,
                      "publish_org": collection in legacy.unrestricted_write_collections}
            after = permission_summary(snapshot, principal, collection)
            if before != after:
                differences.append({"principal": principal, "collection": collection, "before": before, "after": after})
    return document, {"legacy_version": row[0], "collection_scope_changes": differences}


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    migrate = commands.add_parser("migrate")
    migrate.add_argument("--legacy-db", type=Path, required=True)
    migrate.add_argument("--identities", type=Path, required=True)
    migrate.add_argument("--report", type=Path, required=True)
    migrate.add_argument("--output-db", type=Path, help="Omit to inspect the migration without creating a database")
    migrate.add_argument("--accept-scope-changes", action="store_true")
    export = commands.add_parser("export")
    export.add_argument("--db", required=True)
    export.add_argument("--output", type=Path, required=True)
    imp = commands.add_parser("import")
    imp.add_argument("--db", required=True)
    imp.add_argument("--document", type=Path, required=True)
    imp.add_argument("--author", required=True)
    imp.add_argument("--draft-revision", type=int, required=True)
    imp.add_argument("--base-version", type=int, required=True)
    args = parser.parse_args()
    if args.command == "migrate":
        doc, report = migration_document(args.legacy_db, yaml.safe_load(args.identities.read_text()))
        with args.report.open("x") as f:
            json.dump(report, f, indent=2)
        if args.output_db:
            if report["collection_scope_changes"] and not args.accept_scope_changes:
                raise ValueError("Review the scope-change report, then explicitly pass --accept-scope-changes")
            snapshot = validate(doc)
            assert snapshot.active_principals is not None
            check_seats(snapshot.active_principals)
            store = AccessStore(str(args.output_db), create=True)
            draft = store.save_draft(doc, author="migration", revision=0, base_version=1)
            store.apply(author="migration", revision=draft.revision, base_version=1, check_seats=check_seats,
                        note=f"Imported legacy ACL version {report['legacy_version']}")
    elif args.command == "export":
        doc = AccessStore(args.db).latest().document
        validate(doc)
        with args.output.open("x") as f:
            yaml.safe_dump(doc, f, sort_keys=False)
    else:
        store = AccessStore(args.db)
        draft = store.save_draft(yaml.safe_load(args.document.read_text()), author=args.author,
                                 revision=args.draft_revision, base_version=args.base_version)
        print(json.dumps({"draft_revision": draft.revision, "base_version": draft.base_version,
                          "status": "draft only; preview and apply through the admin UI"}))


if __name__ == "__main__":
    main()
