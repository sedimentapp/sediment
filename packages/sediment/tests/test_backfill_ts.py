"""backfill_ts.backfill_collection against a local in-memory Qdrant."""

import pytest
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from sediment.backfill_ts import backfill_collection, filename_ts

DIM = 4


def seed(client: QdrantClient, points: list[tuple[int, dict]]) -> None:
    client.create_collection("acme", vectors_config=VectorParams(size=DIM, distance=Distance.COSINE))
    client.upsert(
        "acme",
        points=[PointStruct(id=pid, vector=[0.1] * DIM, payload=payload) for pid, payload in points],
    )


@pytest.fixture
def client():
    return QdrantClient(":memory:")


def payloads(client: QdrantClient) -> dict:
    results, _ = client.scroll("acme", limit=100, with_payload=True)
    return {r.id: r.payload for r in results if r.payload is not None}


def test_stamps_points_of_local_sources(client):
    seed(client, [
        (1, {"text": "a", "source": "youtrack", "file": "youtrack/VN-1.md"}),
        (2, {"text": "b", "source": "youtrack", "file": "youtrack/VN-1.md"}),
        (3, {"text": "c", "source": "mattermost", "file": "mattermost/infra/r1.md"}),
    ])
    mtimes = {"youtrack/VN-1.md": 1700000000, "mattermost/infra/r1.md": 1700000100}

    ok = backfill_collection(client, "acme", mtimes, {"youtrack", "mattermost"})

    assert ok is True
    p = payloads(client)
    assert p[1]["ts"] == 1700000000
    assert p[2]["ts"] == 1700000000
    assert p[3]["ts"] == 1700000100


def test_already_stamped_points_untouched(client):
    seed(client, [
        (1, {"text": "a", "source": "youtrack", "file": "youtrack/VN-1.md", "ts": 1600000000}),
    ])

    ok = backfill_collection(client, "acme", {"youtrack/VN-1.md": 1700000000}, {"youtrack"})

    assert ok is True
    assert payloads(client)[1]["ts"] == 1600000000


def test_other_host_sources_left_alone_without_failing(client):
    seed(client, [
        (1, {"text": "a", "source": "telegram", "file": "telegram/acme/x.md"}),
    ])

    ok = backfill_collection(client, "acme", {}, {"youtrack"})

    assert ok is True
    assert "ts" not in payloads(client)[1]


def test_manual_points_left_alone_without_failing(client):
    seed(client, [
        (1, {"text": "note", "source": "manual", "file": "notes/x", "space": "manual:carol"}),
    ])

    ok = backfill_collection(client, "acme", {}, {"youtrack"})

    assert ok is True
    assert "ts" not in payloads(client)[1]


def test_missing_raw_file_of_local_source_fails_run(client):
    seed(client, [
        (1, {"text": "a", "source": "youtrack", "file": "youtrack/VN-1.md"}),
        (2, {"text": "b", "source": "youtrack", "file": "youtrack/GONE-9.md"}),
    ])

    ok = backfill_collection(client, "acme", {"youtrack/VN-1.md": 1700000000}, {"youtrack"})

    assert ok is False
    p = payloads(client)
    assert p[1]["ts"] == 1700000000  # stampable points are still migrated
    assert "ts" not in p[2]  # no invented timestamp


def test_point_without_source_fails_run(client):
    seed(client, [
        (1, {"text": "orphan", "file": "youtrack/VN-1.md"}),
    ])

    ok = backfill_collection(client, "acme", {"youtrack/VN-1.md": 1700000000}, {"youtrack"})

    assert ok is False
    assert "ts" not in payloads(client)[1]


def test_filename_ts_parsing():
    assert filename_ts("mattermost/infra/abc.2026-05-19T08-04.md") == 1779177840  # 2026-05-19 08:04 UTC
    assert filename_ts("mattermost/infra/abc.md") is None
    assert filename_ts("youtrack/VN-1.md") is None


def test_orphan_stamped_from_filename_when_opted_in(client):
    seed(client, [
        (1, {"text": "a", "source": "mattermost", "file": "mattermost/infra/abc.2026-05-19T08-04.md"}),
        (2, {"text": "b", "source": "mattermost", "file": "mattermost/infra/abc.md"}),  # no stamp
    ])

    ok = backfill_collection(client, "acme", {}, {"mattermost"}, orphan_ts_from_filename=True)

    assert ok is False  # the stampless orphan still fails the run
    p = payloads(client)
    assert p[1]["ts"] == 1779177840
    assert "ts" not in p[2]


def test_orphan_not_stamped_without_flag(client):
    seed(client, [
        (1, {"text": "a", "source": "mattermost", "file": "mattermost/infra/abc.2026-05-19T08-04.md"}),
    ])

    ok = backfill_collection(client, "acme", {}, {"mattermost"})

    assert ok is False
    assert "ts" not in payloads(client)[1]


def test_existing_raw_file_wins_over_filename_stamp(client):
    seed(client, [
        (1, {"text": "a", "source": "mattermost", "file": "mattermost/infra/abc.2026-05-19T08-04.md"}),
    ])
    mtimes = {"mattermost/infra/abc.2026-05-19T08-04.md": 1700000000}

    ok = backfill_collection(client, "acme", mtimes, {"mattermost"}, orphan_ts_from_filename=True)

    assert ok is True
    assert payloads(client)[1]["ts"] == 1700000000


def test_idempotent_rerun(client):
    seed(client, [
        (1, {"text": "a", "source": "youtrack", "file": "youtrack/VN-1.md"}),
    ])
    mtimes = {"youtrack/VN-1.md": 1700000000}

    assert backfill_collection(client, "acme", mtimes, {"youtrack"}) is True
    first = payloads(client)
    assert backfill_collection(client, "acme", mtimes, {"youtrack"}) is True
    assert payloads(client) == first
