import json
from datetime import date

import pick
from schema import Post


def make(id, created_at, text="本文", handle="aichan_nel", like=0, repost=0, reply=0, quote=0,
         is_repost=False, article_text=None):
    return Post(
        id=id, url=f"https://x.com/{handle}/status/{id}", author_handle=handle, author_name="n",
        created_at=created_at, text=text, like_count=like, repost_count=repost,
        reply_count=reply, quote_count=quote, collected_at="2026-10-07T00:00:00+00:00",
        is_repost=is_repost, article_text=article_text,
    )


SEP = "Mon Sep 21 03:28:32 +0000 2026"


def test_parse_created_at_with_year():
    parsed = pick.parse_created_at(SEP)
    assert (parsed.year, parsed.month, parsed.day) == (2026, 9, 21)
    assert pick.parse_created_at("garbage") is None


def test_jst_conversion_crosses_day():
    post = make("1", "Mon Sep 21 20:00:00 +0000 2026")
    assert pick.to_jst(post).date() == date(2026, 9, 22)


def test_engagement():
    assert pick.engagement(make("1", SEP, like=1, repost=2, reply=3, quote=4)) == 10


def test_keyword_or_case_insensitive_and_article():
    posts = [
        make("1", SEP, text="VRChat更新"),
        make("2", SEP, text="無関係", article_text="Shader の話"),
        make("3", SEP, text="なし"),
    ]
    result = pick.filter_posts(posts, keywords=["vrchat", "SHADER"])
    assert [p.id for p in result] == ["1", "2"]


def test_filter_handle_reposts_and_dates():
    posts = [
        make("1", "Thu Jan 01 03:00:00 +0000 2026"),
        make("2", "Sun Mar 15 03:00:00 +0000 2026", is_repost=True),
        make("3", "Sun Mar 15 03:00:00 +0000 2026", handle="other"),
        make("4", "Tue Dec 01 03:00:00 +0000 2026"),
    ]
    assert [p.id for p in pick.filter_posts(posts, handle="@AICHAN_NEL")] == ["1", "2", "4"]
    assert [p.id for p in pick.filter_posts(posts, exclude_reposts=True)] == ["1", "3", "4"]
    assert [p.id for p in pick.filter_posts(
        posts, since=date(2026, 2, 1), until=date(2026, 3, 15))] == ["2", "3"]


def test_select_top_per_month():
    posts = [
        make("a", "Thu Jan 01 03:00:00 +0000 2026", like=5),
        make("b", "Fri Jan 02 03:00:00 +0000 2026", like=9),
        make("c", "Sun Feb 01 03:00:00 +0000 2026", like=1),
    ]
    assert [p.id for p in pick.select_top(posts, "month", 1)] == ["b", "c"]
    assert [p.id for p in pick.select_top(posts, "none", 2)] == ["a", "b"]
    assert [p.id for p in pick.select_top(posts, "none", None)] == ["a", "b", "c"]


def test_load_posts_dedupes_and_skips_non_posts(tmp_path):
    f = tmp_path / "a.jsonl"
    rec = make("1", SEP).to_dict()
    f.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in [rec, rec, {"trend_word": "x"}]) + "\n",
        encoding="utf-8",
    )
    assert [p.id for p in pick.load_posts([str(f)])] == ["1"]


def test_render_markdown():
    post = make("1", SEP, text="あ" * 150, like=2)
    out = pick.render_markdown([post])
    assert "## 2026-09" in out
    assert "2026-09-21" in out
    assert "エンゲージメント2" in out
    assert "あ" * 100 + "…" in out
    assert "あ" * 101 not in out
    assert post.url in out


def _with_source(post, source_query):
    post.source_query = source_query
    return post


def test_post_kind_from_related_source_query():
    assert pick.post_kind(_with_source(make("1", SEP), "related:123:reply")) == "reply"
    assert pick.post_kind(_with_source(make("1", SEP), "related:123:quote")) == "quote"
    assert pick.post_kind(_with_source(make("1", SEP), "ig-related:AbC_-1:comment")) == "comment"
    assert pick.post_kind(_with_source(make("1", SEP), "th-related:AbC:quote")) == "quote"
    # 関連収集以外のsource_query(検索クエリ等)は末尾が一致しても種別なし
    assert pick.post_kind(_with_source(make("1", SEP), "from:foo reply")) is None
    assert pick.post_kind(_with_source(make("1", SEP), "q since:reply")) is None
    assert pick.post_kind(make("1", SEP)) is None


def test_filter_by_kinds():
    posts = [
        _with_source(make("1", SEP), "related:9:reply"),
        _with_source(make("2", SEP), "related:9:quote"),
        _with_source(make("3", SEP), "ig-related:Ab:comment"),
        _with_source(make("4", SEP), "VRChat"),
    ]
    assert [p.id for p in pick.filter_posts(posts, kinds=["reply", "comment"])] == ["1", "3"]
    assert [p.id for p in pick.filter_posts(posts, kinds=[])] == ["1", "2", "3", "4"]


def test_kind_option_parses_multiple():
    args = pick.build_arg_parser().parse_args(["a.jsonl", "--kind", "reply", "--kind", "quote"])
    assert args.kind == ["reply", "quote"]
