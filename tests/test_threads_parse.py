"""threads.pyのエンドポイント判定・投稿正規化ロジックの単体テスト。

Playwright非依存。Threads内部API/ページ埋め込みJSONの構造を模したサンプルJSONを使う。
"""
from threads import (
    ThreadsCapture,
    extract_threads_endpoint_name,
    has_threads_session_cookie,
    is_threads_challenge_url,
    parse_threads_payload,
)


def _post(pk: str, code: str, username: str = "brand", **overrides) -> dict:
    node = {
        "pk": pk,
        "id": f"{pk}_111",
        "code": code,
        "taken_at": 1767225600,  # 2026-01-01T00:00:00Z
        "media_type": 19,  # テキストのみ投稿
        "user": {"pk": "111", "username": username, "full_name": "Brand Official"},
        "caption": {"text": "新作リップの色、どれが好き?"},
        "like_count": 250,
        "text_post_app_info": {
            "direct_reply_count": 12,
            "repost_count": 3,
            "quote_count": 1,
            "share_info": {"quoted_post": None, "reposted_post": None},
        },
    }
    node.update(overrides)
    return node


def _feed_payload(*thread_items_list: list) -> dict:
    return {
        "data": {
            "mediaData": {
                "edges": [
                    {"node": {"thread_items": [{"post": p} for p in items]}}
                    for items in thread_items_list
                ]
            }
        }
    }


def test_extract_threads_endpoint_name():
    assert extract_threads_endpoint_name("https://www.threads.com/graphql/query") == "graphql_query"
    assert extract_threads_endpoint_name("https://www.threads.net/api/graphql") == "api_graphql"
    assert extract_threads_endpoint_name("https://www.instagram.com/graphql/query") is None
    assert extract_threads_endpoint_name("https://www.threads.com/@brand") is None


def test_parse_basic_post():
    posts = parse_threads_payload(
        _feed_payload([_post("3600000000000000001", "TH001")]),
        "graphql_query", source_query="threads:brand",
        collected_at="2026-09-30T00:00:00+09:00", target_handle="brand",
    )
    assert len(posts) == 1
    post = posts[0]
    assert post.id == "3600000000000000001"
    assert post.url == "https://www.threads.com/@brand/post/TH001"
    assert (post.author_handle, post.author_name) == ("brand", "Brand Official")
    assert post.created_at == "2026-01-01T00:00:00+00:00"
    assert post.text == "新作リップの色、どれが好き?"
    assert (post.like_count, post.reply_count, post.repost_count, post.quote_count) == (250, 12, 3, 1)
    assert post.media == []
    assert post.is_repost is False
    assert post.quoted_id is None
    assert post.source_query == "threads:brand"


def test_thread_chain_yields_each_item_and_quoted_post_is_not_duplicated():
    quoted = _post("3600000000000000009", "QUOTED", username="someone")
    first = _post("3600000000000000002", "TH002")
    first["text_post_app_info"]["share_info"]["quoted_post"] = quoted
    second = _post("3600000000000000003", "TH003", caption={"text": "続き"})
    posts = parse_threads_payload(_feed_payload([first, second]), "embedded", target_handle="brand")
    assert [p.id for p in posts] == ["3600000000000000002", "3600000000000000003"]
    assert posts[0].quoted_id == "3600000000000000009"


def test_repost_by_target_is_marked():
    other = _post("3600000000000000004", "OTHER1", username="someone")
    posts = parse_threads_payload(_feed_payload([other]), "graphql_query", target_handle="Brand")
    assert posts[0].author_handle == "someone"
    assert posts[0].is_repost is True


def test_media_and_missing_fields():
    photo = _post(
        "3600000000000000005", "PHOTO1", media_type=1, caption=None,
        image_versions2={"candidates": [{"url": "https://cdn/p.jpg"}]},
    )
    no_user = _post("3600000000000000006", "NOUSER")
    del no_user["user"]
    posts = parse_threads_payload(_feed_payload([photo], [no_user]), "graphql_query")
    assert [p.id for p in posts] == ["3600000000000000005"]
    assert posts[0].text == ""
    assert [(m.type, m.url) for m in posts[0].media] == [("photo", "https://cdn/p.jpg")]


def test_instagram_style_node_without_text_post_app_info_is_ignored():
    node = _post("3600000000000000007", "IGONLY")
    del node["text_post_app_info"]
    assert parse_threads_payload(_feed_payload([node]), "graphql_query") == []


def test_capture_inject_is_drained_once():
    capture = ThreadsCapture()
    capture.inject("embedded", {"a": 1})
    assert capture.drain() == [("embedded", {"a": 1})]
    assert capture.drain() == []


def test_session_cookie_and_challenge_url():
    assert has_threads_session_cookie([{"name": "sessionid", "value": "x", "domain": ".threads.com"}])
    assert has_threads_session_cookie([{"name": "sessionid", "value": "x", "domain": ".threads.net"}])
    assert not has_threads_session_cookie([{"name": "sessionid", "value": "x", "domain": ".instagram.com"}])
    assert is_threads_challenge_url("https://www.threads.com/challenge/?next=/")
    assert not is_threads_challenge_url("https://www.threads.com/@brand")
