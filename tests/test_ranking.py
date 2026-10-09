"""ranking.pyのいいね数ランキング・Markdown生成の単体テスト(Playwright非依存・合成データ)。"""
from datetime import date
from pathlib import Path

import ranking
from schema import Post


def make(id, like, handle="alice", in_reply_to=None, quoted=None, text="本文",
         created_at="Fri Oct 09 03:00:00 +0000 2026", kind="reply"):
    return Post(
        id=id, url=f"https://example.com/{handle}/{id}", author_handle=handle, author_name=handle,
        created_at=created_at, text=text, like_count=like, repost_count=0, reply_count=0,
        quote_count=0, collected_at="2026-10-09T00:00:00+09:00", quoted_id=quoted,
        in_reply_to_id=in_reply_to, source_query=f"related:1:{kind}",
    )


def is_reply(post):
    return post.in_reply_to_id == "1"


def test_rank_by_likes_sorts_desc_and_limits():
    posts = [make("2", 5, in_reply_to="1"), make("3", 50, in_reply_to="1"),
             make("4", 10, in_reply_to="1"), make("5", 1, in_reply_to="1")]
    ranked = ranking.rank_by_likes(posts, is_reply, origin_id="1", origin_handle="origin", top=3)
    assert [p.id for p in ranked] == ["3", "4", "2"]


def test_rank_by_likes_keeps_collection_order_on_tie():
    posts = [make("2", 7, in_reply_to="1"), make("3", 7, in_reply_to="1")]
    ranked = ranking.rank_by_likes(posts, is_reply, origin_id="1", origin_handle="origin", top=10)
    assert [p.id for p in ranked] == ["2", "3"]


def test_rank_by_likes_excludes_nested_origin_and_self():
    posts = [
        make("1", 999, handle="origin"),                      # 起点自身
        make("2", 100, handle="Origin", in_reply_to="1"),     # 起点投稿者本人の返信(大文字小文字無視)
        make("3", 50, in_reply_to="2"),                       # 返信への返信
        make("4", 10, in_reply_to="1"),
    ]
    ranked = ranking.rank_by_likes(posts, is_reply, origin_id="1", origin_handle="@origin", top=10)
    assert [p.id for p in ranked] == ["4"]


def test_rank_by_likes_without_origin_handle_keeps_all_targets():
    posts = [make("2", 1, handle="origin", in_reply_to="1")]
    ranked = ranking.rank_by_likes(posts, is_reply, origin_id="1", origin_handle=None, top=10)
    assert [p.id for p in ranked] == ["2"]


def test_render_line_has_rank_likes_author_jst_summary_url():
    long_text = "あ" * 150
    post = make("2", 1234, handle="bob", in_reply_to="1", text=long_text,
                created_at="Fri Oct 09 20:30:00 +0000 2026")
    line = ranking.render_line(1, post)
    assert line.startswith("1. いいね 1234 / @bob / 2026-10-10 05:30 / ")
    assert "あ" * 100 + "…" in line
    assert "あ" * 101 not in line
    assert line.endswith(" / https://example.com/bob/2")


def test_render_line_iso_created_at_and_newlines_flattened():
    post = make("2", 3, text="一行目\n二行目", created_at="2026-10-09T15:00:00+00:00")
    line = ranking.render_line(2, post)
    assert "2026-10-10 00:00" in line
    assert "一行目 二行目" in line


def test_render_top_markdown_sections():
    reply = make("2", 9, in_reply_to="1")
    text = ranking.render_top_markdown(
        title="X 人気リプライ・引用 上位10件",
        origin_url="https://x.com/origin/status/1",
        origin_handle="origin",
        generated_at="2026-10-09T12:00:00+09:00",
        sections=[("リプライ", [reply], 5), ("引用", [], 0)],
    )
    assert text.startswith("# X 人気リプライ・引用 上位10件\n")
    assert "https://x.com/origin/status/1" in text
    assert "## リプライ(対象5件中 上位1件)" in text
    assert "1. いいね 9 / @alice" in text
    assert "## 引用(対象0件中 上位0件)" in text
    assert "(該当なし)" in text


def test_top_markdown_path(tmp_path: Path):
    path = ranking.top_markdown_path(tmp_path, "x", "123", today=date(2026, 10, 9))
    assert path == tmp_path / "top_x_123_20261009.md"


def test_write_top_markdown_creates_dir_and_overwrites(tmp_path: Path):
    path = tmp_path / "sub" / "top.md"
    ranking.write_top_markdown(path, "old")
    ranking.write_top_markdown(path, "new")
    assert path.read_text(encoding="utf-8") == "new"


def test_build_sections_counts_and_ranks():
    posts = [make("2", 5, in_reply_to="1"), make("3", 8, in_reply_to="1"),
             make("4", 9, quoted="1", kind="quote"), make("5", 1, in_reply_to="2")]
    sections = ranking.build_sections(
        posts,
        [("リプライ", lambda p: p.in_reply_to_id == "1"), ("引用", lambda p: p.quoted_id == "1")],
        origin_id="1", origin_handle="origin", top=1,
    )
    assert [(label, [p.id for p in ranked], total) for label, ranked, total in sections] == [
        ("リプライ", ["3"], 2),
        ("引用", ["4"], 1),
    ]


def test_render_top_markdown_notes():
    text = ranking.render_top_markdown(
        title="t", origin_url="u", origin_handle=None, generated_at="g",
        sections=[], notes=["収集は途中で中断されました"],
    )
    assert "- 起点投稿者: 不明(本人除外なし)" in text
    assert "- 収集は途中で中断されました" in text
