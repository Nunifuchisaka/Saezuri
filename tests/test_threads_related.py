"""threads.pyの返信・引用一覧の正規化と投稿URL解釈の単体テスト。

Playwright非依存。ユーザー名・本文・ID・URLはすべて架空値の合成データ。
"""
import pytest

from instagram import pk_to_shortcode
from threads import (
    find_threads_post,
    parse_feedback_quote_items,
    parse_threads_post_url,
    parse_threads_replies,
)

ORIGIN_PK = "3600000000000000001"
ORIGIN_CODE = "ORIGIN01"


def _node(pk, code, username, like=0, reply_level=None, reply_to=None, is_reply=None, **info_extra):
    info = {
        "direct_reply_count": 0,
        "repost_count": 0,
        "quote_count": 0,
        "share_info": {"quoted_post": None},
    }
    if is_reply is not None:
        info["is_reply"] = is_reply
    if reply_level is not None:
        info["reply_level"] = reply_level
    if reply_to is not None:
        info["reply_to_author"] = {"id": "1", "username": reply_to}
    info.update(info_extra)
    return {
        "pk": pk,
        "id": f"{pk}_1",
        "code": code,
        "taken_at": 1767225600,
        "user": {"pk": "1", "username": username},
        "caption": {"text": f"本文{pk}"},
        "like_count": like,
        "text_post_app_info": info,
    }


def _replies_payload(*threads):
    """direct_replies.edges[].node.posts.edges[].node の形(1スレッド=直接返信+孫返信)。"""
    return {
        "data": {
            "media": {
                "id": ORIGIN_PK,
                "text_post_app_info": {
                    "direct_replies": {
                        "edges": [
                            {"node": {"posts": {"edges": [{"node": n} for n in nodes]}}}
                            for nodes in threads
                        ],
                        "page_info": {"end_cursor": None, "has_next_page": False},
                    }
                },
            }
        }
    }


def test_parse_threads_post_url():
    assert parse_threads_post_url("https://www.threads.com/@some_user/post/AbC-_1?x=1") == ("some_user", "AbC-_1")
    assert parse_threads_post_url("https://www.threads.net/@u.s.e.r/post/Code9/") == ("u.s.e.r", "Code9")
    with pytest.raises(ValueError):
        parse_threads_post_url("https://www.threads.com/@some_user")


def test_parse_replies_direct_vs_nested():
    payload = _replies_payload(
        [
            _node("11", "R11", "fan_a", like=5, reply_level=1, reply_to="origin_user", is_reply=True),
            _node("12", "R12", "fan_b", like=9, reply_level=2, reply_to="fan_a", is_reply=True),
        ],
        [_node("13", "R13", "Origin_User", like=1, reply_level=1, reply_to="origin_user", is_reply=True)],
    )
    posts = parse_threads_replies(
        payload, "api_graphql", origin_pk=ORIGIN_PK, origin_handle="origin_user",
        source_query=f"th-related:{ORIGIN_CODE}:reply", collected_at="2026-10-09T00:00:00+09:00",
    )
    by_id = {p.id: p for p in posts}
    assert set(by_id) == {"11", "12", "13"}
    assert by_id["11"].in_reply_to_id == ORIGIN_PK
    assert by_id["12"].in_reply_to_id is None
    assert by_id["13"].in_reply_to_id == ORIGIN_PK
    assert by_id["11"].like_count == 5
    assert by_id["11"].is_repost is False
    assert by_id["11"].url == "https://www.threads.com/@fan_a/post/R11"
    assert by_id["11"].source_query == f"th-related:{ORIGIN_CODE}:reply"


def test_parse_replies_skips_origin_and_non_replies():
    origin = _node(ORIGIN_PK, ORIGIN_CODE, "origin_user", is_reply=False)
    other = _node("21", "OTHER", "someone", is_reply=False)
    reply = _node("22", "R22", "fan_c", reply_level=1, reply_to="origin_user", is_reply=True)
    payload = {"a": [{"post": origin}, {"post": other}], "b": _replies_payload([reply])}
    posts = parse_threads_replies(payload, "embedded", origin_pk=ORIGIN_PK, origin_handle="origin_user")
    assert [p.id for p in posts] == ["22"]


def test_parse_replies_reply_to_other_author_is_not_direct():
    reply = _node("31", "R31", "fan_d", reply_level=1, reply_to="someone_else", is_reply=True)
    posts = parse_threads_replies(_replies_payload([reply]), "x", origin_pk=ORIGIN_PK, origin_handle="origin_user")
    assert posts[0].in_reply_to_id is None


def test_parse_replies_descends_into_direct_replies_of_post_node():
    """起点ノード自体がdirect_repliesを抱えている形でも返信を取りこぼさない。"""
    origin = _node(ORIGIN_PK, ORIGIN_CODE, "origin_user", is_reply=False)
    origin["text_post_app_info"]["direct_replies"] = _replies_payload(
        [_node("41", "R41", "fan_e", reply_level=1, reply_to="origin_user", is_reply=True)]
    )["data"]["media"]["text_post_app_info"]["direct_replies"]
    posts = parse_threads_replies({"data": {"media": origin}}, "x", origin_pk=ORIGIN_PK, origin_handle="origin_user")
    assert [p.id for p in posts] == ["41"]


def _quote_item(media_pk, username, shortcode=True, icon="quote"):
    dest = f"media?id={media_pk}_99"
    if shortcode:
        dest += f"&shortcode={pk_to_shortcode(media_pk)}"
    return {
        "pk": media_pk,
        "destination": dest,
        "timestamp": 1767225600.0,
        "__typename": "XDTFeedbackHubItem",
        "extra": {"icon_name": icon, "content": "抜粋", "context": "表示名", "title": None},
        "actor": {"id": "99", "pk": "99", "username": username},
    }


def test_parse_feedback_quote_items():
    payload = {
        "data": {
            "feedback_hub_tab_items": {
                "edges": [
                    {"node": _quote_item("3600000000000000101", "quoter_a")},
                    {"node": _quote_item("3600000000000000102", "quoter_b", shortcode=False)},
                    {"node": _quote_item("3600000000000000103", "liker", icon="like")},
                ],
                "page_info": {"end_cursor": None, "has_next_page": False},
            }
        }
    }
    refs = parse_feedback_quote_items(payload, "graphql_query")
    assert [(r.pk, r.username) for r in refs] == [
        ("3600000000000000101", "quoter_a"),
        ("3600000000000000102", "quoter_b"),
    ]
    assert refs[0].shortcode == pk_to_shortcode("3600000000000000101")
    assert refs[1].shortcode == pk_to_shortcode("3600000000000000102")
    assert refs[0].url == f"https://www.threads.com/@quoter_a/post/{refs[0].shortcode}"


def test_parse_feedback_quote_items_from_post_activity():
    payload = {"data": {"feedback_hub": {"post_activity": [
        _quote_item("3600000000000000201", "quoter_c"),
        _quote_item("3600000000000000202", "liker", icon="like"),
    ]}}}
    assert [r.pk for r in parse_feedback_quote_items(payload, "graphql_query")] == ["3600000000000000201"]


def test_find_threads_post_by_code_and_pk():
    origin = _node(ORIGIN_PK, ORIGIN_CODE, "origin_user", like=7)
    reply = _node("51", "R51", "fan_f", reply_level=1, reply_to="origin_user", is_reply=True)
    payloads = [_replies_payload([reply]), {"data": {"media": origin}}]
    found = find_threads_post(payloads, code=ORIGIN_CODE)
    assert found is not None and found.id == ORIGIN_PK and found.like_count == 7
    assert find_threads_post(payloads, pk=ORIGIN_PK).author_handle == "origin_user"
    assert find_threads_post(payloads, code="NOPE") is None
