from sediment_mcp.access import FileAccess
import os

# sediment_mcp.server requires these at import time; set before any test imports it.
# QdrantClient does not connect eagerly, so a dummy URL is fine for unit tests.
os.environ.setdefault("QDRANT_URL", "http://qdrant.invalid:6333")
os.environ.setdefault("EMBED_URL", "http://embed.invalid:8080")
os.environ.setdefault("EMBED_MODEL", "test-model")
os.environ.setdefault("MCP_ACL_DISABLE", "1")

import pytest  # noqa: E402
from qdrant_client import QdrantClient  # noqa: E402
from qdrant_client.models import Distance, PayloadSchemaType, PointStruct, VectorParams  # noqa: E402
from knowledge_schema import index_contract  # noqa: E402


@pytest.fixture
def qdrant(monkeypatch):
    """In-memory Qdrant wired into the server module, ACL off."""
    from sediment_mcp import server

    client = QdrantClient(":memory:")
    client.create_collection("acme", vectors_config=VectorParams(size=4, distance=Distance.COSINE), metadata={"sediment": index_contract("test-model", 4)})
    client.create_payload_index("acme", "ts", field_schema=PayloadSchemaType.INTEGER)
    monkeypatch.setattr(server, "client", client)
    monkeypatch.setattr(server, "ACCESS", FileAccess(None))
    return client


@pytest.fixture
def upsert(qdrant):
    def _upsert(points: list[dict]):
        qdrant.upsert(
            "acme",
            points=[
                PointStruct(id=i + 1, vector=[0.1, 0.2, 0.3, 0.4], payload=p)
                for i, p in enumerate(points)
            ],
        )

    return _upsert
