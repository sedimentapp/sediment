import pytest
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams

from knowledge_schema import index_contract, validate_index
from sediment import load as ql
from sediment.spaces import SpaceResolver


@pytest.mark.parametrize("field,value", [
    ("embedding_model", "different-model"), ("schema_version", 2),
    ("chunking_version", 2), ("chunk_size", 900), ("chunk_overlap", 50),
    ("dimension", 9),
])
def test_incompatible_contract_is_rejected_before_writer_mutation(tmp_path, monkeypatch, field, value):
    monkeypatch.setenv("EMBED_MODEL", "test-model")
    contract = index_contract("test-model", 8)
    contract[field] = value
    client = QdrantClient(":memory:")
    client.create_collection("acme", vectors_config=VectorParams(size=8, distance=Distance.COSINE), metadata={"sediment": contract})
    def mutation(*args, **kwargs):
        pytest.fail("incompatible collection must not be mutated")
    monkeypatch.setattr(client, "upsert", mutation)
    monkeypatch.setattr(client, "delete", mutation)
    monkeypatch.setattr(client, "create_payload_index", mutation)
    with pytest.raises(ValueError, match="Incompatible or unversioned"):
        ql.load_collection("acme", ["youtrack"], tmp_path, client, 8, False, SpaceResolver.from_profile({}))


def test_unversioned_collection_is_not_adopted():
    client = QdrantClient(":memory:")
    client.create_collection("old", vectors_config=VectorParams(size=8, distance=Distance.COSINE))
    with pytest.raises(ValueError, match="unversioned"):
        validate_index(client.get_collection("old").config, "test-model", 8)


def test_new_collection_persists_contract(tmp_path, monkeypatch):
    monkeypatch.setenv("EMBED_MODEL", "test-model")
    client = QdrantClient(":memory:")
    ql.load_collection("acme", ["youtrack"], tmp_path, client, 8, False, SpaceResolver.from_profile({}))
    assert client.get_collection("acme").config.metadata == {"sediment": index_contract("test-model", 8)}


def test_rebuild_uses_new_dimension(tmp_path, monkeypatch):
    monkeypatch.setenv("EMBED_MODEL", "new-model")
    client = QdrantClient(":memory:")
    client.create_collection("acme", vectors_config=VectorParams(size=8, distance=Distance.COSINE))
    ql.load_collection("acme", ["youtrack"], tmp_path, client, 12, True, SpaceResolver.from_profile({}))
    validate_index(client.get_collection("acme").config, "new-model", 12)


def test_model_configuration_has_no_implicit_default(monkeypatch):
    monkeypatch.setenv("EMBED_URL", "http://embed.invalid")
    monkeypatch.delenv("EMBED_MODEL", raising=False)
    with pytest.raises(RuntimeError, match="EMBED_MODEL"):
        ql.embedding_config()
