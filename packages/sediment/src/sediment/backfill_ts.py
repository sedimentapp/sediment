#!/usr/bin/env python3 -u
"""Backfill the `ts` payload field (raw-file mtime, epoch seconds) onto existing points.

Payload-only migration: vectors are untouched, no embedding server involved.
Idempotent — points already carrying `ts` are skipped.

Raw files may live on different hosts — a k8s PVC holds the sources fetched by
the CronJob, while sources that need an interactive login or local files sit on
a workstation. So the backfill runs once per host: each run stamps the sources
whose raw dir exists under raw_dir and reports what is left for other hosts.
The migration is complete when a run on any host reports 0 points left.

Points of a locally-present source whose raw file is gone from disk are
reported and fail the run — never stamped with an invented timestamp.
Exception, opt-in via --orphan-ts-from-filename: orphaned points (raw file
missing everywhere — e.g. threads lost in a PVC re-create) whose filename
carries a `<rootid>.YYYY-MM-DDTHH-MM.md` stamp are stamped from that value —
it is the first-post time of the file's content, written by the fetcher.
Orphans without a stamp in the filename still fail the run.
`manual` points have no raw file and no recoverable creation time: they stay
without `ts` (readers must treat missing ts as "unknown"); new manual entries
are stamped by the server at creation time.
"""

import argparse
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from qdrant_client import QdrantClient
from qdrant_client.models import SetPayload, SetPayloadOperation

from sediment._common import load_profile
from sediment.load import (
    _qdrant_call,
    collect_files,
    load_collections,
    qdrant_client,
    resolve_raw_dir,
    select_collections,
)

SCROLL_BATCH = 500
UPDATE_OPS_BATCH = 200

# fetcher-written continuation-file stamp: <rootid>.2026-05-19T08-04.md
_FILENAME_STAMP_RE = re.compile(r"\.(\d{4}-\d{2}-\d{2}T\d{2}-\d{2})\.md$")


def filename_ts(file: str) -> int | None:
    """First-post time encoded in a continuation filename, or None.

    The stamp was written in the fetch host's local TZ; parsed here as UTC,
    so the recovered value can be off by a few hours — acceptable for
    freshness views, and the only surviving timestamp for orphaned files.
    """
    m = _FILENAME_STAMP_RE.search(file)
    if not m:
        return None
    return int(datetime.strptime(m.group(1), "%Y-%m-%dT%H-%M").replace(tzinfo=UTC).timestamp())


def backfill_collection(
    client: QdrantClient,
    collection: str,
    mtimes: dict[str, int],
    present_sources: set[str],
    orphan_ts_from_filename: bool = False,
) -> bool:
    """Stamp ts from mtimes for locally-present sources; True when nothing is broken.

    mtimes: {rel_path: mtime} for every raw file found under this host's raw_dir.
    present_sources: sources whose raw dir exists here — only their points are
    expected in mtimes; other sources are left for their own host's run.
    """
    print(f"\n=== {collection} (local sources: {', '.join(sorted(present_sources)) or 'none'}) ===")

    total = 0
    stamped = 0
    already = 0
    orphans = 0
    manual = 0
    other_hosts: Counter[str] = Counter()  # source -> points left for another host
    broken: list[str] = []  # local source but raw file missing, or no source/file at all
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
            with_payload=["file", "source", "ts"],
            with_vectors=False,
        )
        for r in results:
            total += 1
            payload = r.payload or {}
            if payload.get("ts"):
                already += 1
                continue
            source = payload.get("source")
            if source == "manual":
                manual += 1
                continue
            if not isinstance(source, str) or not source:
                broken.append(f"{r.id} source={source!r}")
                continue
            if source not in present_sources:
                other_hosts[source] += 1
                continue
            file = payload.get("file")
            ts = mtimes.get(file) if isinstance(file, str) else None
            if ts is None and orphan_ts_from_filename and isinstance(file, str):
                ts = filename_ts(file)
                if ts is not None:
                    orphans += 1
            if ts is None:
                broken.append(f"{r.id} file={file!r} (raw file not found)")
                continue
            ops.append(SetPayloadOperation(set_payload=SetPayload(payload={"ts": ts}, points=[r.id])))
            stamped += 1
            if len(ops) >= UPDATE_OPS_BATCH:
                flush()
        flush()
        print(f"  scanned {total} (stamped {stamped}, already {already})")
        if offset is None:
            break

    print(f"Done: {total} points, {stamped} stamped, {already} already had ts")
    if orphans:
        print(f"  {orphans} orphaned points stamped from the filename date (raw file gone)")
    if manual:
        print(f"  {manual} manual points left without ts (no recoverable creation time)")
    for source, count in sorted(other_hosts.items()):
        print(f"  {count} '{source}' points left for the host that has its raw files")
    if broken:
        print(f"\n!!! UNSTAMPABLE POINTS in '{collection}': {len(broken)}")
        for line in broken[:50]:
            print(f"  {line}")
        if len(broken) > 50:
            print(f"  ... and {len(broken) - 50} more")
    return not broken


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill ts (raw-file mtime) onto existing Qdrant points"
    )
    parser.add_argument("--collection", default="all")
    parser.add_argument("--config-dir", required=True,
                        help="Directory with _profile.yaml declaring the collections and vault paths")
    parser.add_argument(
        "--orphan-ts-from-filename", action="store_true",
        help="For points of a local source whose raw file is gone, recover ts "
             "from the <rootid>.YYYY-MM-DDTHH-MM.md filename stamp instead of failing",
    )
    parser.add_argument(
        "--sources", metavar="SRC[,SRC...]",
        help="Only treat these sources as local, even if other source dirs exist "
             "under raw_dir (guards against stale vault copies on the wrong host)",
    )
    args = parser.parse_args()

    config = load_profile(args.config_dir)  # also loads .env next to it
    collections = load_collections(config)

    only_sources = {s.strip() for s in args.sources.split(",") if s.strip()} if args.sources else None

    targets = select_collections(collections, args.collection)
    client = qdrant_client(timeout=120)

    all_ok = True
    for collection in targets:
        sources = collections[collection]["sources"]
        raw_dir = resolve_raw_dir(collection, args.config_dir)
        present = {s for s in sources if (raw_dir / s).is_dir()}
        if only_sources is not None:
            present &= only_sources
        mtimes = {
            rel: int(Path(full).stat().st_mtime)
            for _, rel, full in collect_files(sorted(present), raw_dir)
        }
        all_ok &= backfill_collection(
            client, collection, mtimes, present,
            orphan_ts_from_filename=args.orphan_ts_from_filename,
        )

    if not all_ok:
        raise SystemExit("Backfill incomplete — resolve the listed points and re-run")
    print("\nBackfill run complete for this host's sources.")


if __name__ == "__main__":
    main()
