"""MCP server exposing Qdrant knowledge base search over Streamable HTTP."""

import os
import re
import time
import uuid
from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any

import anyio.to_thread
from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.utilities.logging import get_logger
from knowledge_schema import (
    CHUNK_OVERLAP,
    DOC_KINDS,
    SOURCES,
    SPACE_KINDS,
    VISIBILITY_VALUES,
    embed,
    make_space,
)
from qdrant_client import QdrantClient
from qdrant_client.http.exceptions import UnexpectedResponse
from qdrant_client.models import (
    Condition,
    Direction,
    FieldCondition,
    Filter,
    IsEmptyCondition,
    MatchText,
    MatchValue,
    OrderBy,
    PayloadField,
    PointStruct,
    Range,
    Record,
)
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from sediment_mcp.access import load_access
from knowledge_schema import validate_index
from sediment_mcp.auth import build_auth_provider, current_principal
from sediment_mcp.extensions import load_extensions
from sediment_mcp.limits import (
    MAX_CHUNK_INDEX,
    MAX_COLLECTION_CHARS,
    MAX_DOCUMENT_CHARS,
    MAX_FILENAME_CHARS,
    MAX_KEYWORD_CHARS,
    MAX_KEYWORDS,
    MAX_MANUAL_TEXT_CHARS,
    MAX_QUERY_CHARS,
    MAX_SEARCH_LIMIT,
    MAX_SPACE_CHARS,
    MAX_TITLE_CHARS,
    RateLimitMiddleware,
    rate_limit_per_minute,
)

load_dotenv()

QDRANT_URL = os.environ["QDRANT_URL"]
EMBED_URL = os.environ["EMBED_URL"]
EMBED_MODEL = os.environ["EMBED_MODEL"]
EMBED_QUERY_INSTRUCTION = os.environ.get("EMBED_QUERY_INSTRUCTION")
if EMBED_QUERY_INSTRUCTION is not None and not EMBED_QUERY_INSTRUCTION.strip():
    raise ValueError("EMBED_QUERY_INSTRUCTION must be non-empty when configured")
# Bearer token for external OpenAI-compatible embedding providers; a local
# llama.cpp needs none. Must match the provider the collections were built with.
EMBED_API_KEY = os.environ.get("EMBED_API_KEY")

MCP_PORT = int(os.environ.get("MCP_PORT", "8080"))

mcp = FastMCP("qdrant-knowledge", mask_error_details=True)
client = QdrantClient(url=QDRANT_URL, api_key=os.environ.get("QDRANT_API_KEY"))
logger = get_logger(__name__)

# The source is fixed at startup; each tool reads one current access snapshot.
ACCESS = load_access()

SEARCH_SOURCES = [*SOURCES, "manual"]
_COLLECTION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _probe_via_gateway(request: Request) -> bool:
    # Probes are for the in-cluster kubelet (direct to pod, no forwarding header).
    # Envoy always appends X-Forwarded-For on proxied requests, so its presence
    # means the request came through the public gateway — probes 404 there.
    return "x-forwarded-for" in request.headers


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> PlainTextResponse:
    if _probe_via_gateway(request):
        return PlainTextResponse("not found", status_code=404)
    return PlainTextResponse("ok")


def _check_dependencies() -> None:
    client.get_collections()
    vectors = embed(["readiness"], EMBED_URL, EMBED_MODEL, timeout=10, api_key=EMBED_API_KEY)
    if len(vectors) != 1 or not vectors[0]:
        raise RuntimeError("embedding backend returned no vector")


# cache the live embed check so a flood of probes can't amplify into one
# inference per request
_READY_TTL = 10.0
_ready_cache: tuple[float, bool] = (float("-inf"), False)


def _readiness() -> bool:
    global _ready_cache
    cached_at, cached_ok = _ready_cache
    if time.monotonic() - cached_at < _READY_TTL:
        return cached_ok
    try:
        _check_dependencies()
        ok = True
    except Exception as exc:
        logger.warning("Readiness dependency check failed: %s", exc)
        ok = False
    _ready_cache = (time.monotonic(), ok)
    return ok


@mcp.custom_route("/ready", methods=["GET"])
async def ready(request: Request) -> PlainTextResponse:
    if _probe_via_gateway(request):
        return PlainTextResponse("not found", status_code=404)
    if await anyio.to_thread.run_sync(_readiness):
        return PlainTextResponse("ok")
    return PlainTextResponse("not ready", status_code=503)


def _substring_condition(field: str, needle: str) -> FieldCondition:
    """Substring filter, case-insensitive by default.

    Qdrant MatchText on an unindexed field is case-sensitive, so the default
    path lowercases the needle and matches against the `<field>_lc` shadow
    written by the loader. Wrapping the needle in double quotes ("YouTrack")
    forces a case-sensitive match on the original field.
    """
    if len(needle) >= 2 and needle.startswith('"') and needle.endswith('"'):
        return FieldCondition(key=field, match=MatchText(text=needle[1:-1]))
    return FieldCondition(key=f"{field}_lc", match=MatchText(text=needle.lower()))


_DAY_SECONDS = 86_400


def _parse_ts_bound(value: str, *, upper: bool) -> int:
    """Parse a user-supplied date bound into epoch seconds.

    A bare "YYYY-MM-DD" names a whole UTC day, so as an upper bound it means
    that day's last second — otherwise `until` would silently drop everything
    written on the day the caller named. A timestamp without an offset is read
    as UTC, matching `ts` (epoch seconds of the raw file's mtime).
    """
    text = value.strip()
    try:
        if len(text) == 10:
            day = date.fromisoformat(text)
            start = int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp())
            return start + _DAY_SECONDS - 1 if upper else start
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(
            f"Invalid date {value!r}: use YYYY-MM-DD or an ISO timestamp "
            "such as 2026-03-01T12:00:00Z."
        ) from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return int(moment.timestamp())


def _ts_range(since: str | None, until: str | None) -> Range | None:
    gte = _parse_ts_bound(since, upper=False) if since else None
    lte = _parse_ts_bound(until, upper=True) if until else None
    if gte is None and lte is None:
        return None
    if gte is not None and lte is not None and gte > lte:
        raise ValueError("`since` must not be later than `until`.")
    return Range(gte=gte, lte=lte)


def _build_filter(
    keywords: list[str] | None,
    source: str | None,
    filename: str | None,
    acl_condition: Filter | None,
    ts_range: Range | None = None,
    exact: Mapping[str, str | None] | None = None,
) -> Filter | None:
    conditions = []
    if keywords:
        for kw in keywords:
            conditions.append(_substring_condition("text", kw))
    if source:
        conditions.append(FieldCondition(key="source", match=MatchValue(value=source)))
    if filename:
        conditions.append(_substring_condition("file", filename))
    for field, value in (exact or {}).items():
        if value:
            conditions.append(FieldCondition(key=field, match=MatchValue(value=value)))
    if ts_range is not None:
        conditions.append(FieldCondition(key="ts", range=ts_range))
    if acl_condition is not None:
        # must = AND: user params can only narrow the ACL scope, never widen it
        conditions.append(acl_condition)
    return Filter(must=conditions) if conditions else None


def _format_result(r, show_score: bool = True) -> str:
    p = r.payload
    score = f"[{r.score:.3f}] " if show_score and r.score is not None else ""
    title = f"\n  {p['title']}" if p.get("title") else ""
    # "source: file", not "source/file": the file already starts with the source
    # directory, and a doubled prefix is what get_document would be handed back
    return f"{score}{p['source']}: {p['file']}{title}\n\n{p['text']}"


def _collection_error(collection: str) -> str | None:
    if not collection or len(collection) > MAX_COLLECTION_CHARS or not _COLLECTION_RE.fullmatch(collection):
        return (
            f"Invalid collection name: use 1-{MAX_COLLECTION_CHARS} ASCII letters, "
            "digits, dots, underscores, or hyphens."
        )
    return None


def _search_input_error(
    collection: str,
    query: str,
    keywords: list[str] | None,
    filename: str | None,
    limit: int,
) -> str | None:
    if error := _collection_error(collection):
        return error
    if len(query) > MAX_QUERY_CHARS:
        return f"Query is too long (max {MAX_QUERY_CHARS} characters)."
    if keywords is not None:
        if len(keywords) > MAX_KEYWORDS:
            return f"Too many keywords (max {MAX_KEYWORDS})."
        if any(not keyword or len(keyword) > MAX_KEYWORD_CHARS for keyword in keywords):
            return f"Each keyword must contain 1-{MAX_KEYWORD_CHARS} characters."
    if filename is not None and len(filename) > MAX_FILENAME_CHARS:
        return f"Filename filter is too long (max {MAX_FILENAME_CHARS} characters)."
    if not 1 <= limit <= MAX_SEARCH_LIMIT:
        return f"Limit must be between 1 and {MAX_SEARCH_LIMIT}."
    return None


def _add_input_error(collection: str, text: str, file: str, title: str) -> str | None:
    if error := _collection_error(collection):
        return error
    if not text or len(text) > MAX_MANUAL_TEXT_CHARS:
        return f"Text must contain 1-{MAX_MANUAL_TEXT_CHARS} characters."
    if not file or len(file) > MAX_FILENAME_CHARS:
        return f"File must contain 1-{MAX_FILENAME_CHARS} characters."
    if len(title) > MAX_TITLE_CHARS:
        return f"Title is too long (max {MAX_TITLE_CHARS} characters)."
    return None


@mcp.tool()
def search(
    collection: str,
    query: str = "",
    keywords: list[str] | None = None,
    source: str | None = None,
    filename: str | None = None,
    space: str | None = None,
    space_kind: str | None = None,
    doc_kind: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 20,
) -> str:
    """Search a knowledge base collection.

    Args:
        collection: Qdrant collection name (e.g. "acme", "globex").
        query: Semantic search query (natural language). Leave empty for keyword-only search.
        keywords: Substring filters (AND logic), case-insensitive. Wrap a keyword in double quotes ('"YouTrack"') to force exact-case matching. Good for IPs, hostnames, ticket IDs.
        source: Filter by source. One of: "youtrack", "mattermost", "claude", "telegram", "manual".
        filename: Substring filter on the file field, case-insensitive (e.g. "vn-242" to find by ticket ID); double-quote to force exact case.
        space: Exact container id — one project, channel or chat ("yt:<project>", "mm:<channel id>", "tg:<chat id>", "cc:<project>", "manual:<user>").
        space_kind: Kind of container: "public", "private", "group_dm", "dm" (Mattermost), "dm", "bot", "group", "channel" (Telegram), "project" (YouTrack, Claude Code), "manual". Use "dm" to reach one-on-one conversations, or filter them out.
        doc_kind: Kind of document: "issue", "article" (YouTrack), "thread" (chats), "session", "subagent" (Claude Code), "note" (manual).
        since: Keep only documents from this date on. "YYYY-MM-DD" (UTC) or an ISO timestamp. Dates come from the source content, not from indexing time; documents with no date are excluded whenever since/until is set.
        until: Keep only documents up to this date; a bare "YYYY-MM-DD" includes that whole day.
        limit: Max results (default 20).

    A date range alone is a valid search: with no query it lists that period newest first,
    which is the cheap way to ask "what happened in March" without a semantic guess.
    Results are chunks — use get_document to read one of them in full.

    space/space_kind/doc_kind are filters, never access control, and they match only
    points that carry the field: content indexed before its source learned to label
    itself has no kind and is invisible to a kind filter.
    """
    if error := _search_input_error(collection, query, keywords, filename, limit):
        return error
    if space is not None and (not space or len(space) > MAX_SPACE_CHARS):
        return f"Space must contain 1-{MAX_SPACE_CHARS} characters."
    if space_kind is not None and space_kind not in SPACE_KINDS:
        return f"Unknown space_kind {space_kind!r}. Allowed: {', '.join(SPACE_KINDS)}."
    if doc_kind is not None and doc_kind not in DOC_KINDS:
        return f"Unknown doc_kind {doc_kind!r}. Allowed: {', '.join(DOC_KINDS)}."
    try:
        ts_range = _ts_range(since, until)
    except ValueError as exc:
        return str(exc)
    narrowed = any((query, keywords, filename, space, space_kind, doc_kind)) or ts_range is not None
    if not narrowed:
        return "Provide a query, keywords, filename, space, kind, or a since/until date range."
    if source is not None and source not in SEARCH_SOURCES:
        return f"Unknown source {source!r}. Allowed: {', '.join(SEARCH_SOURCES)}."

    acl_condition = None
    snapshot = ACCESS.snapshot()
    grant = snapshot.resolve(current_principal(), collection) if snapshot.acl is not None else None
    if grant is not None:
        if collection not in grant.collections:
            # same message as for a nonexistent collection — no enumeration oracle
            return f"Collection {collection!r} is not accessible."
        acl_condition = grant.space_condition()

    qfilter = _build_filter(
        keywords,
        source,
        filename,
        acl_condition,
        ts_range,
        {"space": space, "space_kind": space_kind, "doc_kind": doc_kind},
    )

    try:
        if query:
            validate_index(client.get_collection(collection).config, EMBED_MODEL)
            embedding_query = query
            if EMBED_QUERY_INSTRUCTION is not None:
                embedding_query = f"Instruct: {EMBED_QUERY_INSTRUCTION}\nQuery: {query}"
            vector = embed([embedding_query], EMBED_URL, EMBED_MODEL, timeout=30, api_key=EMBED_API_KEY)[0]
            validate_index(client.get_collection(collection).config, EMBED_MODEL, len(vector))
            results = client.query_points(
                collection, query=vector, query_filter=qfilter, limit=limit
            )
            items = [_format_result(r) for r in results.points]
        else:
            validate_index(client.get_collection(collection).config, EMBED_MODEL)
            # Newest first only when a date range is set: order_by drops points
            # that lack the key, and pre-ts entries must stay findable by keyword.
            order_by = OrderBy(key="ts", direction=Direction.DESC) if ts_range else None
            results, _ = client.scroll(
                collection,
                scroll_filter=qfilter,
                limit=limit,
                with_payload=True,
                order_by=order_by,
            )
            items = [_format_result(r, show_score=False) for r in results]
    except UnexpectedResponse as e:
        if e.status_code == 404:
            return f"Collection {collection!r} is not accessible."
        raise

    if not items:
        return "No results found."

    return f"Found {len(items)} results:\n\n" + "\n\n---\n\n".join(items)


# One call reads at most this many chunks; the char budget cuts it shorter on
# dense documents and the caller pages on with from_chunk.
_DOCUMENT_WINDOW = 96


def _payload(point: Record) -> dict[str, Any]:
    # every filter these points come back through matches on a payload field,
    # so a payload-less point would mean Qdrant contradicted its own query
    if point.payload is None:
        raise RuntimeError(f"Point {point.id} came back without a payload")
    return point.payload


def _chunk_index(point: Record) -> int:
    return _payload(point).get("chunk_index", 0)


def _stitch(texts: list[str]) -> str:
    """Join consecutive chunks back into document text.

    The loader starts each chunk with the previous chunk's last CHUNK_OVERLAP
    characters, so that repeat is dropped here. Only an exact overlap-length
    match counts — a shorter coincidence would eat real text, and a gap in
    chunk_index (the loader drops chunks under 30 chars) simply won't match.

    Repeats inside a chunk are left alone: a paragraph longer than CHUNK_SIZE is
    hard-split into overlapping pieces that the loader packs as if they were
    separate paragraphs, and the "\n\n" they are joined with is ambiguous enough
    that undoing it drops real text. A visible repeat beats a silent hole.
    """
    out: list[str] = []
    previous = ""
    for text in texts:
        if out and len(previous) > CHUNK_OVERLAP and text[:CHUNK_OVERLAP] == previous[-CHUNK_OVERLAP:]:
            # removeprefix, not lstrip: the loader glued the carried-over tail on
            # with exactly one "\n\n", and a piece of its own may start with a newline
            out.append(text[CHUNK_OVERLAP:].removeprefix("\n\n"))
        else:
            out.append(text)
        previous = text
    return "\n\n".join(out)


@mcp.tool()
def get_document(collection: str, file: str, from_chunk: int = 0) -> str:
    """Read one whole document that search returned a chunk of.

    Search scores chunks; this reassembles the chunks of a single file into the
    document text, in order and with the loader's chunk overlap removed.

    Args:
        collection: Qdrant collection name (e.g. "acme", "globex").
        file: Exact value of the file field as printed by search after "source: " ("mattermost/apps__c8fh/2026-03.md"). Not a substring — use search(filename=...) to find it.
        from_chunk: Resume at this chunk index. Long documents come back truncated with the index to continue from.
    """
    if error := _collection_error(collection):
        return error
    if not file or len(file) > MAX_FILENAME_CHARS:
        return f"File must contain 1-{MAX_FILENAME_CHARS} characters."
    if not 0 <= from_chunk <= MAX_CHUNK_INDEX:
        return f"from_chunk must be between 0 and {MAX_CHUNK_INDEX}."

    acl_condition = None
    snapshot = ACCESS.snapshot()
    grant = snapshot.resolve(current_principal(), collection) if snapshot.acl is not None else None
    if grant is not None:
        if collection not in grant.collections:
            return f"Collection {collection!r} is not accessible."
        acl_condition = grant.space_condition()

    conditions: list[Condition] = [FieldCondition(key="file", match=MatchValue(value=file))]
    if acl_condition is not None:
        conditions.append(acl_condition)
    window: list[Condition] = [
        FieldCondition(
            key="chunk_index",
            range=Range(gte=from_chunk, lt=from_chunk + _DOCUMENT_WINDOW),
        )
    ]
    if from_chunk == 0:
        # manual entries carry no chunk_index; without this they would be
        # invisible to get_document while search happily returns them
        window.append(IsEmptyCondition(is_empty=PayloadField(key="chunk_index")))

    try:
        validate_index(client.get_collection(collection).config, EMBED_MODEL)
        total = client.count(
            collection, count_filter=Filter(must=conditions), exact=True
        ).count
        points, _ = client.scroll(
            collection,
            scroll_filter=Filter(must=[*conditions, Filter(should=window)]),
            limit=_DOCUMENT_WINDOW,
            with_payload=True,
        )
    except UnexpectedResponse as e:
        if e.status_code == 404:
            return f"Collection {collection!r} is not accessible."
        raise

    if not points:
        if total:
            return f"No chunks at or after index {from_chunk}; {file!r} has {total}."
        return (
            f"No document {file!r} in {collection!r}. The file must match exactly — "
            "use search(filename=...) to find its exact value."
        )

    points.sort(key=_chunk_index)
    texts: list[str] = []
    used = 0
    next_chunk = None
    for point in points:
        text = _payload(point)["text"]
        if texts and used + len(text) > MAX_DOCUMENT_CHARS:
            next_chunk = _chunk_index(point)
            break
        texts.append(text)
        used += len(text)
    if next_chunk is None and len(points) == _DOCUMENT_WINDOW and len(texts) < total:
        next_chunk = _chunk_index(points[-1]) + 1

    head = _payload(points[0])
    span = f"{_chunk_index(points[0])}-{_chunk_index(points[len(texts) - 1])}"
    header = f"{head['source']}: {file} — chunks {span} of {total}"
    if head.get("title"):
        header += f"\n{head['title']}"
    body = _stitch(texts)
    if next_chunk is not None:
        body += f"\n\n[Truncated. Continue with from_chunk={next_chunk}.]"
    return f"{header}\n\n{body}"


def _manual_payload(principal: str, text: str, file: str, title: str, visibility: str) -> dict:
    """Server-stamped payload for manual entries: source/space/author are never
    client-controlled — otherwise a client could plant content into another
    department's space (poisoning)."""
    payload = {
        "text": text,
        "text_lc": text.lower(),
        "source": "manual",
        "file": file,
        "file_lc": file.lower(),
        "space": make_space("manual", principal),
        "space_kind": "manual",
        "doc_kind": "note",
        "author": principal,
        "visibility": visibility,
        "ts": int(time.time()),
        "chunk_index": 0,
    }
    if title:
        payload["title"] = title
    return payload


@mcp.tool()
def add_knowledge(
    collection: str,
    text: str,
    file: str,
    title: str = "",
    visibility: str = "owner",
) -> str:
    """Add a manual knowledge entry to a Qdrant collection.

    The entry is attributed to the authenticated user (source="manual",
    space="manual:<user>"). With visibility="owner" (default) only the author
    finds it; visibility="org" makes it visible to everyone with access to the
    collection.

    Args:
        collection: Qdrant collection name (e.g. "acme", "globex").
        text: The content to store.
        file: File or document identifier.
        title: Optional title for the entry.
        visibility: "owner" (default) or "org".
    """
    if error := _add_input_error(collection, text, file, title):
        return error
    if visibility not in VISIBILITY_VALUES:
        return f"Unknown visibility {visibility!r}. Allowed: {', '.join(VISIBILITY_VALUES)}."

    principal = current_principal()
    grant = ACCESS.snapshot().resolve(principal, collection)
    if grant is not None:
        if collection not in grant.write_collections:
            return f"No write access to collection {collection!r}."
    if visibility == "org" and grant is not None and collection not in grant.unrestricted_write_collections:
        return "visibility='org' requires unrestricted write access to the collection."

    payload = _manual_payload(principal, text, file, title, visibility)

    try:
        validate_index(client.get_collection(collection).config, EMBED_MODEL)
        vector = embed([text], EMBED_URL, EMBED_MODEL, timeout=30, api_key=EMBED_API_KEY)[0]
        validate_index(client.get_collection(collection).config, EMBED_MODEL, len(vector))
        client.upsert(
            collection,
            points=[
                PointStruct(
                    id=uuid.uuid4().hex,
                    vector=vector,
                    payload=payload,
                )
            ],
        )
    except UnexpectedResponse as e:
        if e.status_code == 404:
            return f"Collection {collection!r} is not accessible."
        raise
    return f"Added entry to '{collection}': {payload['space']}/{file} (visibility={visibility})"


def main() -> None:
    mcp.auth = build_auth_provider()
    mcp.add_middleware(RateLimitMiddleware(rate_limit_per_minute()))
    load_extensions(mcp)

    mcp.run(
        transport="http",
        host="0.0.0.0",
        port=MCP_PORT,
        path="/mcp",
        stateless_http=True,
        # Clients connect via the k8s ClusterIP, so the Host header never
        # matches a configured hostname; bearer auth is the access control.
        host_origin_protection=False,
        show_banner=False,
    )


if __name__ == "__main__":
    main()
