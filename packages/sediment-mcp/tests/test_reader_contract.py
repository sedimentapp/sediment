import pytest

from sediment_mcp import server


@pytest.mark.parametrize("operation", ["search", "keywords", "document", "add"])
def test_reader_rejects_same_dimension_different_model(qdrant, monkeypatch, operation):
    monkeypatch.setattr(server, "EMBED_MODEL", "different-model")
    monkeypatch.setattr(server, "current_principal", lambda: "alice")
    def forbidden(*args, **kwargs):
        pytest.fail("incompatible index must not be queried or written")
    monkeypatch.setattr(server, "embed", forbidden)
    monkeypatch.setattr(qdrant, "query_points", forbidden)
    monkeypatch.setattr(qdrant, "scroll", forbidden)
    monkeypatch.setattr(qdrant, "upsert", forbidden)
    with pytest.raises(ValueError, match="Incompatible or unversioned"):
        if operation == "search":
            server.search("acme", query="test question")
        elif operation == "keywords":
            server.search("acme", keywords=["test"])
        elif operation == "document":
            server.get_document("acme", "youtrack/ACME-1.md")
        else:
            server.add_knowledge("acme", "A meaningful manual note.", "notes/test")
