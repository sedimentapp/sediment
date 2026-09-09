# Access control

Community reads a YAML ACL file once at startup. Enterprise managed access stores
users and permissions in SQLite and changes them through the admin UI. Both use
the same collection-scoped ACL evaluator; there is no file-to-database seed or
fallback between modes.

## Community: file configuration

Set `MCP_ACCESS_MODE=file` (the default) and `MCP_ACL_CONFIG` to a readable YAML
file. Existing `user_groups`, `space_groups` and `grants` syntax is unchanged:

```yaml
user_groups:
  readers: [alice]
space_groups:
  engineering: ["yt:INF"]
grants:
  - user_groups: [readers]
    collections: [acme]
    space_groups: [engineering]
```

Named static tokens authenticate the principals; unknown principals have no
access. Changes require a restart. Invalid/missing configuration fails startup.
Explicit `MCP_ACL_DISABLE=1` remains available in file mode, mutually exclusive
with a configured file, and allows all authenticated clients to read/write all
collections. It is not permitted in managed mode.

Grants are combined **within each collection**. A wildcard on one collection does
not grant access to other spaces in another collection. Space names are display
labels; stable IDs are used for authorization. Own manual notes and org-visible
manual entries retain their existing visibility semantics within allowed collections.

## Enterprise: managed users and permissions

Install `sediment-mcp-ee-access`, configure a valid EE license, and set:

```sh
MCP_ACCESS_MODE=database
MCP_ACCESS_DB=/data/acl/access.db
MCP_EXTENSIONS=webadmin
```

Configure the existing browser OAuth settings and `MCP_ADMIN_IDENTITIES` for
managing administrators. These administrators can configure an empty installation,
but receive no automatic MCP data grants. Do not set `MCP_ACL_CONFIG`,
`MCP_ACL_DISABLE`, `MCP_ACL_DB`, or `MCP_GITHUB_ALLOWED_IDENTITIES` in managed mode.
The browser authentication allowlist remains configuration; ordinary GitHub users
belong to the database.

A new database starts with no users and no data access. Its parent directory must
exist and be writable. For migrated/restored installations set
`MCP_ACCESS_REQUIRE_INITIALIZED=1`: a missing database then fails startup instead
of offering initial setup. Corrupt/incompatible databases always fail startup.

In `/admin/acl`, connect a GitHub account by looking up and confirming its numeric
ID, or register the principal of an operator-configured static token. Principals
and identity bindings are permanent; accounts are disabled rather than deleted.
GitHub renames do not transfer ownership. Static tokens stay in secret configuration
and are accepted only while their registered principal is active.

Forms save to one persistent shared draft. Review effective changes per user and
collection, then apply one complete version. Concurrent saves/applies return a
conflict instead of overwriting another administrator's work. The seat limit is
checked transactionally before activation, counting active users and managing
administrators by distinct principal. Disabled users lose access on their next MCP
call, including calls with already-issued tokens. An in-flight call uses its
existing snapshot; it is not cancelled retroactively.

Every call reads the current database version; storage failures block the call.
No per-process hot-swap or stale-permission fallback is used. The supported
deployment is one pod with local SQLite storage; this is not a multi-node database.

History records actor/time and the complete version. Restore creates a draft;
users registered after the restored version are retained, disabled. Export returns
a versioned YAML document of users and permissions without credentials. Import
validates the document and creates a draft, never immediate activation. Account
and group changes cannot remove an already registered identity or change its ID.

## Explicit migration from the legacy SQLite ACL

Stop ACL editing and make a consistent backup before migrating. Keep the original
database and application state. The migration reads the latest `acl_versions`
entry, never a potentially stale YAML seed. The original history remains intact
in the legacy database; it is not presented as reconstructed identity history.

Prepare an explicit identity inventory from the existing authentication config:

```yaml
github_identities:
  "10001": alice
  "10002": bob
static_principals: [alice, automation]
```

No token values belong in this file. Duplicate identity bindings or policy
principals missing from the inventory are errors requiring operator resolution.
GitHub login display labels are unknown after migration and remain null; the
numeric IDs and stable principals are preserved without guessing current logins.

```sh
sediment-mcp-access migrate --legacy-db /data/acl/acl.db \
  --identities /secure/identities.yaml --report /secure/access-report.json
```

Review any permission narrowing caused by fixing cross-collection wildcard/space
leakage. Then repeat with a new report filename and `--output-db /data/acl/access.db`.
If the report contains scope changes, explicitly add `--accept-scope-changes` after
review. Migration validates the current license before writing the new database;
it refuses to overwrite existing output. Set the managed environment variables
and remove the old seed and identity variables together with the new image.
No Qdrant mutation or re-embedding is required.

## Backup and recovery

Back up the SQLite database consistently (SQLite backup API or a stopped process),
including users, active versions, draft and identity bindings. A YAML export is
portable active configuration, not a full backup of history/drafts. OAuth state,
admin login configuration and secrets still require their own existing backups.

`sediment-mcp-access export --db PATH --output FILE` exports the active document.
`sediment-mcp-access import --db PATH --document FILE --author NAME
--draft-revision N --base-version N` imports into the shared draft with explicit
concurrency checks. Preview/apply through the UI.

To revert the deployment, stop the new process and use the retained legacy database
with its matching old image/configuration; never point the old image at the new
schema. New changes made after migration are not reflected in the old database.
