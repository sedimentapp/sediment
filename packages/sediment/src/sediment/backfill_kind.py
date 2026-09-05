#!/usr/bin/env python3 -u
"""Backfill the `space_kind`/`doc_kind` payload fields onto existing points.

Payload-only migration: vectors are untouched, no embedding server involved, and
re-running is a no-op for points already carrying the right kinds.

Run it per vault host, like sediment-backfill-ts: the kinds come from the
installed source rules and from the sidecar each fetcher writes next to its raw
files, so a host stamps what it actually fetches. Points of a source with no
importer installed here are counted and left alone — the host that owns them
stamps them on its own run. Run the fetcher first: the sidecar a run has never
written holds no channel types yet.
"""

import argparse
from collections import Counter

from qdrant_client import QdrantClient
from qdrant_client.models import SetPayload, SetPayloadOperation

from sediment._common import load_profile
from sediment.kinds import KindResolver
from sediment.load import (
    _qdrant_call,
    load_collections,
    qdrant_client,
    resolve_raw_dir,
    select_collections,
)
from sediment.registry import available_sources

SCROLL_BATCH = 500
UPDATE_OPS_BATCH = 200

# add_knowledge writes these itself; older manual entries predate the fields.
MANUAL_KINDS = {"space_kind": "manual", "doc_kind": "note"}


def backfill_collection(
    client: QdrantClient, collection: str, config_dir: str | None, dry_run: bool = False
) -> None:
    print(f"\n=== {collection} ==={' (dry run)' if dry_run else ''}")
    kinds = KindResolver.from_raw_dir(resolve_raw_dir(collection, config_dir))
    installed = set(available_sources())

    total = 0
    stamped: Counter[str] = Counter()
    no_importer: Counter[str] = Counter()
    unlabelled: Counter[str] = Counter()
    ops: list[SetPayloadOperation] = []

    def flush() -> None:
        nonlocal ops
        if not ops or dry_run:
            ops = []
            return
        _qdrant_call(
            "batch_update_points", client.batch_update_points,
            collection,
            update_operations=ops,
            wait=True,
        )
        ops = []

    offset = None
    while True:
        results, offset = _qdrant_call(
            "scroll", client.scroll,
            collection,
            limit=SCROLL_BATCH,
            offset=offset,
            with_payload=["source", "file", "space", "space_kind", "doc_kind"],
            with_vectors=False,
        )
        for r in results:
            total += 1
            payload = r.payload or {}
            source = payload.get("source", "")
            if source == "manual":
                wanted = MANUAL_KINDS
            elif source in installed:
                wanted = {
                    "space_kind": kinds.space_kind(source, payload.get("space", "")),
                    "doc_kind": kinds.doc_kind(source, payload.get("file", "")),
                }
            else:
                no_importer[source] += 1
                continue

            update = {k: v for k, v in wanted.items() if v and payload.get(k) != v}
            for field in ("space_kind", "doc_kind"):
                if not wanted.get(field):
                    unlabelled[f"{source}.{field}"] += 1
            if update:
                ops.append(SetPayloadOperation(set_payload=SetPayload(payload=update, points=[r.id])))
                for field in update:
                    stamped[f"{source}.{field}"] += 1
                if len(ops) >= UPDATE_OPS_BATCH:
                    flush()
        flush()
        if offset is None:
            break

    print(f"  scanned {total} points")
    for key, n in sorted(stamped.items()):
        print(f"  stamped {key}: {n}")
    for key, n in sorted(unlabelled.items()):
        print(f"  no value available for {key}: {n}")
    for source, n in sorted(no_importer.items()):
        print(f"  skipped {n} points of {source!r}: no importer installed on this host")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill space_kind/doc_kind onto existing Qdrant points"
    )
    parser.add_argument("--collection", default="all")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would be stamped, write nothing")
    parser.add_argument("--config-dir", required=True,
                        help="Directory with _profile.yaml declaring the collections (and an optional .env)")
    args = parser.parse_args()

    config = load_profile(args.config_dir)  # also loads .env next to it
    targets = select_collections(load_collections(config), args.collection)
    client = qdrant_client(timeout=120)

    for collection in targets:
        backfill_collection(client, collection, args.config_dir, args.dry_run)


if __name__ == "__main__":
    main()
