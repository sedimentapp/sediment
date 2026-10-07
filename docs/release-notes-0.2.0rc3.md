# Sediment 0.2.0rc3

This release candidate updates the MCP OAuth dependency and moves MCP image
publishing to verified version tags. Ingestion and access control behave as in rc2.

## MCP authentication

- fastmcp is raised to 3.4.8 (minimum `>=3.4.8,<4` for `sediment-mcp` and the
  Enterprise extensions). Earlier versions built the token endpoint from a base URL
  with a trailing slash as `//token`, so `private_key_jwt` assertions from clients
  registered via Client ID Metadata Documents (CIMD) failed audience checks.

## Distribution

The `core.tar.gz` release asset contains the ingestion packages `sediment` and
`knowledge-schema`; it does not install MCP or Enterprise extensions. The core
bundle format and ingestion behavior are unchanged from rc2.

MCP images are now published only from an existing verified version tag, as
`ghcr.io/sedimentapp/sediment-mcp-releases:<tag>`, after the tag check, tests,
Ruff and basedpyright pass. Pin the image digest for deployment. Enterprise
extensions retain their license requirements.
All workspace package versions in this candidate are `0.2.0rc3`.

No migration is required from rc2.

This remains a prerelease. The release workflow independently tests and builds
the core bundle from the version tag.
