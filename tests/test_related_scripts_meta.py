"""ig_related.py / th_related.py の純関数・定数の単体テスト。合成データのみ。"""
import re

import ig_related
import th_related


def test_ig_source_query_and_args():
    assert ig_related.build_source_query("AbC_-1") == "ig-related:AbC_-1:comment"
    args = ig_related.build_arg_parser().parse_args(["--url", "https://www.instagram.com/p/AbC/"])
    assert args.top == 10 and args.max is None


def test_ig_find_origin_post_by_code():
    payload = {"data": {"xdt_api__v1__media__shortcode__web_info": {"items": [
        {"pk": "1000000000000000002", "code": "OTHER", "taken_at": 1, "user": {"username": "x"}},
        {"pk": "1000000000000000001", "code": "AbC", "taken_at": 1, "user": {"username": "owner"}},
    ]}}}
    origin = ig_related.find_origin_post([payload], "AbC")
    assert origin.id == "1000000000000000001"
    assert origin.author_handle == "owner"
    assert ig_related.find_origin_post([payload], "NONE") is None


def test_th_source_query_and_sorted_url():
    assert th_related.build_source_query("Code1", "reply") == "th-related:Code1:reply"
    assert th_related.build_source_query("Code1", "quote") == "th-related:Code1:quote"
    assert th_related.build_top_sorted_url("https://www.threads.com/@u/post/Code1") == (
        "https://www.threads.com/@u/post/Code1?sort_order=top"
    )


def test_th_quote_tab_pattern():
    pattern = re.compile(th_related.QUOTE_TAB_PATTERN)
    for text in ("引用", "引用\n1", "引用 1,234", "引用\n1.2万", "Quotes\n12", "Quote 3"):
        assert pattern.match(text), text
    for text in ("引用する", "再投稿\n12", "フォローする", "「いいね！」\n355"):
        assert not pattern.match(text), text


def test_th_merge_new_keeps_first():
    from schema import Post

    def make(id, sq):
        return Post(id=id, url="u", author_handle="a", author_name="a", created_at="", text="",
                    like_count=0, repost_count=0, reply_count=0, quote_count=0, collected_at="",
                    source_query=sq)

    collected = {}
    th_related.merge_new(collected, [make("1", "reply")])
    th_related.merge_new(collected, [make("1", "quote"), make("2", "quote")])
    assert {k: v.source_query for k, v in collected.items()} == {"1": "reply", "2": "quote"}
