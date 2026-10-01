"""instagram.pyのエンドポイント判定・投稿正規化ロジックの単体テスト。

Playwright非依存。Instagram内部APIのレスポンス構造を模したサンプルJSONを使う。
"""
from instagram import (
    extract_endpoint_name,
    has_session_cookie,
    is_instagram_challenge_url,
    parse_instagram_payload,
    payload_requests_challenge,
)


def _v1_node(pk: str, code: str, **overrides) -> dict:
    node = {
        "pk": pk,
        "id": f"{pk}_111",
        "code": code,
        "taken_at": 1767225600,  # 2026-01-01T00:00:00Z
        "media_type": 1,
        "product_type": "feed",
        "user": {"pk": "111", "username": "brand", "full_name": "Brand Official"},
        "caption": {"text": "新色登場 #コスメ"},
        "like_count": 1200,
        "comment_count": 34,
        "image_versions2": {"candidates": [{"url": "https://scontent.cdninstagram.com/a.jpg"}]},
    }
    node.update(overrides)
    return node


def _timeline_payload(*nodes: dict) -> dict:
    return {
        "data": {
            "xdt_api__v1__feed__user_timeline_graphql_connection": {
                "edges": [{"node": n, "cursor": "c"} for n in nodes],
                "page_info": {"has_next_page": True},
            }
        }
    }


def test_extract_endpoint_name():
    assert extract_endpoint_name(
        "https://www.instagram.com/api/v1/users/web_profile_info/?username=brand"
    ) == "web_profile_info"
    assert extract_endpoint_name("https://www.instagram.com/graphql/query") == "graphql_query"
    assert extract_endpoint_name("https://www.instagram.com/api/v1/feed/user/111/") == "feed_user"
    assert extract_endpoint_name("https://www.instagram.com/brand/") is None
    assert extract_endpoint_name("https://x.com/i/api/graphql/abc/UserTweets") is None


def test_parse_v1_timeline_node():
    posts = parse_instagram_payload(
        _timeline_payload(_v1_node("3500000000000000001", "ABC123")),
        "graphql_query", source_query="instagram:brand", collected_at="2026-09-30T00:00:00+09:00",
    )
    assert len(posts) == 1
    post = posts[0]
    assert post.id == "3500000000000000001"
    assert post.url == "https://www.instagram.com/p/ABC123/"
    assert post.author_handle == "brand"
    assert post.author_name == "Brand Official"
    assert post.created_at == "2026-01-01T00:00:00+00:00"
    assert post.text == "新色登場 #コスメ"
    assert (post.like_count, post.reply_count, post.repost_count, post.quote_count) == (1200, 34, 0, 0)
    assert [(m.type, m.url) for m in post.media] == [("photo", "https://scontent.cdninstagram.com/a.jpg")]
    assert post.source_query == "instagram:brand"


def test_parse_v1_carousel_and_video_without_duplicating_children():
    carousel = _v1_node(
        "3500000000000000002", "CAR001", media_type=8,
        carousel_media=[
            {"pk": "c1", "media_type": 1,
             "image_versions2": {"candidates": [{"url": "https://cdn/1.jpg"}]}},
            {"pk": "c2", "media_type": 2,
             "video_versions": [{"url": "https://cdn/2.mp4"}],
             "image_versions2": {"candidates": [{"url": "https://cdn/2.jpg"}]}},
        ],
    )
    posts = parse_instagram_payload(_timeline_payload(carousel), "graphql_query")
    assert len(posts) == 1
    assert [(m.type, m.url) for m in posts[0].media] == [
        ("photo", "https://cdn/1.jpg"),
        ("video", "https://cdn/2.mp4"),
    ]


def test_parse_skips_story_and_uses_id_when_pk_missing():
    story = _v1_node("3500000000000000003", "STORY1", product_type="story")
    no_pk = _v1_node("ignored", "NOPK01")
    del no_pk["pk"]
    no_pk["id"] = "3500000000000000004_111"
    posts = parse_instagram_payload(_timeline_payload(story, no_pk), "graphql_query")
    assert [p.id for p in posts] == ["3500000000000000004"]


def test_parse_null_caption_and_hidden_likes():
    node = _v1_node("3500000000000000005", "NOCAP1", caption=None, like_count=None)
    posts = parse_instagram_payload(_timeline_payload(node), "graphql_query")
    assert posts[0].text == ""
    assert posts[0].like_count == 0


def test_parse_graph_profile_node_with_default_handle():
    payload = {
        "data": {
            "user": {
                "username": "brand",
                "edge_owner_to_timeline_media": {
                    "count": 1,
                    "edges": [{
                        "node": {
                            "__typename": "GraphSidecar",
                            "id": "3400000000000000001",
                            "shortcode": "GRAPH1",
                            "taken_at_timestamp": 1767225600,
                            "display_url": "https://cdn/cover.jpg",
                            "edge_media_to_caption": {"edges": [{"node": {"text": "限定セット"}}]},
                            "edge_liked_by": {"count": 500},
                            "edge_media_to_comment": {"count": 7},
                            "edge_sidecar_to_children": {"edges": [
                                {"node": {"shortcode": "CHILD1", "display_url": "https://cdn/c1.jpg"}},
                                {"node": {"shortcode": "CHILD2", "is_video": True,
                                          "video_url": "https://cdn/c2.mp4"}},
                            ]},
                        }
                    }],
                },
            }
        }
    }
    posts = parse_instagram_payload(payload, "web_profile_info", default_handle="brand")
    assert len(posts) == 1
    post = posts[0]
    assert (post.id, post.author_handle, post.text) == ("3400000000000000001", "brand", "限定セット")
    assert (post.like_count, post.reply_count) == (500, 7)
    assert [m.type for m in post.media] == ["photo", "video"]


def test_parse_skips_node_without_author():
    node = _v1_node("3500000000000000006", "NOUSER")
    del node["user"]
    assert parse_instagram_payload(_timeline_payload(node), "graphql_query") == []


def test_parse_unrelated_payload_returns_empty():
    assert parse_instagram_payload({"data": {"viewer": {"username": "me"}}}, "graphql_query") == []


def test_challenge_detection():
    assert payload_requests_challenge({"message": "checkpoint_required", "checkpoint_url": "/challenge/x"})
    assert not payload_requests_challenge({"status": "ok"})
    assert is_instagram_challenge_url("https://www.instagram.com/challenge/?next=/")
    assert not is_instagram_challenge_url("https://www.instagram.com/brand/")


def test_has_session_cookie():
    assert has_session_cookie([{"name": "sessionid", "value": "x", "domain": ".instagram.com"}])
    assert not has_session_cookie([{"name": "sessionid", "value": "", "domain": ".instagram.com"}])
    assert not has_session_cookie([{"name": "csrftoken", "value": "x", "domain": ".instagram.com"}])
