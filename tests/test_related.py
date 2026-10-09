"""related.pyの純関数(引用検索タブ・出力パス・上位セクション・引数既定値)の単体テスト。合成データのみ。"""
from pathlib import Path

import related
from schema import Post


def make(id, like, handle="alice", in_reply_to=None, quoted=None):
    return Post(
        id=id, url=f"https://x.com/{handle}/status/{id}", author_handle=handle, author_name=handle,
        created_at="Fri Oct 09 03:00:00 +0000 2026", text="本文", like_count=like, repost_count=0,
        reply_count=0, quote_count=0, collected_at="2026-10-09T00:00:00+09:00",
        quoted_id=quoted, in_reply_to_id=in_reply_to,
    )


def test_quote_search_modes():
    assert related.quote_search_modes("popular") == ["top", "live"]
    assert related.quote_search_modes("recent") == ["live"]


def test_arg_defaults_are_popular_top10():
    args = related.build_arg_parser().parse_args(["--id", "1"])
    assert args.sort == "popular"
    assert args.top == 10


def test_next_related_out_path_with_prefix(tmp_path: Path):
    (tmp_path / "related_20261009_004.jsonl").write_text("")
    (tmp_path / "ig_related_20261009_001.jsonl").write_text("")
    assert related.next_related_out_path(tmp_path, "20261009") == tmp_path / "related_20261009_005.jsonl"
    assert related.next_related_out_path(tmp_path, "20261009", prefix="ig_related") == (
        tmp_path / "ig_related_20261009_002.jsonl"
    )
    assert related.next_related_out_path(tmp_path, "20261009", prefix="th_related") == (
        tmp_path / "th_related_20261009_001.jsonl"
    )


def test_build_x_top_sections_direct_only_and_self_excluded():
    posts = [
        make("2", 30, in_reply_to="1"),
        make("3", 99, in_reply_to="2"),                 # 返信への返信
        make("4", 50, handle="origin", in_reply_to="1"),  # 本人の返信
        make("5", 10, quoted="1"),
        make("6", 80, handle="origin", quoted="1"),       # 自己引用
        make("7", 20, in_reply_to="1"),
    ]
    sections = related.build_x_top_sections(posts, "1", "origin", {"reply", "quote", "author"}, top=10)
    assert [(label, [p.id for p in ranked], total) for label, ranked, total in sections] == [
        ("リプライ", ["2", "7"], 2),
        ("引用", ["5"], 1),
    ]


def test_build_x_top_sections_only_collected_relations():
    sections = related.build_x_top_sections([], "1", None, {"quote"}, top=10)
    assert [label for label, _, _ in sections] == ["引用"]
    assert related.build_x_top_sections([], "1", None, {"author"}, top=10) == []


def test_quote_scroll_script_popular_scrolls_to_bottom():
    from capture import SCROLL_SCRIPT

    assert related.quote_scroll_script("popular") == related.QUOTE_SEARCH_SCROLL_SCRIPT
    assert "scrollHeight" in related.QUOTE_SEARCH_SCROLL_SCRIPT
    assert related.quote_scroll_script("recent") == SCROLL_SCRIPT
