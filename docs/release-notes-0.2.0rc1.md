# Sediment 0.2.0rc1

Release candidate. The core archive contains the tested sediment and
knowledge-schema wheels, hashed dependencies and a standalone installer.

- Interrupted document loads are detected and repaired on the next run.
- Fetch progress is persisted per profile/source and catches up daily after downtime.
- Collections carry a model/schema/chunking contract checked by writer and reader.
- Wheel bundles use a pinned build environment and are tested in a fresh venv.

## Required migration

Existing automatic fetch jobs need explicit first-time initialization with
`--initial-since YYYY-MM-DD`. Existing collections without an index contract are
rejected. Build a new collection from complete source data, migrate manual records
with their ownership, verify it, and coordinate the writer/reader switch.
`EMBED_MODEL` must be set explicitly. Collection metadata requires Qdrant >= 1.16.
See the index migration section in README before updating a running installation.

The bundle needs Python 3.14 and uv. Third-party dependencies are downloaded from
PyPI or cache with pinned versions and hashes. Preserve the previous venv and
collection for rollback. Private source plugins are distributed separately.
