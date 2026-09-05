import pytest
from qdrant_client.models import FieldCondition, Filter, MatchAny, MatchText, MatchValue

from sediment_mcp import server
from sediment_mcp.acl import Grant
from sediment_mcp.server import _build_filter, _ts_range


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


def test_bare_date_bounds_span_the_whole_day():
    r = _ts_range("2026-03-01", "2026-03-31")
    assert r is not None
    # 2026-03-01T00:00:00Z .. 2026-03-31T23:59:59Z
    assert (r.gte, r.lte) == (1772323200, 1775001599)


def test_timestamp_bounds_are_taken_as_given_and_default_to_utc():
    aware = _ts_range("2026-03-01T12:00:00Z", None)
    naive = _ts_range("2026-03-01T12:00:00", None)
    assert aware is not None and naive is not None
    assert aware.gte == naive.gte == 1772366400
    assert aware.lte is None


def test_open_ended_and_empty_ranges():
    assert _ts_range(None, None) is None
    until_only = _ts_range(None, "2026-03-01")
    assert until_only is not None and until_only.gte is None


def test_unparseable_date_is_rejected_loudly():
    with pytest.raises(ValueError, match="Invalid date"):
        _ts_range("March 2026", None)
    with pytest.raises(ValueError, match="Invalid date"):
        _ts_range("2026-13-01", None)


def test_inverted_range_is_rejected():
    with pytest.raises(ValueError, match="must not be later"):
        _ts_range("2026-03-31", "2026-03-01")


def test_build_filter_puts_ts_before_the_acl_condition():
    cond = restricted_grant().space_condition()
    qfilter = _build_filter(None, None, None, cond, _ts_range("2026-03-01", None))
    assert isinstance(qfilter, Filter) and isinstance(qfilter.must, list)
    ts_condition = qfilter.must[0]
    assert isinstance(ts_condition, FieldCondition)
    assert ts_condition.key == "ts" and ts_condition.range is not None
    assert qfilter.must[-1] is cond


def test_search_reports_a_bad_date_instead_of_querying():
    assert "Invalid date" in server.search("acme", query="x", since="March")
    assert "must not be later" in server.search("acme", query="x", since="2026-03-31", until="2026-03-01")


def test_search_needs_at_least_one_criterion():
    assert "Provide a query" in server.search("acme")


def test_search_rejects_unknown_kinds():
    assert "Unknown space_kind" in server.search("acme", query="x", space_kind="chanel")
    assert "Unknown doc_kind" in server.search("acme", query="x", doc_kind="ticket")


def test_build_filter_adds_exact_conditions_before_the_acl():
    cond = restricted_grant().space_condition()
    qfilter = _build_filter(None, None, None, cond, None, {"space_kind": "dm", "doc_kind": None})
    assert isinstance(qfilter, Filter) and isinstance(qfilter.must, list)
    kind_condition = qfilter.must[0]
    assert isinstance(kind_condition, FieldCondition)
    assert kind_condition.key == "space_kind"
    assert kind_condition.match == MatchValue(value="dm")
    assert qfilter.must[-1] is cond
