"""Shared knowledge-base contract between sediment (writer) and sediment-mcp (reader).

Payload fields written into Qdrant points (all optional except text/source/file):
    text, text_lc, source, file, file_lc, title, filename, chunk_index,
    content_hash, space, space_name, visibility, author

`text_lc`/`file_lc` are lowercase shadows of text/file: Qdrant MatchText on an
unindexed field is a case-sensitive substring match, so the reader's default
case-insensitive keyword/filename filters run against the shadows with a
lowercased needle (quoting the needle switches to the original field).

`space` is the ACL container the document belongs to: "<prefix>:<key>" where
prefix comes from SPACE_PREFIXES and key is the stable source-side id
(Mattermost channel_id, YouTrack project short name, Telegram chat_id,
Claude Code project, or the authoring principal for manual entries).
`space_name` is the human-readable counterpart (display/debug only, never
used for enforcement). `visibility` ("owner"/"org") and `author` are set
only on manual entries stamped by sediment-mcp's add_knowledge.

Collections are declared per deployment in the loader profile.
"""

import json
import urllib.request

SOURCES: list[str] = ["youtrack", "mattermost", "claude", "telegram"]

# space prefix per origin; "manual" covers entries created via add_knowledge
SPACE_PREFIXES: dict[str, str] = {
    "youtrack": "yt",
    "mattermost": "mm",
    "telegram": "tg",
    "claude": "cc",
    "manual": "manual",
}

VISIBILITY_VALUES: tuple[str, ...] = ("owner", "org")


def make_space(kind: str, key: str) -> str:
    """Build a space value "<prefix>:<key>"; both writer and reader must use this."""
    prefix = SPACE_PREFIXES.get(kind)
    if prefix is None:
        raise ValueError(f"Unknown space kind {kind!r}. Allowed: {', '.join(SPACE_PREFIXES)}")
    if not key:
        raise ValueError(f"Empty space key for kind {kind!r}")
    return f"{prefix}:{key}"


def embed(
    texts: list[str],
    embed_url: str,
    model: str,
    timeout: int = 300,
    api_key: str | None = None,
) -> list[list[float]]:
    """POST texts to an OpenAI-compatible embeddings endpoint.

    `embed_url` is the server base URL and "/v1/embeddings" is appended; a URL
    already ending in "/embeddings" is used verbatim — for providers with a
    non-standard prefix (e.g. Gemini's /v1beta/openai/embeddings).
    `api_key` is sent as a Bearer token when set (external providers; a local
    llama.cpp needs none).
    """
    url = embed_url if embed_url.endswith("/embeddings") else f"{embed_url}/v1/embeddings"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(
        url,
        data=json.dumps({"model": model, "input": texts}).encode(),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read())
    return [d["embedding"] for d in payload["data"]]
