"""load_collection against a local in-memory Qdrant: space stamping and fail-closed paths."""

from pathlib import Path

import pytest
from qdrant_client import QdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchValue

import sediment.load as ql
from sediment.spaces import SpaceResolver

DIM = 8

PROFILE = {
    "mattermost": {
        "channels": [
            {"id": "chanidinfra", "name": "infra"},
        ]
    }
}

LONG = "This paragraph is long enough to survive the 30-char chunk filter."


@pytest.fixture
def client():
    return QdrantClient(":memory:")


@pytest.fixture
def spaces():
    return SpaceResolver.from_profile(PROFILE)


@pytest.fixture(autouse=True)
def fake_embed(monkeypatch):
    monkeypatch.setattr(ql, "embed", lambda texts: [[0.1] * DIM for _ in texts])


def write(raw_dir: Path, rel: str, text: str) -> None:
    path = raw_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def points_by_file(client: QdrantClient, rel: str) -> list[dict]:
    results, _ = client.scroll(
        "acme",
        scroll_filter=Filter(must=[FieldCondition(key="file", match=MatchValue(value=rel))]),
        limit=100,
        with_payload=True,
    )
    return [r.payload for r in results if r.payload is not None]


def load(client, raw_dir, spaces, sources=("youtrack", "mattermost")):
    return ql.load_collection("acme", list(sources), raw_dir, client, DIM, rebuild=False, spaces=spaces)


def test_space_stamped_on_load(tmp_path, client, spaces):
    write(tmp_path, "youtrack/VN-1.md", f"# VN-1\n\n{LONG}")
    write(tmp_path, "mattermost/infra/root1.md", f"# thread\n\n{LONG}")

    unmapped = load(client, tmp_path, spaces)

    assert unmapped == []
    (yt,) = points_by_file(client, "youtrack/VN-1.md")
    assert yt["space"] == "yt:VN"
    assert yt["space_name"] == "VN"
    (mm,) = points_by_file(client, "mattermost/infra/root1.md")
    assert mm["space"] == "mm:chanidinfra"
    assert mm["space_name"] == "infra"


def test_ts_stamped_from_file_mtime(tmp_path, client, spaces):
    write(tmp_path, "youtrack/VN-1.md", f"# VN-1\n\n{LONG}")

    load(client, tmp_path, spaces)

    (yt,) = points_by_file(client, "youtrack/VN-1.md")
    assert yt["ts"] == int((tmp_path / "youtrack/VN-1.md").stat().st_mtime)


def test_loader_scrubs_existing_raw_secrets_and_preserves_mtime(tmp_path, client, spaces):
    path = tmp_path / "youtrack/VN-1.md"
    write(tmp_path, "youtrack/VN-1.md", f"# VN-1\n\n{LONG}\nYOUTRACK_TOKEN=perm:secret")
    before_mtime_ns = path.stat().st_mtime_ns

    load(client, tmp_path, spaces)

    assert "perm:secret" not in path.read_text()
    assert "[REDACTED]" in path.read_text()
    assert path.stat().st_mtime_ns == before_mtime_ns
    (point,) = points_by_file(client, "youtrack/VN-1.md")
    assert "perm:secret" not in point["text"]


def test_unchanged_file_skipped_on_second_run(tmp_path, client, spaces):
    write(tmp_path, "youtrack/VN-1.md", f"# VN-1\n\n{LONG}")
    load(client, tmp_path, spaces)
    count = client.count("acme", exact=True).count

    unmapped = load(client, tmp_path, spaces)

    assert unmapped == []
    assert client.count("acme", exact=True).count == count


def test_changed_file_replaces_points(tmp_path, client, spaces):
    write(tmp_path, "youtrack/VN-1.md", f"# VN-1\n\n{LONG}")
    load(client, tmp_path, spaces)
    old_hash = points_by_file(client, "youtrack/VN-1.md")[0]["content_hash"]

    write(tmp_path, "youtrack/VN-1.md", f"# VN-1\n\n{LONG} Updated content here.")
    load(client, tmp_path, spaces)

    points = points_by_file(client, "youtrack/VN-1.md")
    assert len(points) == 1
    assert points[0]["content_hash"] != old_hash


def test_unmapped_file_skipped_and_reported(tmp_path, client, spaces):
    write(tmp_path, "mattermost/renamed-chan/root9.md", f"# t\n\n{LONG}")

    unmapped = load(client, tmp_path, spaces)

    assert len(unmapped) == 1
    assert unmapped[0].rel_path == "mattermost/renamed-chan/root9.md"
    assert client.count("acme", exact=True).count == 0  # fail-closed: never indexed


def test_unmapped_changed_file_keeps_old_points(tmp_path, client, spaces):
    # File loads fine under the known channel dir...
    write(tmp_path, "mattermost/infra/root1.md", f"# t\n\n{LONG}")
    load(client, tmp_path, spaces)

    # ...then the channel is renamed in the config, and the file content changes:
    # the old points must survive (deleting them would orphan the file entirely).
    stale_spaces = SpaceResolver.from_profile(
        {"mattermost": {"channels": [{"id": "chanidinfra", "name": "infra-renamed"}]}}
    )
    write(tmp_path, "mattermost/infra/root1.md", f"# t\n\n{LONG} New reply arrived.")

    unmapped = load(client, tmp_path, stale_spaces)

    assert len(unmapped) == 1
    points = points_by_file(client, "mattermost/infra/root1.md")
    assert len(points) == 1
    assert points[0]["space"] == "mm:chanidinfra"  # old point intact


def test_mattermost_without_map_is_config_error(tmp_path, client):
    write(tmp_path, "mattermost/infra/root1.md", f"# t\n\n{LONG}")
    with pytest.raises(RuntimeError, match="--config-dir"):
        load(client, tmp_path, SpaceResolver.from_profile({}))


def test_qdrant_url_is_required(monkeypatch):
    monkeypatch.delenv("QDRANT_URL", raising=False)
    with pytest.raises(RuntimeError, match="QDRANT_URL"):
        ql.require_qdrant_url()


def test_qdrant_client_uses_optional_api_key(monkeypatch):
    captured = {}
    monkeypatch.setenv("QDRANT_URL", "https://qdrant.example")
    monkeypatch.setenv("QDRANT_API_KEY", "secret-key")
    monkeypatch.setattr(ql, "QdrantClient", lambda **kwargs: captured.update(kwargs))

    ql.qdrant_client(timeout=30)

    assert captured == {
        "url": "https://qdrant.example",
        "api_key": "secret-key",
        "timeout": 30,
    }


def test_config_dir_env_is_loaded_before_endpoint_resolution(tmp_path, monkeypatch):
    monkeypatch.delenv("QDRANT_URL", raising=False)
    monkeypatch.delenv("EMBED_URL", raising=False)
    monkeypatch.delenv("EMBED_MODEL", raising=False)
    (tmp_path / ".env").write_text(
        "QDRANT_URL=http://qdrant.from-config:6333\n"
        "EMBED_URL=http://embed.from-config:8080\n"
    )

    (tmp_path / "_profile.yaml").write_text("collections:\n  acme:\n    sources: [claude]\n")
    ql.load_profile(str(tmp_path))

    assert ql.require_qdrant_url() == "http://qdrant.from-config:6333"
    assert ql.embedding_config() == ("http://embed.from-config:8080", "bge-m3")


def test_rebuild_requires_explicit_confirmation(capsys, tmp_path):
    with pytest.raises(SystemExit):
        ql.main(["--config-dir", str(tmp_path), "--collection", "acme", "--rebuild"])
    assert "--rebuild requires --yes-really-rebuild" in capsys.readouterr().err


def test_rebuild_confirmation_rejected_without_rebuild(capsys, tmp_path):
    with pytest.raises(SystemExit):
        ql.main(["--config-dir", str(tmp_path), "--collection", "acme", "--yes-really-rebuild"])
    assert "only valid with --rebuild" in capsys.readouterr().err


def test_load_collections_validates_sources():
    cfg = ql.load_collections({"collections": {"acme": {"sources": ["claude", "telegram"]}}})
    assert cfg == {"acme": {"sources": ["claude", "telegram"]}}

    with pytest.raises(RuntimeError, match="no 'collections' block"):
        ql.load_collections({"profiles": {"acme": {}}})

    with pytest.raises(RuntimeError, match="non-empty list"):
        ql.load_collections({"collections": {"acme": {"sources": []}}})

    # a typo in a source name must fail loudly, not silently skip the source
    with pytest.raises(RuntimeError, match="unknown source"):
        ql.load_collections({"collections": {"acme": {"sources": ["clade"]}}})


def test_select_collections_rejects_unknown_name():
    collections: dict[str, ql.CollectionCfg] = {
        "acme": {"sources": ["claude"]},
        "globex": {"sources": ["telegram"]},
    }

    assert ql.select_collections(collections, "all") == ["acme", "globex"]
    assert ql.select_collections(collections, "globex") == ["globex"]

    with pytest.raises(SystemExit, match="unknown collection 'typo'"):
        ql.select_collections(collections, "typo")


def test_raw_dir_env_name_matches_deployed_vars():
    assert ql.raw_dir_env_name("acme") == "RAW_DIR_ACME"
    assert ql.raw_dir_env_name("my-collection") == "RAW_DIR_MY_COLLECTION"


def test_parallel_workers_match_sequential(tmp_path, spaces):
    # More chunks than BATCH_SIZE so the pool actually gets several batches
    for i in range(ql.BATCH_SIZE + 6):
        write(tmp_path, f"youtrack/VN-{i}.md", f"# VN-{i}\n\n{LONG} body number {i}")

    def run(workers):
        client = QdrantClient(":memory:")
        unmapped = ql.load_collection(
            "acme", ["youtrack"], tmp_path, client, DIM, rebuild=False, spaces=spaces, workers=workers
        )
        assert unmapped == []
        results, _ = client.scroll("acme", limit=1000, with_payload=True)
        return sorted(
            (r.payload["file"], r.payload["chunk_index"], r.payload["text"], r.payload["space"])
            for r in results if r.payload is not None
        )

    sequential = run(1)
    assert len(sequential) == ql.BATCH_SIZE + 6
    assert run(4) == sequential
