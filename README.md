# sediment

A personal knowledge base: messages, tickets and conversations from work systems settle into Qdrant, where AI agents pick them up over MCP.

Hence the name — **sediment**: everything that passes through work (chats, tickets, conversations with Claude) accumulates in raw markdown, gets chunked, embedded and stored in a vector DB. From there Claude Code / Desktop search it over MCP by semantics + keywords, so the accumulated context is not lost between sessions.

## Why

A typical problem: you discussed the architecture in Mattermost two months ago, the ticket lived in YouTrack, and the decision on it — in a Telegram thread. Recalling who said what and where is genuinely hard. sediment solves this: everything is merged into one searchable base, an MCP client asks "what did we decide about X?" and gets fragments from every source, ranked by relevance.

## How it works

```
                   raw-fetch (batch, via cron)
  ┌─────────────┐  ─────────────────────▶  ┌──────────────┐
  │ YouTrack    │                          │ vault/*.md   │
  │ Mattermost  │                          │ (markdown,   │
  │ + plugins   │                          │  sanitized)  │
  └─────────────┘                          └──────┬───────┘
                                                  │
                                                  │ sediment-load (800-char chunks, bge-m3 via llama.cpp)
                                                  ▼
                                           ┌──────────────┐
                                           │ Qdrant       │
                                           │ collections  │
                                           │ (per profile)│
                                           └──────┬───────┘
                                                  │ sediment-mcp (HTTP + Bearer)
                                                  ▼
                                           ┌──────────────┐
                                           │ Claude Code  │
                                           │ / Desktop    │
                                           │ (search +    │
                                           │  add_knowledge) │
                                           └──────────────┘
```

The pipeline is deliberately split in two stages:

1. **raw-fetch** sends nothing to Qdrant, it accumulates `.md` files in the vault. That makes debugging easier (diff the vault — see what arrived), chunks are cheap to rebuild, and secrets in markdown are stripped by `_sanitize()` before any indexing.
2. **sediment-load** is incremental: dedup by the file's `content_hash`, changed files are rebuilt, new ones added, unchanged ones skipped. A single run against an empty collection = a full reindex.

## Profiles

Configuration lives in `_profile.yaml` (locally) or in a ConfigMap (in k8s) and consists of two blocks. `collections:` declares the Qdrant collections and the sources loaded into each one (`{name: {sources: [...]}}`). `profiles:` describes the fetchers: every profile carries a `vault_path` (the raw directory is derived from it) plus per-source settings. A collection name matches the name of the profile it is fed by.

The point is context isolation: if you run several unrelated tracks (say, your own infra and a client project), their knowledge should not mix in the results. Every profile is fetched independently and searched as a separate MCP collection.

## Sources

Automatic `raw-fetch` runs keep progress per profile/source in
`<vault_path>/.fetch-state.sqlite3`. The first automatic run requires an explicit
`--initial-since YYYY-MM-DD`; choose the earliest date that must be collected.
Subsequent runs omit date flags and catch up in daily windows. Progress is saved
only after each successful fetch, with the boundary day replayed and a two-day
overlap retained. After an interrupted initialization, resume without
`--initial-since` for sources that already have a checkpoint; initialize the
remaining sources separately using `--profile` and `--source`.

`--since` selects a manual backfill (optionally with `--until`), which never changes
automatic progress. Source-specific options such as `--articles-only` or
`--projects` require this manual mode. A missing checkpoint is an error rather
than an assumed two-day history. Existing timers need a one-time initialization
before deploying this version. Store the progress database together with its raw
vault; restore them together. Concurrent writers to the same vault fail visibly
instead of silently racing their checkpoints.

Each automatic window logs its profile/source, dates, raw file count and last
success; an empty raw inventory produces a warning. These observations describe
local collection progress, not proof that an external source exposed all its
data. Discovery exclusions still apply; changing the configured scope requires
an explicit historical backfill for newly included data.

Built in are the sources that run unattended in a container: **YouTrack** and **Mattermost**, which need nothing but a URL and a token.

Mattermost channels can be listed by hand (`channels:`) or picked up automatically: `discover: {types: [O, P], active_within_days: 90}` fetches every channel the account has joined of those types — public, private, group DMs, direct — that is not dormant. Auto-added channels get a `<name>__<channel_id>` raw directory, so their `space` is readable straight off the path and they never need a config entry. `exclude: {ids: [], names: []}` bans a channel outright: it is never fetched, never indexed, and whatever it already put in Qdrant is purged on the next load — a ban overrides `channels` too.

A channel fetched for the first time lands in a `space` no ACL grant covers, so its content is invisible until someone grants it; the fetch prints those spaces at the end of the run.

Anything bound to one machine — an interactive login, a session file, transcripts that exist only on the laptop that wrote them — ships as a separate distribution and registers a `Source` under the `sediment.sources` entry-point group:

```toml
[project.entry-points."sediment.sources"]
telegram = "my_package.telegram:SOURCE"
```

A `Source` carries both halves of one convention: the fetcher that writes raw files, and the rule that reads ownership (the ACL `space`) back out of the paths it wrote — see `packages/sediment/src/sediment/sources/__init__.py`. Installing the plugin into the same environment as `sediment` is all it takes; `raw-fetch --help` then lists the new source. A plugin that fails to load is a fatal error rather than a skipped source, because the pipeline reports what it never looked for as "nothing new".

Source *names* are not part of that: `knowledge_schema.SOURCES` stays complete on every host, so a reader can filter documents produced by an importer it does not have installed.

## Packages

A uv workspace with three members:

- **`packages/sediment`** — the batch pipeline: CLI `raw-fetch` + `sediment-load`, plus payload-only migrations (`sediment-backfill-*`). Runs on cron in k8s, and on systemd timers locally and on a workstation.
- **`packages/sediment-mcp`** — the MCP server (Streamable HTTP + Bearer auth). Tools: `search(collection, query, keywords, source, filename, space, space_kind, doc_kind, since, until, limit)`, `get_document(collection, file, from_chunk)` to read a whole document search returned a chunk of, and `add_knowledge(collection, text, source, file, title)` for manual notes.
- **`packages/knowledge-schema`** — the shared contract: `SOURCES` (the list of valid `source` values for filtering), `embed(texts, embed_url, model, api_key=None)`, the chunk geometry (`CHUNK_SIZE`/`CHUNK_OVERLAP`) the loader splits by and the reader stitches back, and the `SPACE_KINDS`/`DOC_KINDS` vocabularies. Imported by both packages so that the writer and the reader never drift apart.

## Quickstart (dev)

For tested wheel bundles, installation without source checkouts, and the
artifact promotion process, see [Release bundles](docs/releases.md).

```bash
uv sync --all-packages
uv run --package sediment raw-fetch --config-dir . --profile <name> --source mattermost --since 2026-04-01 --until 2026-04-21
uv run --package sediment sediment-load --config-dir . --collection <name> --source mattermost
MCP_ACL_DISABLE=1 uv run --package sediment-mcp sediment-mcp-serve   # explicit allow-all, for local dev only
```

Secrets and endpoints go into the `.env` next to `_profile.yaml` (which is
pointed at via `--config-dir`). `QDRANT_URL` and `EMBED_URL` are mandatory; there
are no production URLs by default. The optional `QDRANT_API_KEY` is used by all
loader/backfill commands and by the MCP reader.

## Embeddings

Any OpenAI-compatible `/v1/embeddings` will do — your own llama.cpp (`llama-server --embedding` with bge-m3) or an external provider (OpenAI, Jina, DeepInfra, ...) if there is nowhere to host your own:

- `EMBED_URL` — the server base (`/v1/embeddings` is appended automatically; a URL that already ends with `/embeddings` is used as is — for non-standard prefixes such as Gemini's `/v1beta/openai/embeddings`);
- `EMBED_MODEL` — the model name at the provider;
- `EMBED_API_KEY` — the Bearer key, only for external providers.

The writer (`sediment-load`) and the reader (`sediment-mcp`) must use the same model — query and document vectors have to live in one space. Before starting, `sediment-load` makes a probe embedding call (validating the URL/model/key) and fails if the model's dimensionality does not match the existing collection; changing the model means `--rebuild --yes-really-rebuild` for every collection. The loader re-runs `sanitize()` over old raw files, preserves their mtime and reindexes the cleaned content. In k8s the key comes from the SOPS `secrets.yaml`: in the `sediment` chart it is enabled simply by having `EMBED_API_KEY` in the `secrets: {ENV_NAME: value}` map, in the `sediment-mcp` chart — via `embedApiKeyFromSecret: true` in values.

## Index compatibility and migration

Collections created by the loader carry a `sediment` metadata contract: schema
version, `EMBED_MODEL`, vector dimension, chunking version, size and overlap.
This requires [Qdrant 1.16 or newer](https://qdrant.tech/documentation/manage-data/collections/).
The loader checks every existing target before starting writes. MCP checks the
contract after authorization and before search, document reads and manual writes.
An unversioned or incompatible collection is rejected, including a different
model with the same vector dimension. `EMBED_MODEL` is mandatory for both sides.

Use a stable, revision-specific model name supported by your embedding service.
The contract checks the configured name, not the weights behind a mutable alias;
silently replacing weights under the same name cannot be detected by this check.
Increment the schema or chunking version when changing their semantics.

To migrate an existing installation:

1. Preserve the old collections and back up the complete raw vaults and manual
   records, including their ownership/visibility. Inventory every source and host;
   one workstation's raw directory is not necessarily a complete collection.
2. Create a new profile/collection name configured with the complete source set
   and the intended model. Run `sediment-load` for the new collection, collecting
   contributions from all required hosts under the same contract.
3. Re-embed and import manual records while preserving their ACL payloads.
   They have no raw-fetch copy: raw-only rebuilding does not restore them.
   There is no automated manual-record migration command yet.
4. Verify document/chunk counts, ownership, access and representative queries.
   Stop writers for a final catch-up, then switch clients and ACL grants to the new
   collection. Keep the old collection and matching application/model configuration
   available for rollback until the migration is accepted.

Do not stamp new metadata onto old vectors to bypass validation. The existing
`--rebuild --yes-really-rebuild` path remains destructive: it deletes the entire
collection, including manual records and sources absent from the current host.
It now accepts a changed vector dimension, but cannot be combined with `--source`.
Use it only for disposable or independently recoverable targets.

## Type checking and pre-commit

The typechecker is **basedpyright** (a pyright fork with stricter defaults and more active development). Manual run:

```bash
uvx basedpyright      # reads [tool.basedpyright] from the root pyproject.toml
```

Settings in `pyproject.toml`:

```toml
[tool.basedpyright]
include = ["packages"]
typeCheckingMode = "standard"   # basedpyright defaults to "all", which flags every Unknown/Any coming from third-party libs (Telethon, yaml) and produces hundreds of noisy warnings; "standard" disables the reportUnknown*/reportAny family
pythonVersion = "3.14"
```

Running it on commit is handled by `pre-commit` ([pre-commit.com](https://pre-commit.com)):

```bash
uvx pre-commit install       # creates .git/hooks/pre-commit, done once after clone
uvx pre-commit run --all     # manual run over the whole repo
```

`.pre-commit-config.yaml` points at **basedpyright-pre-commit-mirror** — a separate repo that is tagged automatically in sync with basedpyright releases and ships `.pre-commit-hooks.yaml` (basedpyright itself does not provide a pre-commit integration). This is the standard pattern for Python linters — `ruff-pre-commit`, `mypy-mirror` and others work exactly the same way.

How it works under the hood:

1. Pre-commit reads `.pre-commit-config.yaml` and clones the mirror at the given `rev:` into `~/.cache/pre-commit/`
2. It creates an isolated venv and installs `basedpyright==<rev>` there (the version pin lives in `rev:`, not in `pyproject.toml`)
3. On every `git commit` it runs `basedpyright` over the staged files
4. `basedpyright` finds `pyproject.toml` on its own and applies `[tool.basedpyright]`

Upgrading means editing `rev:` in `.pre-commit-config.yaml`; the venv is rebuilt on the next commit.

## Where it runs

A k8s CronJob + a k8s Deployment + systemd timers on workstations. This repository holds the code; CI publishes container images to `ghcr.io/sedimentapp/{sediment,sediment-mcp}`. Deployment manifests live outside it — an installation carries its own charts and values, since those are where the site-specific parts (hosts, collections, identities, secrets) belong.
