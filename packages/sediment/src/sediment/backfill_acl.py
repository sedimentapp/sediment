#!/usr/bin/env python3 -u
"""Backfill the ACL `space`/`space_name` payload fields onto existing Qdrant points.

Payload-only migration: vectors are untouched, no embedding server involved.
Idempotent — set_payload overwrites only the stamped keys, so re-running after
a partial failure converges. Run this to completion (clean report, exit 0)
BEFORE enabling ACL enforcement in sediment-mcp: points without `space` are
invisible under ACL (fail-closed).

Files that cannot be mapped (renamed channels, points from add_knowledge with
pipeline sources) are listed in full and fail the run — never skipped silently.
"""

import argparse
import time
from collections import defaultdict

from qdrant_client import QdrantClient
from qdrant_client.http.exceptions import UnexpectedResponse
from qdrant_client.models import Filter, IsEmptyCondition, PayloadField

from sediment._common import load_profile
from sediment.load import (
    _qdrant_call,
    ensure_payload_indexes,
    load_collections,
    qdrant_client,
    select_collections,
)
from sediment.spaces import SpaceDerivationError, SpaceResolver

SET_PAYLOAD_BATCH = 500


def scroll_all(client: QdrantClient, collection: str):
    """Yield (point_id, payload) for every point, payload limited to file/source/space."""
    offset = None
    while True:
        results, offset = _qdrant_call(
            "scroll", client.scroll,
            collection,
            limit=1000,
            offset=offset,
            with_payload=["file", "source", "space"],
            with_vectors=False,
        )
        for r in results:
            yield r.id, (r.payload or {})
        if offset is None:
            break


def _facet_spaces(client: QdrantClient, collection: str):
    """Facet over `space`. The keyword index build is waited on at creation, but on
    large collections it can still lag behind — bounded retry, then fail."""
    for attempt in range(6):
        try:
            return _qdrant_call(
                "facet", client.facet,
                collection,
                key="space",
                limit=1000,
                exact=True,
            )
        except UnexpectedResponse as e:
            if e.status_code == 400 and b"No appropriate index" in (e.content or b"") and attempt < 5:
                print("  space index still building, retrying facet in 10s...")
                time.sleep(10)
                continue
            raise
    raise RuntimeError("facet retry loop exhausted without result")


def backfill_collection(
    client: QdrantClient,
    collection: str,
    spaces: SpaceResolver,
    stamp_manual_org: bool,
) -> bool:
    """Stamp space/space_name; returns True when the collection is fully migrated."""
    print(f"\n=== {collection} ===")

    ids_by_space: dict[tuple[str, str], list] = defaultdict(list)
    manual_ids: list = []
    manual_examples: list[str] = []
    unmapped: dict[str, str] = {}  # rel_path -> reason (unique per file)
    derived_cache: dict[str, tuple[str, str] | SpaceDerivationError] = {}
    total = 0

    for point_id, payload in scroll_all(client, collection):
        total += 1
        source = payload.get("source")
        file = payload.get("file")
        # Manual review bucket: explicit manual entries, plus malformed points
        # lacking a usable source/file (not pipeline-written, cannot be derived).
        # Ones already carrying a space are migrated — nothing to review.
        if source == "manual" or not isinstance(source, str) or not isinstance(file, str) or not file:
            if not payload.get("space"):
                manual_ids.append(point_id)
                if len(manual_examples) < 50:
                    manual_examples.append(f"{point_id} source={source!r} file={file!r}")
            continue
        cached = derived_cache.get(file)
        if cached is None:
            try:
                cached = spaces.derive(source, file)
            except SpaceDerivationError as e:
                cached = e
            derived_cache[file] = cached
        if isinstance(cached, SpaceDerivationError):
            unmapped[file] = cached.reason
            continue
        ids_by_space[cached].append(point_id)

    print(f"Scanned {total} points: "
          f"{sum(len(v) for v in ids_by_space.values())} mappable, "
          f"{len(manual_ids)} manual, "
          f"{len(unmapped)} unmapped files")

    for (space, space_name), ids in sorted(ids_by_space.items()):
        for i in range(0, len(ids), SET_PAYLOAD_BATCH):
            _qdrant_call(
                "set_payload", client.set_payload,
                collection,
                payload={"space": space, "space_name": space_name},
                points=ids[i : i + SET_PAYLOAD_BATCH],
                wait=True,
            )
        print(f"  {space} ({space_name}): {len(ids)} points")

    if manual_ids and stamp_manual_org:
        for i in range(0, len(manual_ids), SET_PAYLOAD_BATCH):
            _qdrant_call(
                "set_payload", client.set_payload,
                collection,
                payload={"space": "manual:unknown", "visibility": "org", "author": "unknown"},
                points=manual_ids[i : i + SET_PAYLOAD_BATCH],
                wait=True,
            )
        print(f"  manual:unknown (visibility=org): {len(manual_ids)} points stamped")

    ensure_payload_indexes(client, collection)

    ok = True
    if unmapped:
        ok = False
        print(f"\n!!! UNMAPPED FILES in '{collection}': {len(unmapped)} (points left without space, invisible under ACL)")
        for file, reason in sorted(unmapped.items()):
            print(f"  {file}: {reason}")
    if manual_ids and not stamp_manual_org:
        ok = False
        print(f"\n!!! MANUAL POINTS in '{collection}': {len(manual_ids)} not stamped "
              f"(re-run with --stamp-manual-org to mark them org-visible, or fix by hand)")
        for line in manual_examples:
            print(f"  {line}")
        if len(manual_ids) > len(manual_examples):
            print(f"  ... and {len(manual_ids) - len(manual_examples)} more")

    remaining = _qdrant_call(
        "count", client.count,
        collection,
        count_filter=Filter(must=[IsEmptyCondition(is_empty=PayloadField(key="space"))]),
        exact=True,
    ).count
    print(f"\nPoints without space in '{collection}': {remaining}")

    facets = _facet_spaces(client, collection)
    print("Space distribution:")
    for hit in facets.hits:
        print(f"  {hit.value}: {hit.count}")

    return ok and remaining == 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill ACL space payload onto existing Qdrant points")
    parser.add_argument("--config-dir", required=True,
                        help="Directory with _profile.yaml providing the name->id maps space derivation needs")
    parser.add_argument("--collection", default="all")
    parser.add_argument("--stamp-manual-org", action="store_true",
                        help="Stamp manual points (source=manual or no file) as space=manual:unknown, visibility=org")
    args = parser.parse_args()

    config = load_profile(args.config_dir)
    targets = select_collections(load_collections(config), args.collection)

    # generous timeout: exact facet/count over the whole collection is slow
    # on the CPU-limited prod Qdrant
    client = qdrant_client(timeout=120)

    all_ok = True
    for collection in targets:
        spaces = SpaceResolver.from_profile(config.get("profiles", {}).get(collection) or {})
        all_ok &= backfill_collection(client, collection, spaces, args.stamp_manual_org)

    if not all_ok:
        raise SystemExit("Backfill incomplete — resolve the listed files/points and re-run")
    print("\nBackfill complete: all points carry a space.")


if __name__ == "__main__":
    main()
