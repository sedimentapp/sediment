"""Read-only Qdrant queries behind the admin pages.

All functions take the shared QdrantClient (sync) and are called from the
async handlers via a worker thread — the admin UI must not block the MCP
event loop of the same process.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from qdrant_client import QdrantClient
from qdrant_client.models import Direction, FieldCondition, Filter, MatchValue, OrderBy

# Above this many distinct spaces the per-space freshness queries (one scroll
# each) get slow; current inventories are well under it.
FACET_LIMIT = 5000


@dataclass(frozen=True)
class CollectionOverview:
    name: str
    status: str
    points_count: int
    by_source: dict[str, int]


@dataclass(frozen=True)
class SpaceRow:
    space: str
    name: str | None
    source: str | None
    points: int
    last_ts: int | None  # None = no point in the space carries ts ("unknown")


@dataclass(frozen=True)
class SpacesReport:
    collection: str
    points_count: int
    rows: list[SpaceRow]
    unspaced: int  # points without a `space` payload — invisible under ACL
    ts_indexed: bool  # order_by needs the ts range index (created by sediment-load)


def collections_overview(client: QdrantClient) -> list[CollectionOverview]:
    out = []
    for desc in sorted(client.get_collections().collections, key=lambda c: c.name):
        info = client.get_collection(desc.name)
        facet = client.facet(desc.name, key="source", limit=FACET_LIMIT, exact=True)
        by_source = {str(h.value): h.count for h in facet.hits}
        out.append(
            CollectionOverview(
                name=desc.name,
                status=info.status.value,
                points_count=info.points_count or 0,
                by_source=dict(sorted(by_source.items())),
            )
        )
    return out


def _space_filter(space: str) -> Filter:
    return Filter(must=[FieldCondition(key="space", match=MatchValue(value=space))])


@dataclass(frozen=True)
class ManualEntry:
    collection: str
    point_id: str
    author: str | None
    visibility: str | None
    file: str | None
    title: str | None
    preview: str
    ts: int | None


PREVIEW_LEN = 200


class ManualDeleteError(Exception):
    """A requested point is absent or is not a manual entry."""


def manual_entries(client: QdrantClient) -> list[ManualEntry]:
    """Every source=manual point across all collections, newest first."""
    entries = []
    for desc in client.get_collections().collections:
        offset = None
        while True:
            points, offset = client.scroll(
                desc.name,
                scroll_filter=Filter(
                    must=[FieldCondition(key="source", match=MatchValue(value="manual"))]
                ),
                limit=100,
                offset=offset,
                with_payload=["author", "visibility", "file", "title", "text", "ts"],
            )
            for p in points:
                payload = p.payload or {}
                text = payload.get("text") or ""
                entries.append(
                    ManualEntry(
                        collection=desc.name,
                        point_id=str(p.id),
                        author=payload.get("author"),
                        visibility=payload.get("visibility"),
                        file=payload.get("file"),
                        title=payload.get("title"),
                        preview=text[:PREVIEW_LEN] + ("…" if len(text) > PREVIEW_LEN else ""),
                        ts=payload.get("ts"),
                    )
                )
            if offset is None:
                break
    entries.sort(key=lambda e: -(e.ts or 0))
    return entries


def delete_manual_point(client: QdrantClient, collection: str, point_id: str) -> None:
    points = client.retrieve(
        collection,
        ids=[point_id],
        with_payload=["source"],
        with_vectors=False,
    )
    if len(points) != 1 or (points[0].payload or {}).get("source") != "manual":
        raise ManualDeleteError("Point does not exist or is not a manual entry")
    client.delete(collection, points_selector=[point_id], wait=True)


def space_names(client: QdrantClient, spaces: set[str]) -> dict[str, str]:
    """Human-readable space_name for each space that has points anywhere.

    Display-only (never enforcement): spaces without points in any collection
    are simply absent from the result — the caller shows the raw id.
    """
    collections = [c.name for c in client.get_collections().collections]

    def lookup(space: str) -> tuple[str, str] | None:
        for collection in collections:
            points, _ = client.scroll(
                collection,
                scroll_filter=_space_filter(space),
                limit=1,
                with_payload=["space_name"],
            )
            if points:
                name = (points[0].payload or {}).get("space_name")
                if name:
                    return space, str(name)
        return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        return dict(filter(None, pool.map(lookup, sorted(spaces))))


def spaces_inventory(client: QdrantClient, collection: str) -> SpacesReport:
    info = client.get_collection(collection)
    points_count = info.points_count or 0
    # freshness (order_by ts) needs the range index sediment-load creates in
    # ensure_payload_indexes; without it show the inventory with ts unknown
    # instead of failing the whole page
    ts_indexed = "ts" in (info.payload_schema or {})
    facet = client.facet(collection, key="space", limit=FACET_LIMIT, exact=True)

    def row(hit) -> SpaceRow:
        space = str(hit.value)
        newest = []
        if ts_indexed:
            # newest point = freshness; order_by ts skips points without ts,
            # so an empty result means the whole space predates the ts field
            newest, _ = client.scroll(
                collection,
                scroll_filter=_space_filter(space),
                limit=1,
                with_payload=["space_name", "source", "ts"],
                order_by=OrderBy(key="ts", direction=Direction.DESC),
            )
        if not newest:
            newest, _ = client.scroll(
                collection,
                scroll_filter=_space_filter(space),
                limit=1,
                with_payload=["space_name", "source"],
            )
        payload = (newest[0].payload or {}) if newest else {}
        return SpaceRow(
            space=space,
            name=payload.get("space_name"),
            source=payload.get("source"),
            points=hit.count,
            last_ts=payload.get("ts"),
        )

    # one or two scrolls per space, network-bound — parallelize
    # (QdrantClient's REST transport is thread-safe)
    with ThreadPoolExecutor(max_workers=8) as pool:
        rows = list(pool.map(row, facet.hits))

    rows.sort(key=lambda r: (r.source or "", -(r.last_ts or 0), r.space))
    return SpacesReport(
        collection=collection,
        points_count=points_count,
        rows=rows,
        unspaced=points_count - sum(r.points for r in rows),
        ts_indexed=ts_indexed,
    )
