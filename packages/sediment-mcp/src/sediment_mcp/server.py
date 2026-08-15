"""MCP server exposing Qdrant knowledge base search over Streamable HTTP."""

import os
import re
import time
import uuid

import anyio.to_thread
from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.utilities.logging import get_logger
from knowledge_schema import SOURCES, VISIBILITY_VALUES, embed, make_space
from qdrant_client import QdrantClient
from qdrant_client.http.exceptions import UnexpectedResponse
from qdrant_client.models import (
    FieldCondition,
    Filter,
    MatchText,
    MatchValue,
    PointStruct,
)
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from sediment_mcp.acl import load_acl
from sediment_mcp.auth import build_auth_provider, current_principal
from sediment_mcp.extensions import load_extensions
from sediment_mcp.limits import (
    MAX_COLLECTION_CHARS,
    MAX_FILENAME_CHARS,
    MAX_KEYWORD_CHARS,
    MAX_KEYWORDS,
    MAX_MANUAL_TEXT_CHARS,
    MAX_QUERY_CHARS,
    MAX_SEARCH_LIMIT,
    MAX_TITLE_CHARS,
    RateLimitMiddleware,
    rate_limit_per_minute,
)

load_dotenv()

QDRANT_URL = os.environ["QDRANT_URL"]
EMBED_URL = os.environ["EMBED_URL"]
EMBED_MODEL = os.environ["EMBED_MODEL"]
# Bearer token for external OpenAI-compatible embedding providers; a local
# llama.cpp needs none. Must match the provider the collections were built with.
EMBED_API_KEY = os.environ.get("EMBED_API_KEY")

MCP_PORT = int(os.environ.get("MCP_PORT", "8080"))

mcp = FastMCP("qdrant-knowledge", mask_error_details=True)
client = QdrantClient(url=QDRANT_URL, api_key=os.environ.get("QDRANT_API_KEY"))
logger = get_logger(__name__)

# Loaded once at startup; a broken config is a fatal import error — the server
# must never come up half-protected. None = ACL disabled (allow-all).
ACL = load_acl()

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


def _build_filter(
    keywords: list[str] | None,
    source: str | None,
    filename: str | None,
    acl_condition: Filter | None,
) -> Filter | None:
    conditions = []
    if keywords:
        for kw in keywords:
            conditions.append(_substring_condition("text", kw))
    if source:
        conditions.append(FieldCondition(key="source", match=MatchValue(value=source)))
    if filename:
        conditions.append(_substring_condition("file", filename))
    if acl_condition is not None:
        # must = AND: user params can only narrow the ACL scope, never widen it
        conditions.append(acl_condition)
    return Filter(must=conditions) if conditions else None


def _format_result(r, show_score: bool = True) -> str:
    p = r.payload
    score = f"[{r.score:.3f}] " if show_score and r.score is not None else ""
    title = f"\n  {p['title']}" if p.get("title") else ""
    return f"{score}{p['source']}/{p['file']}{title}\n\n{p['text']}"


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
    limit: int = 20,
) -> str:
    """Search a knowledge base collection.

    Args:
        collection: Qdrant collection name (e.g. "acme", "globex").
        query: Semantic search query (natural language). Leave empty for keyword-only search.
        keywords: Substring filters (AND logic), case-insensitive. Wrap a keyword in double quotes ('"YouTrack"') to force exact-case matching. Good for IPs, hostnames, ticket IDs.
        source: Filter by source. One of: "youtrack", "mattermost", "claude", "telegram", "manual".
        filename: Substring filter on the file field, case-insensitive (e.g. "vn-242" to find by ticket ID); double-quote to force exact case.
        limit: Max results (default 20).
    """
    if error := _search_input_error(collection, query, keywords, filename, limit):
        return error
    if not query and not keywords and not filename:
        return "Provide either a query, keywords, or filename."
    if source is not None and source not in SEARCH_SOURCES:
        return f"Unknown source {source!r}. Allowed: {', '.join(SEARCH_SOURCES)}."

    acl_condition = None
    if ACL is not None:
        grant = ACL.resolve(current_principal())
        if collection not in grant.collections:
            # same message as for a nonexistent collection — no enumeration oracle
            return f"Collection {collection!r} is not accessible."
        acl_condition = grant.space_condition()

    qfilter = _build_filter(keywords, source, filename, acl_condition)

    try:
        if query:
            vector = embed([query], EMBED_URL, EMBED_MODEL, timeout=30, api_key=EMBED_API_KEY)[0]
            results = client.query_points(
                collection, query=vector, query_filter=qfilter, limit=limit
            )
            items = [_format_result(r) for r in results.points]
        else:
            results, _ = client.scroll(
                collection, scroll_filter=qfilter, limit=limit, with_payload=True
            )
            items = [_format_result(r, show_score=False) for r in results]
    except UnexpectedResponse as e:
        if e.status_code == 404:
            return f"Collection {collection!r} is not accessible."
        raise

    if not items:
        return "No results found."

    return f"Found {len(items)} results:\n\n" + "\n\n---\n\n".join(items)


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
        "author": principal,
        "visibility": visibility,
        "ts": int(time.time()),
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
    grant = None
    if ACL is not None:
        grant = ACL.resolve(principal)
        if collection not in grant.write_collections:
            return f"No write access to collection {collection!r}."
    if visibility == "org" and grant is not None and collection not in grant.unrestricted_write_collections:
        return "visibility='org' requires unrestricted write access to the collection."

    vector = embed([text], EMBED_URL, EMBED_MODEL, timeout=30, api_key=EMBED_API_KEY)[0]
    payload = _manual_payload(principal, text, file, title, visibility)

    try:
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
