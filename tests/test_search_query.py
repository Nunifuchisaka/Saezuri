from datetime import date

import pytest

import search


def test_normalize_handle():
    assert search.normalize_handle("@aichan_nel") == "aichan_nel"


def test_build_query_all_parts():
    q = search.build_query("-filter:replies", "@aichan_nel", date(2026, 1, 1), date(2026, 2, 1))
    assert q == "-filter:replies from:aichan_nel since:2026-01-01 until:2026-02-01"


def test_build_query_without_query():
    assert search.build_query("", "x") == "from:x"


def test_build_search_query_unchanged():
    assert search.build_search_query("abc", None) == "abc"


def test_resolve_date_range_since_wins():
    today = date(2026, 10, 7)
    assert search.resolve_date_range(date(2026, 1, 1), None, 7, today) == (date(2026, 1, 1), None)
    assert search.resolve_date_range(None, None, 7, today) == (date(2026, 9, 30), None)


def test_split_windows():
    w = search.split_windows(date(2026, 1, 1), date(2026, 1, 11), 4)
    assert w == [
        (date(2026, 1, 1), date(2026, 1, 5)),
        (date(2026, 1, 5), date(2026, 1, 9)),
        (date(2026, 1, 9), date(2026, 1, 11)),
    ]
    assert search.split_windows(date(2026, 1, 5), date(2026, 1, 1), 3) == []
    with pytest.raises(ValueError):
        search.split_windows(date(2026, 1, 1), date(2026, 1, 2), 0)
