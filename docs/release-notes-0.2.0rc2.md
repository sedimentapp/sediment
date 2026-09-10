# Sediment 0.2.0rc2

This release candidate separates Community file ACLs from Enterprise managed
access and fixes permission leakage between collections.

## Access control

- Community retains the existing YAML format (`user_groups`, `space_groups`,
  `grants`). Configuration is validated at startup; changes require a restart.
- Enterprise stores users and permissions together in versioned SQLite state.
  Administrators edit a persistent shared draft, preview effective permissions,
  and activate the complete version atomically. Concurrent edits return conflicts.
- GitHub accounts use permanent numeric identity bindings. Disabled accounts lose
  access on their next call, including existing OAuth and static-token sessions.
- Full access to one collection no longer removes space restrictions in another.
- Managed access fails closed on configuration, license, or storage errors.

## Upgrade requirements

Existing Enterprise SQLite installations require an explicit migration before
starting the new MCP image. Back up application state, migrate the active legacy
policy and identity inventory, review the report, then switch configuration and
image together. Automatic YAML-to-SQLite seeding has been removed. Keep the old
database and matching image for recovery. See [Access control](access-control.md)
for commands and configuration, including single-pod SQLite requirements.

The permission fix can intentionally narrow access previously widened across
collections. Review the migration report. Qdrant data and embeddings do not need
to change.

## Distribution

The `core.tar.gz` release asset contains the ingestion packages `sediment` and
`knowledge-schema`; it does not install MCP or Enterprise extensions. The core
bundle format and ingestion behavior are unchanged from rc1.

MCP is distributed separately as `ghcr.io/sedimentapp/sediment-mcp:sha-<commit>`,
built from the release commit by the MCP image workflow. Pin its immutable digest
for deployment. Enterprise extensions retain their license requirements.
All workspace package versions in this candidate are `0.2.0rc2`.

This remains a prerelease. The release workflow independently tests and builds
the core bundle from the version tag.
