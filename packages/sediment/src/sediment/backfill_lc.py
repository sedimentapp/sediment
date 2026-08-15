#!/usr/bin/env python3 -u
"""Backfill the `text_lc`/`file_lc` lowercase shadow payload fields onto existing points.

Payload-only migration: vectors are untouched, no embedding server involved.
Idempotent — points already carrying `text_lc` are skipped, so re-running after
a partial failure (or after new points arrive from a not-yet-updated loader)
converges. Run to completion BEFORE deploying the sediment-mcp reader that
filters on the shadows: points without them are invisible to the default
case-insensitive keyword/filename search.

Points lacking `text` entirely are reported and fail the run — never skipped
silently.
"""

import argparse

from qdrant_client import QdrantClient
from qdrant_client.models import SetPayload, SetPayloadOperation

from sediment._common import load_profile
from sediment.load import _qdrant_call, load_collections, qdrant_client, select_collections

SCROLL_BATCH = 500
UPDATE_OPS_BATCH = 200


def backfill_collection(client: QdrantClient, collection: str) -> bool:
    """Stamp text_lc/file_lc; returns True when the collection is fully migrated."""
    print(f"\n=== {collection} ===")

    total = 0
    stamped = 0
    already = 0
    broken: list[str] = []  # points without text — cannot derive a shadow
    ops: list[SetPayloadOperation] = []

    def flush() -> None:
        nonlocal ops
        if not ops:
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
            with_payload=["text", "file", "text_lc"],
            with_vectors=False,
        )
        for r in results:
            total += 1
            payload = r.payload or {}
            if payload.get("text_lc"):
                already += 1
                continue
            text = payload.get("text")
            if not isinstance(text, str) or not text:
                broken.append(f"{r.id} text={text!r}")
                continue
            shadow = {"text_lc": text.lower()}
            file = payload.get("file")
            if isinstance(file, str) and file:
                shadow["file_lc"] = file.lower()
            ops.append(SetPayloadOperation(set_payload=SetPayload(payload=shadow, points=[r.id])))
            stamped += 1
            if len(ops) >= UPDATE_OPS_BATCH:
                flush()
        flush()
        print(f"  scanned {total} (stamped {stamped}, already {already})")
        if offset is None:
            break

    print(f"Done: {total} points, {stamped} stamped, {already} already had text_lc")
    if broken:
        print(f"\n!!! POINTS WITHOUT text in '{collection}': {len(broken)} (no shadow written)")
        for line in broken[:50]:
            print(f"  {line}")
        if len(broken) > 50:
            print(f"  ... and {len(broken) - 50} more")
    return not broken


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill text_lc/file_lc lowercase shadow fields onto existing Qdrant points"
    )
    parser.add_argument("--collection", default="all")
    parser.add_argument("--config-dir", required=True,
                        help="Directory with _profile.yaml declaring the collections (and an optional .env)")
    args = parser.parse_args()

    config = load_profile(args.config_dir)  # also loads .env next to it
    targets = select_collections(load_collections(config), args.collection)
    client = qdrant_client(timeout=120)

    all_ok = True
    for collection in targets:
        all_ok &= backfill_collection(client, collection)

    if not all_ok:
        raise SystemExit("Backfill incomplete — resolve the listed points and re-run")
    print("\nBackfill complete: all points carry text_lc.")


if __name__ == "__main__":
    main()
