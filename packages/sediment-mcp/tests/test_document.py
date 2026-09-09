from sediment_mcp.access import FileAccess
from knowledge_schema import CHUNK_OVERLAP

from sediment_mcp import server
from sediment_mcp.acl import Acl
from sediment_mcp.server import _stitch, get_document


def chunked(paragraphs: list[str]) -> list[str]:
    """Chunks shaped the way the loader shapes them: each one carries the
    previous chunk's last CHUNK_OVERLAP characters."""
    chunks = [paragraphs[0]]
    for para in paragraphs[1:]:
        chunks.append(chunks[-1][-CHUNK_OVERLAP:] + "\n\n" + para)
    return chunks


def test_stitch_removes_carried_over_overlap():
    paragraphs = ["A" * 300, "B" * 300, "C" * 300]
    assert _stitch(chunked(paragraphs)) == "\n\n".join(paragraphs)


def test_stitch_keeps_text_when_chunks_do_not_overlap():
    assert _stitch(["first", "second"]) == "first\n\nsecond"


def test_stitch_does_not_trim_a_short_coincidental_match():
    # both chunks start and end with "." — a greedy longest-match would eat it
    assert _stitch(["a.", ".b"]) == "a.\n\n.b"


def test_stitch_survives_a_gap_in_chunks():
    paragraphs = ["A" * 300, "B" * 300, "C" * 300]
    first, _, third = chunked(paragraphs)
    assert _stitch([first, third]) == f"{first}\n\n{third}"


def document_points(paragraphs: list[str], file: str = "mattermost/apps__c8/2026-03.md"):
    return [
        {
            "text": text,
            "source": "mattermost",
            "file": file,
            "title": "March",
            "chunk_index": i,
        }
        for i, text in enumerate(chunked(paragraphs))
    ]


def test_returns_whole_document_in_chunk_order(upsert):
    paragraphs = ["A" * 300, "B" * 300, "C" * 300]
    points = document_points(paragraphs)
    upsert([points[2], points[0], points[1]])  # scroll order is not chunk order

    out = get_document("acme", "mattermost/apps__c8/2026-03.md")

    assert out.startswith("mattermost: mattermost/apps__c8/2026-03.md — chunks 0-2 of 3\nMarch\n\n")
    assert out.endswith("\n\n".join(paragraphs))


def test_manual_entry_without_chunk_index_is_reachable(upsert):
    upsert([{"text": "a note", "source": "manual", "file": "notes/x"}])

    out = get_document("acme", "notes/x")

    assert out.endswith("a note")


def test_unknown_file_points_back_at_search(upsert):
    upsert(document_points(["A" * 100]))

    out = get_document("acme", "mattermost/apps__c8/2026-04.md")

    assert "search(filename=...)" in out


def test_long_document_is_truncated_and_resumable(upsert, monkeypatch):
    monkeypatch.setattr(server, "MAX_DOCUMENT_CHARS", 800)
    paragraphs = ["A" * 300, "B" * 300, "C" * 300]
    upsert(document_points(paragraphs))

    first = get_document("acme", "mattermost/apps__c8/2026-03.md")
    assert "chunks 0-1 of 3" in first
    assert "[Truncated. Continue with from_chunk=2.]" in first

    rest = get_document("acme", "mattermost/apps__c8/2026-03.md", from_chunk=2)
    assert "chunks 2-2 of 3" in rest
    assert "Truncated" not in rest


def test_from_chunk_past_the_end_says_so(upsert):
    upsert(document_points(["A" * 100]))

    out = get_document("acme", "mattermost/apps__c8/2026-03.md", from_chunk=50)

    assert "No chunks at or after index 50" in out


def test_rejects_out_of_range_from_chunk(qdrant):
    assert "from_chunk must be" in get_document("acme", "notes/x", from_chunk=-1)


def test_acl_hides_a_document_outside_the_granted_spaces(upsert, monkeypatch):
    acl = Acl(
        {
            "grants": [
                {"users": ["carol"], "collections": ["acme"], "spaces": ["mm:visible"]},
            ]
        }
    )
    monkeypatch.setattr(server, "ACCESS", FileAccess(acl))
    monkeypatch.setattr(server, "current_principal", lambda: "carol")
    upsert(
        [
            {"text": "ours", "source": "mattermost", "file": "mm/ours.md", "space": "mm:visible", "chunk_index": 0},
            {"text": "theirs", "source": "mattermost", "file": "mm/theirs.md", "space": "mm:other", "chunk_index": 0},
        ]
    )

    assert get_document("acme", "mm/ours.md").endswith("ours")
    assert "No document" in get_document("acme", "mm/theirs.md")
    assert "not accessible" in get_document("globex", "mm/ours.md")


def test_other_collection_wildcard_does_not_bypass_document_or_search_filter(upsert, monkeypatch):
    acl = Acl({"grants": [
        {"users": ["carol"], "collections": ["globex"], "spaces": ["*"], "unrestricted": True},
        {"users": ["carol"], "collections": ["acme"], "spaces": ["mm:visible"]},
    ]})
    monkeypatch.setattr(server, "ACCESS", FileAccess(acl))
    monkeypatch.setattr(server, "current_principal", lambda: "carol")
    upsert([
        {"text": "visible needle", "source": "mattermost", "file": "mm/ours.md", "space": "mm:visible", "chunk_index": 0},
        {"text": "private needle", "source": "mattermost", "file": "mm/theirs.md", "space": "mm:other", "chunk_index": 0},
    ])
    assert "No document" in get_document("acme", "mm/theirs.md")
    found = server.search("acme", keywords=['"needle"'])
    assert "visible needle" in found
    assert "private needle" not in found
