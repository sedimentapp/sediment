from qdrant_client.models import FieldCondition, Filter, MatchAny, MatchText, MatchValue

from sediment_mcp.acl import Grant
from sediment_mcp.server import _build_filter


def restricted_grant() -> Grant:
    return Grant(
        collections=frozenset({"acme"}),
        spaces=frozenset({"yt:INF", "manual:carol"}),
        write_collections=frozenset(),
        unrestricted_write_collections=frozenset(),
    )


def test_space_condition_shape():
    cond = restricted_grant().space_condition()
    assert isinstance(cond, Filter)
    assert cond.must is None
    assert cond.should is not None
    space_branch, visibility_branch = cond.should
    assert isinstance(space_branch, FieldCondition)
    assert space_branch.key == "space"
    assert isinstance(space_branch.match, MatchAny)
    assert space_branch.match.any == ["manual:carol", "yt:INF"]
    assert isinstance(visibility_branch, FieldCondition)
    assert visibility_branch.key == "visibility"
    assert visibility_branch.match == MatchValue(value="org")


def test_unrestricted_grant_has_no_space_condition():
    grant = Grant(
        collections=frozenset({"acme"}),
        spaces=None,
        write_collections=frozenset(),
        unrestricted_write_collections=frozenset(),
    )
    assert grant.space_condition() is None


def test_build_filter_without_acl_and_conditions_is_none():
    assert _build_filter(None, None, None, None) is None


def test_build_filter_acl_only_still_filters():
    cond = restricted_grant().space_condition()
    qfilter = _build_filter(None, None, None, cond)
    assert isinstance(qfilter, Filter)
    assert qfilter.must == [cond]


def test_build_filter_acl_composes_with_user_conditions():
    cond = restricted_grant().space_condition()
    qfilter = _build_filter(["10.0.0.1"], "youtrack", "VN-242", cond)
    assert isinstance(qfilter, Filter)
    assert isinstance(qfilter.must, list)
    keys = [c.key for c in qfilter.must if isinstance(c, FieldCondition)]
    assert keys == ["text_lc", "source", "file_lc"]
    assert qfilter.must[-1] is cond  # ACL is ANDed with (never weakened by) user filters


def _substring_conditions(qfilter: Filter | None) -> list[tuple[str, str]]:
    assert qfilter is not None and isinstance(qfilter.must, list)
    out = []
    for c in qfilter.must:
        assert isinstance(c, FieldCondition)
        assert isinstance(c.match, MatchText)
        out.append((c.key, c.match.text))
    return out


def test_substring_conditions_default_to_lowercase_shadow():
    qfilter = _build_filter(["YouTrack"], None, "VN-242", None)
    assert _substring_conditions(qfilter) == [("text_lc", "youtrack"), ("file_lc", "vn-242")]


def test_quoted_needle_forces_exact_case_on_original_field():
    qfilter = _build_filter(['"YouTrack"'], None, '"VN-242"', None)
    assert _substring_conditions(qfilter) == [("text", "YouTrack"), ("file", "VN-242")]


def test_lone_double_quote_is_not_treated_as_quoted():
    qfilter = _build_filter(['"'], None, None, None)
    assert _substring_conditions(qfilter) == [("text_lc", '"')]
