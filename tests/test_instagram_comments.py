"""instagram.pyのコメント正規化・shortcode変換・投稿URL解釈の単体テスト。

Playwright非依存。ユーザー名・本文・ID・URLはすべて架空値の合成データ。
"""
import pytest

from instagram import (
    InstagramCapture,
    parse_instagram_comments,
    parse_instagram_post_url,
    pk_to_shortcode,
    shortcode_to_pk,
)

MEDIA_PK = "1000000000000000001"
CODE = "AbC-_123xyz"


def _comment(pk, like, username="fan_a", parent=None, child_count=0):
    return {
        "pk": pk,
        "created_at": 1767225600,  # 2026-01-01T00:00:00Z
        "text": f"コメント{pk}",
        "comment_like_count": like,
        "child_comment_count": None if parent else child_count,
        "parent_comment_id": parent,
        "user": {"id": "9", "pk": "9", "username": username, "is_verified": False},
        "__typename": "XDTCommentDict",
    }


def _connection_payload(*nodes):
    return {
        "data": {
            "xdt_api__v1__media__media_id__comments__connection": {
                "edges": [{"node": n} for n in nodes],
                "page_info": {"end_cursor": "c1", "has_next_page": True},
            }
        }
    }


def test_shortcode_pk_known_values():
    assert shortcode_to_pk("A") == "0"
    assert shortcode_to_pk("B") == "1"
    assert shortcode_to_pk("BA") == "64"
    assert shortcode_to_pk("_") == "63"
    assert pk_to_shortcode("64") == "BA"
    assert pk_to_shortcode(63) == "_"


def test_shortcode_pk_roundtrip():
    for pk in ("1", "4095", "1000000000000000001", "3999999999999999999"):
        assert shortcode_to_pk(pk_to_shortcode(pk)) == pk


def test_shortcode_to_pk_rejects_invalid():
    with pytest.raises(ValueError):
        shortcode_to_pk("ab!c")
    with pytest.raises(ValueError):
        shortcode_to_pk("")


def test_parse_instagram_post_url():
    assert parse_instagram_post_url(f"https://www.instagram.com/p/{CODE}/") == CODE
    assert parse_instagram_post_url(f"https://www.instagram.com/reel/{CODE}/?igsh=x") == CODE
    assert parse_instagram_post_url(f"https://instagram.com/some_user/p/{CODE}/") == CODE
    with pytest.raises(ValueError):
        parse_instagram_post_url("https://www.instagram.com/some_user/")


def test_parse_comments_top_level_gets_media_pk():
    posts = parse_instagram_comments(
        _connection_payload(_comment("11", 5), _comment("12", 40, username="fan_b", child_count=2)),
        "api_graphql", media_pk=MEDIA_PK, code=CODE,
        source_query=f"ig-related:{CODE}:comment", collected_at="2026-10-09T00:00:00+09:00",
    )
    assert [p.id for p in posts] == ["11", "12"]
    second = posts[1]
    assert second.url == f"https://www.instagram.com/p/{CODE}/c/12/"
    assert second.author_handle == "fan_b"
    assert second.like_count == 40
    assert second.reply_count == 2
    assert second.repost_count == 0 and second.quote_count == 0
    assert second.in_reply_to_id == MEDIA_PK
    assert second.created_at == "2026-01-01T00:00:00+00:00"
    assert second.text == "コメント12"
    assert second.source_query == f"ig-related:{CODE}:comment"


def test_parse_comments_child_keeps_parent_id():
    posts = parse_instagram_comments(
        _connection_payload(_comment("21", 1, parent="11")), "api_graphql",
        media_pk=MEDIA_PK, code=CODE,
    )
    assert posts[0].in_reply_to_id == "11"
    assert posts[0].reply_count == 0


def test_parse_comments_skips_missing_user_and_ignores_media_nodes(caplog):
    broken = _comment("31", 3)
    broken["user"] = {}
    payload = _connection_payload(broken, _comment("32", 4))
    payload["data"]["xdt_api__v1__media__shortcode__web_info"] = {
        "items": [{"pk": MEDIA_PK, "code": CODE, "taken_at": 1, "comment_count": 2}]
    }
    posts = parse_instagram_comments(payload, "embedded", media_pk=MEDIA_PK, code=CODE)
    assert [p.id for p in posts] == ["32"]
    assert "username" in caplog.text


def test_instagram_capture_inject_comes_first_in_drain():
    capture = InstagramCapture()
    capture.inject("embedded", {"a": 1})
    assert capture.drain() == [("embedded", {"a": 1})]
    assert capture.drain() == []
