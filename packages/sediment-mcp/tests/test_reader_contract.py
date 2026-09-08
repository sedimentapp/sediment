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


def test_query_instruction_does_not_prefix_manual_documents(qdrant, monkeypatch):
    monkeypatch.setattr(server, "EMBED_QUERY_INSTRUCTION", "Find relevant passages.")
    monkeypatch.setattr(server, "current_principal", lambda: "alice")
    calls = []
    def recording(texts, *args, **kwargs):
        calls.append(texts)
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]
    monkeypatch.setattr(server, "embed", recording)
    server.search("acme", query="Where is the configuration?")
    server.add_knowledge("acme", "A meaningful manual note.", "notes/instruction-test")
    assert calls == [["Instruct: Find relevant passages.\nQuery: Where is the configuration?"], ["A meaningful manual note."]]
