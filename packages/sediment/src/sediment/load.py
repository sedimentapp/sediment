#!/usr/bin/env python3 -u
"""Load raw knowledge files into Qdrant via OpenAI-compatible embedding server."""

import hashlib
import os
import re
import threading
import time
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, TypedDict

import httpx
import yaml
from knowledge_schema import SOURCES
from knowledge_schema import embed as _embed
from qdrant_client import QdrantClient
from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    PayloadSchemaType,
    PointStruct,
    VectorParams,
)

from sediment._common import load_profile, sanitize
from sediment.spaces import SpaceDerivationError, SpaceResolver

CHUNK_SIZE = 800  # chars
CHUNK_OVERLAP = 100
BATCH_SIZE = 64


def require_qdrant_url() -> str:
    value = os.environ.get("QDRANT_URL")
    if not value:
        raise RuntimeError("QDRANT_URL environment variable is required")
    return value


def qdrant_client(timeout: int | None = None) -> QdrantClient:
    kwargs: dict[str, Any] = {
        "url": require_qdrant_url(),
        "api_key": os.environ.get("QDRANT_API_KEY"),
    }
    if timeout is not None:
        kwargs["timeout"] = timeout
    return QdrantClient(**kwargs)


def embedding_config() -> tuple[str, str]:
    url = os.environ.get("EMBED_URL")
    if not url:
        raise RuntimeError("EMBED_URL environment variable is required")
    return url, os.environ.get("EMBED_MODEL", "bge-m3")


def _err_detail(e: BaseException) -> str:
    if isinstance(e, urllib.error.HTTPError):
        body = e.read().decode("utf-8", errors="replace")[:500] if e.fp else ""
        return f"status={e.code} body={body!r}"
    if isinstance(e, urllib.error.URLError):
        return f"reason={e.reason!r}"
    return repr(e)


def _qdrant_call(op: str, fn, *args, **kwargs):
    """Wrap a Qdrant client call with exponential-backoff retry.

    Retries transient failures: 5xx UnexpectedResponse, httpx.HTTPError,
    and ResponseHandlingException (transport-level disconnects wrapped by qdrant-client).
    4xx UnexpectedResponse is not retried — payload/config bug, retry won't help.
    """
    for attempt in range(5):
        try:
            return fn(*args, **kwargs)
        except UnexpectedResponse as e:
            if e.status_code and e.status_code < 500:
                raise
            if attempt == 4:
                raise
            wait = 2 ** attempt * 5
            print(f"  {op} failed (HTTP {e.status_code}), retry in {wait}s...")
            time.sleep(wait)
        except (httpx.HTTPError, ResponseHandlingException) as e:
            if attempt == 4:
                raise
            wait = 2 ** attempt * 5
            print(f"  {op} failed ({type(e).__name__}: {e}), retry in {wait}s...")
            time.sleep(wait)
    raise RuntimeError(f"{op}: retry loop exhausted without raising or returning")

class CollectionCfg(TypedDict):
    sources: list[str]


def raw_dir_env_name(collection: str) -> str:
    """Env var that overrides a collection's raw dir: RAW_DIR_<COLLECTION>."""
    return "RAW_DIR_" + re.sub(r"[^A-Z0-9]", "_", collection.upper())


def load_collections(config: dict[str, Any]) -> dict[str, CollectionCfg]:
    """Collections declared in _profile.yaml, validated against the source contract.

    A collection lists every source that may appear in its vault across all
    fetch hosts — not just the ones the current host fetches (raw dirs of the
    others are simply absent here and skipped).
    """
    declared = config.get("collections")
    if not isinstance(declared, dict) or not declared:
        raise RuntimeError(
            "_profile.yaml has no 'collections' block — declare the collections to "
            "load, e.g. 'collections: {mycollection: {sources: [claude]}}'"
        )

    collections: dict[str, CollectionCfg] = {}
    for name, cfg in declared.items():
        sources = (cfg or {}).get("sources")
        if not isinstance(sources, list) or not sources:
            raise RuntimeError(
                f"collection '{name}': 'sources' must be a non-empty list of "
                f"known sources ({', '.join(SOURCES)})"
            )
        unknown = [s for s in sources if s not in SOURCES]
        if unknown:
            raise RuntimeError(
                f"collection '{name}': unknown source(s) {', '.join(unknown)} — "
                f"known sources are {', '.join(SOURCES)}"
            )
        collections[name] = CollectionCfg(sources=list(sources))
    return collections


def select_collections(collections: dict[str, CollectionCfg], requested: str) -> list[str]:
    """Resolve a --collection argument against the configured collections."""
    if requested == "all":
        return list(collections)
    if requested not in collections:
        raise SystemExit(
            f"unknown collection '{requested}' — configured collections are "
            f"{', '.join(collections) or '(none)'}"
        )
    return [requested]


def resolve_raw_dir(collection: str, config_dir: str | None) -> Path:
    """Resolve raw_dir: env var (explicit override) takes precedence, then profile vault_path."""
    env_name = raw_dir_env_name(collection)
    env_value = os.environ.get(env_name)
    if env_value:
        return Path(env_value).expanduser()

    if not config_dir:
        raise RuntimeError(
            f"Cannot resolve raw_dir for '{collection}': "
            f"set env var {env_name} or pass --config-dir"
        )

    profile_path = Path(config_dir) / "_profile.yaml"
    if not profile_path.exists():
        raise FileNotFoundError(f"Profile not found: {profile_path}")
    with open(profile_path) as f:
        config = yaml.safe_load(f)

    profile = config.get("profiles", {}).get(collection)
    if not profile or not profile.get("vault_path"):
        raise RuntimeError(
            f"Profile '{collection}' missing 'vault_path' in {profile_path}"
        )
    return Path(profile["vault_path"]).expanduser() / "raw"


def embed(texts: list[str]) -> list[list[float]]:
    embed_url, embed_model = embedding_config()
    return _embed(texts, embed_url, embed_model, api_key=os.environ.get("EMBED_API_KEY"))


def _split_oversized(paragraphs: list[str]) -> list[str]:
    # Without this a single oversized paragraph (log dump, serialized JSON) would land in one chunk
    # and trip the embedding server's n_ctx (bge-m3: 8192 tokens).
    out: list[str] = []
    step = CHUNK_SIZE - CHUNK_OVERLAP
    for p in paragraphs:
        if len(p) <= CHUNK_SIZE:
            out.append(p)
            continue
        for i in range(0, len(p), step):
            piece = p[i : i + CHUNK_SIZE]
            if piece:
                out.append(piece)
    return out


def chunk_text(text: str, source: str, filepath: str) -> list[dict[str, Any]]:
    """Split text into overlapping chunks with metadata."""
    lines = text.split("\n")
    title = ""
    for line in lines:
        if line.startswith("# "):
            title = line[2:].strip()
            break

    paragraphs = _split_oversized([p.strip() for p in text.split("\n\n") if p.strip()])
    chunks = []
    current = ""

    for para in paragraphs:
        if len(current) + len(para) + 2 > CHUNK_SIZE and current:
            chunks.append(current)
            if CHUNK_OVERLAP and len(current) > CHUNK_OVERLAP:
                current = current[-CHUNK_OVERLAP:] + "\n\n" + para
            else:
                current = para
        else:
            current = current + "\n\n" + para if current else para

    if current:
        chunks.append(current)

    if not chunks:
        chunks = [text]

    filename = Path(filepath).stem
    return [
        {
            "text": chunk,
            "source": source,
            "file": filepath,
            "filename": filename,
            "title": title,
            "chunk_index": i,
        }
        for i, chunk in enumerate(chunks)
        if len(chunk.strip()) > 30
    ]


def point_id(file_path: str, chunk_index: int) -> str:
    """Deterministic UUID-like ID from file path + chunk index."""
    h = hashlib.md5(f"{file_path}:{chunk_index}".encode()).hexdigest()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def content_hash(text: str) -> str:
    """Hash of file content for change detection."""
    return hashlib.md5(text.encode()).hexdigest()


def collect_files(sources: list[str], raw_dir: Path) -> list[tuple[str, str, str]]:
    """Collect all raw files for given sources: (source, rel_path, full_path)."""
    files = []
    for source in sources:
        source_dir = raw_dir / source
        if not source_dir.exists():
            continue
        for f in source_dir.rglob("*.md"):
            rel = str(f.relative_to(raw_dir))
            files.append((source, rel, str(f)))
    return files


def get_indexed_files(client: QdrantClient, collection: str) -> dict[str, str]:
    """Get {file_path: content_hash} for all files already in Qdrant.

    Files without content_hash (loaded before incremental support)
    get a sentinel value so they're treated as "present but unknown hash",
    meaning they'll be skipped unless --rebuild is used.
    """
    indexed = {}
    offset = None
    while True:
        results, offset = _qdrant_call(
            "scroll", client.scroll,
            collection,
            limit=1000,
            offset=offset,
            with_payload=["file", "content_hash"],
        )
        for r in results:
            if r.payload is None:
                continue
            f = r.payload.get("file", "")
            if not f:
                continue
            h = r.payload.get("content_hash", "")
            if f not in indexed or h:  # prefer entry with actual hash
                indexed[f] = h
        if offset is None:
            break
    return indexed


def ensure_payload_indexes(client: QdrantClient, collection: str) -> None:
    """Create payload indexes: keyword for ACL filtering, integer on ts for
    range filters and freshness views; idempotent, built online by Qdrant.

    No full-text index on text/file on purpose: MatchText on unindexed fields is
    substring matching, which the search tool's keywords/filename params rely on;
    a full-text index would silently switch them to token-based matching.
    """
    for field, schema in (
        ("space", PayloadSchemaType.KEYWORD),
        ("source", PayloadSchemaType.KEYWORD),
        ("ts", PayloadSchemaType.INTEGER),
    ):
        _qdrant_call(
            f"create_payload_index({field})", client.create_payload_index,
            collection,
            field_name=field,
            field_schema=schema,
            wait=True,
        )


def load_collection(
    collection: str,
    sources: list[str],
    raw_dir: Path,
    client: QdrantClient,
    dim: int,
    rebuild: bool,
    spaces: SpaceResolver,
    workers: int = 1,
) -> list[SpaceDerivationError]:
    """Load files from given sources into a Qdrant collection.

    Returns derivation failures: those files are skipped (fail-closed — a point
    without space would be permanently invisible under ACL and the content_hash
    increment would never revisit it) and must be reported by the caller.
    """
    print(f"\n=== {collection} ({', '.join(sources)}) raw_dir={raw_dir} ===")

    files = collect_files(sources, raw_dir)
    print(f"Found {len(files)} raw files")

    if _qdrant_call("collection_exists", client.collection_exists, collection):
        if rebuild:
            _qdrant_call("delete_collection", client.delete_collection, collection)
            _qdrant_call(
                "create_collection", client.create_collection,
                collection,
                vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
            )
            print(f"Recreated collection '{collection}'")
        else:
            info = _qdrant_call("get_collection", client.get_collection, collection)
            print(f"Collection '{collection}' exists: {info.points_count} points")
    else:
        _qdrant_call(
            "create_collection", client.create_collection,
            collection,
            vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
        )
        print(f"Created collection '{collection}'")

    ensure_payload_indexes(client, collection)

    # Get already indexed files for incremental mode
    if not rebuild:
        print("Scanning indexed files...")
        indexed = get_indexed_files(client, collection)
        print(f"  {len(indexed)} files already indexed")
    else:
        indexed = {}

    # Chunk only new/changed files
    all_chunks = []
    skipped_files = 0
    changed_files = []
    unmapped: list[SpaceDerivationError] = []
    for source, rel_path, full_path in files:
        path = Path(full_path)
        original_stat = path.stat()
        raw_text = path.read_text()
        text = sanitize(raw_text)
        if text != raw_text:
            path.write_text(text)
            os.utime(
                path,
                ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
            )
            print(f"  Sanitized secrets in raw file: {rel_path}")
        h = content_hash(text)
        # mtime, not load time: fetchers only rewrite files that got new
        # content, so this reflects content freshness and survives --rebuild
        ts = int(original_stat.st_mtime)
        old_hash = indexed.get(rel_path)
        if old_hash is not None and (old_hash == "" or old_hash == h):
            skipped_files += 1
            continue
        # Derive before touching changed_files: a file we cannot map must not
        # get its old points deleted, and must never be indexed without a space.
        try:
            space, space_name = spaces.derive(source, rel_path)
        except SpaceDerivationError as e:
            unmapped.append(e)
            continue
        if old_hash is not None:
            changed_files.append(rel_path)
        chunks = chunk_text(text, source, rel_path)
        for c in chunks:
            c["content_hash"] = h
            c["space"] = space
            c["space_name"] = space_name
            c["ts"] = ts
        all_chunks.extend(chunks)

    print(f"Skipped {skipped_files} unchanged files")
    if unmapped:
        print(f"\n!!! UNMAPPED FILES (not indexed) in '{collection}': {len(unmapped)}")
        for e in unmapped:
            print(f"  {e.rel_path}: {e.reason}")
    if changed_files:
        print(f"Changed files: {len(changed_files)}")
        for f in changed_files:
            _qdrant_call(
                "delete", client.delete,
                collection,
                points_selector=Filter(must=[FieldCondition(key="file", match=MatchValue(value=f))]),
            )
        print("  Deleted old chunks for changed files")

    if not all_chunks:
        print("Nothing new to load.")
        info = _qdrant_call("get_collection", client.get_collection, collection)
        print(f"Collection '{collection}': {info.points_count} points")
        return unmapped

    files_with_chunks = len({c["file"] for c in all_chunks})
    print(f"Loading {len(all_chunks)} new chunks from {files_with_chunks} files")

    # Embed and upsert decoupled across a thread pool: while one worker waits on
    # a Qdrant upsert (network) another keeps the embedding server busy (GPU),
    # and concurrent embed requests let that server batch them. workers=1 is the
    # plain sequential path. Order is irrelevant — point ids are deterministic
    # and upserts are idempotent.
    t0 = time.time()
    total = len(all_chunks)
    incomplete_files: set[str] = set()
    batches = [all_chunks[i : i + BATCH_SIZE] for i in range(0, total, BATCH_SIZE)]
    lock = threading.Lock()
    done = 0

    def embed_with_salvage(batch: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[list[float]]]:
        try:
            return batch, embed([c["text"] for c in batch])
        except (urllib.error.URLError, TimeoutError, KeyError) as e:
            # Batch failed (network, timeout, or malformed response). Retry each chunk individually
            # to salvage as many as possible; transient failures schedule the file for retry next run,
            # 4xx responses are logged and left in place (retry won't help).
            print(f"  Batch error ({type(e).__name__}: {_err_detail(e)}), retrying individually...")
            embeddings: list[list[float]] = []
            kept: list[dict[str, Any]] = []
            for c in batch:
                try:
                    embeddings.append(embed([c["text"]])[0])
                    kept.append(c)
                except (urllib.error.URLError, TimeoutError, KeyError) as ce:
                    print(f"    Skipped chunk ({type(ce).__name__}): {c['file']}#{c['chunk_index']} ({len(c['text'])} chars) {_err_detail(ce)}")
                    if not (isinstance(ce, urllib.error.HTTPError) and ce.code < 500):
                        with lock:
                            incomplete_files.add(c["file"])
            return kept, embeddings

    def process_batch(batch: list[dict[str, Any]]) -> None:
        nonlocal done
        batch, embeddings = embed_with_salvage(batch)
        points = [
            PointStruct(
                id=point_id(c["file"], c["chunk_index"]),
                vector=emb,
                payload={
                    "text": c["text"],
                    # lowercase shadows: Qdrant MatchText on unindexed fields is
                    # case-sensitive substring; the search tool's default
                    # case-insensitive mode filters on these
                    "text_lc": c["text"].lower(),
                    "file_lc": c["file"].lower(),
                    "source": c["source"],
                    "file": c["file"],
                    "filename": c["filename"],
                    "title": c["title"],
                    "chunk_index": c["chunk_index"],
                    "content_hash": c["content_hash"],
                    "space": c["space"],
                    "space_name": c["space_name"],
                    "ts": c["ts"],
                },
            )
            for c, emb in zip(batch, embeddings)
        ]
        if points:
            _qdrant_call("upsert", client.upsert, collection, points)
        with lock:
            done += len(batch)
            elapsed = time.time() - t0
            rate = done / elapsed if elapsed > 0 else 0
            eta = (total - done) / rate if rate > 0 else 0
            print(f"  {done}/{total} chunks ({rate:.0f}/s, ETA {eta:.0f}s)")

    if workers > 1:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            # iterating re-raises whatever a worker raised, so a hard failure
            # still aborts the run instead of being swallowed
            for _ in pool.map(process_batch, batches):
                pass
    else:
        for batch in batches:
            process_batch(batch)

    # Remove partially loaded files so next run retries them
    if incomplete_files:
        print(f"\nRemoving {len(incomplete_files)} incomplete files for retry on next run:")
        for f in incomplete_files:
            print(f"  {f}")
            _qdrant_call(
                "delete", client.delete,
                collection,
                points_selector=Filter(must=[FieldCondition(key="file", match=MatchValue(value=f))]),
            )

    elapsed = time.time() - t0
    info = _qdrant_call("get_collection", client.get_collection, collection)
    print(f"\nDone in {elapsed:.1f}s")
    print(f"Collection '{collection}': {info.points_count} points, {dim}d vectors")
    return unmapped


def main(argv: list[str] | None = None):
    import argparse
    parser = argparse.ArgumentParser(description="Load raw knowledge files into Qdrant")
    parser.add_argument("--collection", default="all",
                        help="Which collection to load, or 'all' (default: all)")
    parser.add_argument("--rebuild", action="store_true", help="Drop and recreate collection")
    parser.add_argument(
        "--yes-really-rebuild",
        action="store_true",
        help="Required confirmation for --rebuild",
    )
    parser.add_argument("--source", help="Load only this source (e.g. youtrack, mattermost)")
    parser.add_argument(
        "--workers", type=int, default=1, metavar="N",
        help="Embed/upsert batches in N parallel workers (default 1 = sequential). "
             "Helps when the embedding server can batch concurrent requests; "
             "it also overlaps embedding with Qdrant upserts.",
    )
    parser.add_argument("--config-dir", required=True,
                        help="Directory with _profile.yaml declaring the collections to load")
    args = parser.parse_args(argv)

    if args.workers < 1:
        parser.error("--workers must be >= 1")
    if args.rebuild and not args.yes_really_rebuild:
        parser.error("--rebuild requires --yes-really-rebuild")
    if args.yes_really_rebuild and not args.rebuild:
        parser.error("--yes-really-rebuild is only valid with --rebuild")

    config = load_profile(args.config_dir)  # also loads .env next to it
    collections = load_collections(config)

    qdrant_url = require_qdrant_url()
    embed_url, embed_model = embedding_config()
    print(f"Qdrant endpoint: {qdrant_url}")
    client = qdrant_client()

    targets = {name: collections[name] for name in select_collections(collections, args.collection)}

    # Per-collection derivation contexts (e.g. mattermost's name->id map); a
    # source whose rule needs config the profile lacks fails fast on first use.
    space_resolvers: dict[str, SpaceResolver] = {
        collection: SpaceResolver.from_profile(config.get("profiles", {}).get(collection) or {})
        for collection in targets
    }

    # Preflight: one real embedding call validates URL, model name and API key in
    # a single round-trip (external providers expose no llama.cpp-style /health).
    # Runs after load_profile so an EMBED_API_KEY from --config-dir/.env is seen.
    try:
        probe_dim = len(embed(["ping"])[0])
    except (urllib.error.URLError, TimeoutError) as e:
        raise RuntimeError(
            f"Embedding preflight failed (url={embed_url}, model={embed_model}): {e}"
        )

    # Prefer an existing target collection's vector size when creating the rest,
    # and fail fast if the configured model no longer matches it.
    dim = None
    for collection in targets:
        if _qdrant_call("collection_exists", client.collection_exists, collection):
            info = _qdrant_call("get_collection", client.get_collection, collection)
            vectors = info.config.params.vectors
            if isinstance(vectors, VectorParams):  # single unnamed vector, as we create them
                dim = vectors.size
                break
    if dim is None:
        dim = probe_dim
    elif dim != probe_dim:
        raise RuntimeError(
            f"Embedding dimension mismatch: existing collection has {dim}, model "
            f"{embed_model!r} at {embed_url} returns {probe_dim}. Fix EMBED_URL/"
            "EMBED_MODEL or --rebuild the collections with the new model."
        )
    print(f"Embedding dimension: {dim}")

    total_unmapped = 0
    for collection, cfg in targets.items():
        sources = [args.source] if args.source else cfg["sources"]
        raw_dir = resolve_raw_dir(collection, args.config_dir)
        unmapped = load_collection(
            collection, sources, raw_dir, client, dim, args.rebuild,
            space_resolvers[collection], workers=args.workers,
        )
        total_unmapped += len(unmapped)

    if total_unmapped:
        raise SystemExit(
            f"{total_unmapped} file(s) skipped without a derivable space (see UNMAPPED FILES above); "
            "fix the profile config and re-run"
        )


if __name__ == "__main__":
    main()
