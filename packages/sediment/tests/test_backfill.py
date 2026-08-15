"""backfill_collection against a local in-memory Qdrant."""

import pytest
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from sediment.backfill_acl import backfill_collection
from sediment.spaces import SpaceResolver

DIM = 4

PROFILE = {
    "mattermost": {
        "channels": [
            {"id": "chanidinfra", "name": "infra"},
        ]
    }
}


def seed(client: QdrantClient, points: list[tuple[int, dict]]) -> None:
    client.create_collection("acme", vectors_config=VectorParams(size=DIM, distance=Distance.COSINE))
    client.upsert(
        "acme",
        points=[PointStruct(id=pid, vector=[0.1] * DIM, payload=payload) for pid, payload in points],
    )


@pytest.fixture
def client():
    return QdrantClient(":memory:")


@pytest.fixture
def spaces():
    return SpaceResolver.from_profile(PROFILE)


def payloads(client: QdrantClient) -> dict:
    results, _ = client.scroll("acme", limit=100, with_payload=True)
    return {r.id: r.payload for r in results if r.payload is not None}


def test_stamps_mappable_points(client, spaces):
    seed(client, [
        (1, {"text": "a", "source": "youtrack", "file": "youtrack/VN-1.md"}),
        (2, {"text": "b", "source": "youtrack", "file": "youtrack/VN-1.md"}),
        (3, {"text": "c", "source": "mattermost", "file": "mattermost/infra/r1.md"}),
    ])

    ok = backfill_collection(client, "acme", spaces, stamp_manual_org=False)

    assert ok is True
    p = payloads(client)
    assert p[1]["space"] == "yt:VN" and p[1]["space_name"] == "VN"
    assert p[2]["space"] == "yt:VN"
    assert p[3]["space"] == "mm:chanidinfra" and p[3]["space_name"] == "infra"


def test_unmapped_points_fail_run_and_stay_unstamped(client, spaces):
    seed(client, [
        (1, {"text": "a", "source": "youtrack", "file": "youtrack/VN-1.md"}),
        (2, {"text": "b", "source": "mattermost", "file": "mattermost/gone/r1.md"}),
    ])

    ok = backfill_collection(client, "acme", spaces, stamp_manual_org=False)

    assert ok is False
    p = payloads(client)
    assert p[1]["space"] == "yt:VN"  # mappable points are still migrated
    assert "space" not in p[2]  # fail-closed: no invented space


def test_manual_points_listed_but_not_stamped_by_default(client, spaces):
    seed(client, [
        (1, {"text": "note", "source": "manual", "file": "notes/x"}),
        (2, {"text": "orphan"}),  # no file at all
    ])

    ok = backfill_collection(client, "acme", spaces, stamp_manual_org=False)

    assert ok is False
    p = payloads(client)
    assert "space" not in p[1] and "space" not in p[2]


def test_manual_point_with_space_is_already_migrated(client, spaces):
    seed(client, [
        (1, {"text": "note", "source": "manual", "file": "notes/x",
             "space": "manual:carol", "author": "carol", "visibility": "org"}),
    ])

    ok = backfill_collection(client, "acme", spaces, stamp_manual_org=False)

    assert ok is True
    assert payloads(client)[1]["space"] == "manual:carol"  # untouched


def test_stamp_manual_org_flag(client, spaces):
    seed(client, [
        (1, {"text": "note", "source": "manual", "file": "notes/x"}),
        (2, {"text": "orphan"}),
    ])

    ok = backfill_collection(client, "acme", spaces, stamp_manual_org=True)

    assert ok is True
    p = payloads(client)
    for pid in (1, 2):
        assert p[pid]["space"] == "manual:unknown"
        assert p[pid]["visibility"] == "org"
        assert p[pid]["author"] == "unknown"


def test_idempotent_rerun(client, spaces):
    seed(client, [
        (1, {"text": "a", "source": "youtrack", "file": "youtrack/VN-1.md"}),
    ])

    assert backfill_collection(client, "acme", spaces, stamp_manual_org=False) is True
    first = payloads(client)
    assert backfill_collection(client, "acme", spaces, stamp_manual_org=False) is True
    assert payloads(client) == first
