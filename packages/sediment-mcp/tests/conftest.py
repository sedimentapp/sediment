import os

# sediment_mcp.server requires these at import time; set before any test imports it.
# QdrantClient does not connect eagerly, so a dummy URL is fine for unit tests.
os.environ.setdefault("QDRANT_URL", "http://qdrant.invalid:6333")
os.environ.setdefault("EMBED_URL", "http://embed.invalid:8080")
os.environ.setdefault("EMBED_MODEL", "test-model")
os.environ.setdefault("MCP_ACL_DISABLE", "1")
