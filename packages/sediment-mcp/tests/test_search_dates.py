from datetime import UTC, datetime
from typing import Any

from sediment_mcp import server


def epoch(day: str) -> int:
    return int(datetime.fromisoformat(f"{day}T09:00:00").replace(tzinfo=UTC).timestamp())


def doc(text: str, file: str, source: str = "mattermost", ts: int | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "text": text,
        "text_lc": text.lower(),
        "source": source,
        "file": file,
        "file_lc": file.lower(),
    }
    if ts is not None:
        payload["ts"] = ts
    return payload


DOCS = [
    doc("february standup", "mm/feb.md", ts=epoch("2026-02-15")),
    doc("march standup", "mm/mar.md", ts=epoch("2026-03-10")),
    doc("april standup", "mm/apr.md", ts=epoch("2026-04-01")),
    doc("undated standup", "mm/old.md", source="manual"),
]


def files(result: str) -> list[str]:
    return [
        line.split(": ", 1)[1]
        for line in result.splitlines()
        if line.startswith(("mattermost: ", "manual: "))
    ]


def test_date_range_keeps_only_that_period(upsert):
    upsert(DOCS)

    assert files(server.search("acme", since="2026-03-01", until="2026-03-31")) == ["mm/mar.md"]


def test_bare_until_includes_the_named_day(upsert):
    upsert(DOCS)

    assert files(server.search("acme", since="2026-04-01", until="2026-04-01")) == ["mm/apr.md"]


def test_date_range_lists_newest_first(upsert):
    upsert(DOCS)

    assert files(server.search("acme", since="2026-01-01")) == ["mm/apr.md", "mm/mar.md", "mm/feb.md"]


def test_undated_points_stay_findable_without_a_date_range(upsert):
    upsert(DOCS)

    assert files(server.search("acme", keywords=["undated"])) == ["mm/old.md"]
    assert files(server.search("acme", keywords=["standup"], since="2026-01-01")) == [
        "mm/apr.md",
        "mm/mar.md",
        "mm/feb.md",
    ]


def test_date_range_narrows_a_keyword_search(upsert):
    upsert(DOCS)

    assert files(server.search("acme", keywords=["standup"], until="2026-02-28")) == ["mm/feb.md"]


KINDED = [
    doc("release plan", "mm/team.md", ts=epoch("2026-03-10")) | {"space": "mm:t1", "space_kind": "public", "doc_kind": "thread"},
    doc("lunch?", "mm/bob.md", ts=epoch("2026-03-11")) | {"space": "mm:d1", "space_kind": "dm", "doc_kind": "thread"},
    doc("how to deploy", "yt/INF-A-1.md", source="youtrack", ts=epoch("2026-03-12")) | {"space": "yt:INF", "space_kind": "project", "doc_kind": "article"},
    doc("deploy broken", "yt/INF-42.md", source="youtrack", ts=epoch("2026-03-13")) | {"space": "yt:INF", "space_kind": "project", "doc_kind": "issue"},
    doc("unlabelled old point", "mm/legacy.md", ts=epoch("2026-03-14")),
]


def files_of(result: str) -> list[str]:
    return [
        line.split(": ", 1)[1]
        for line in result.splitlines()
        if line.startswith(("mattermost: ", "manual: ", "youtrack: "))
    ]


def test_space_kind_separates_dms_from_channels(upsert):
    upsert(KINDED)

    assert files_of(server.search("acme", space_kind="dm")) == ["mm/bob.md"]
    assert files_of(server.search("acme", space_kind="public")) == ["mm/team.md"]


def test_doc_kind_separates_articles_from_issues(upsert):
    upsert(KINDED)

    assert files_of(server.search("acme", doc_kind="article")) == ["yt/INF-A-1.md"]
    assert files_of(server.search("acme", doc_kind="issue")) == ["yt/INF-42.md"]


def test_space_pins_one_container(upsert):
    upsert(KINDED)

    assert sorted(files_of(server.search("acme", space="yt:INF"))) == ["yt/INF-42.md", "yt/INF-A-1.md"]


def test_filters_compose_with_a_date_range(upsert):
    upsert(KINDED)

    result = server.search("acme", space="yt:INF", since="2026-03-13")
    assert files_of(result) == ["yt/INF-42.md"]


def test_a_point_without_kinds_is_invisible_to_a_kind_filter(upsert):
    upsert(KINDED)

    assert "mm/legacy.md" in files_of(server.search("acme", keywords=["unlabelled"]))
    assert "mm/legacy.md" not in files_of(server.search("acme", space_kind="public"))
