"""tiktok.py のコメント正規化と tt_related.py の純関数の単体テスト。合成データのみ。"""
import pytest

import pick
import tt_related
from tiktok import extract_tiktok_endpoint_name, parse_tiktok_comments, parse_tiktok_video_url

VIDEO_ID = "7000000000000000001"


def _comment(cid, *, likes=0, reply_id="0", aweme_id=VIDEO_ID, handle="fan_a", replies=0, **extra):
    node = {
        "cid": cid, "text": f"text {cid}", "create_time": 1700000000, "digg_count": likes,
        "reply_comment_total": replies, "reply_id": reply_id, "aweme_id": aweme_id,
        "comment_language": "ja", "user": {"unique_id": handle, "nickname": f"name {handle}"},
    }
    node.update(extra)
    return node


def test_parse_video_url():
    assert parse_tiktok_video_url(f"https://www.tiktok.com/@brand_x/video/{VIDEO_ID}?lang=ja") == (
        "brand_x", VIDEO_ID)
    assert parse_tiktok_video_url(f"https://www.tiktok.com/@brand_x/photo/{VIDEO_ID}")[1] == VIDEO_ID
    with pytest.raises(ValueError):
        parse_tiktok_video_url("https://www.tiktok.com/@brand_x")


def test_endpoint_comment_list_only_top_level():
    assert extract_tiktok_endpoint_name("https://www.tiktok.com/api/comment/list/?aweme_id=1") == "comment_list"
    assert extract_tiktok_endpoint_name("https://www.tiktok.com/api/comment/list/reply/?item_id=1") is None


def test_parse_comments_fields_and_reply_target():
    payload = {"comments": [
        _comment("101", likes=50, replies=3),
        _comment("102", likes=5, reply_id="101", handle="fan_b"),
        _comment("103", aweme_id="7000000000000000999"),  # 別動画は混入として除外
        {"cid": "104", "create_time": 1, "digg_count": 1, "user": {}},  # 投稿者不明はスキップ
    ], "cursor": 20, "has_more": 1, "total": 4}
    posts = parse_tiktok_comments(payload, "comment_list", video_id=VIDEO_ID, video_handle="brand_x",
                                  source_query="tt-related:x:comment", collected_at="2026-01-01T00:00:00+00:00")
    assert [p.id for p in posts] == ["101", "102"]
    top = posts[0]
    assert top.like_count == 50 and top.reply_count == 3 and top.in_reply_to_id == VIDEO_ID
    assert top.url == f"https://www.tiktok.com/@brand_x/video/{VIDEO_ID}?comment_id=101"
    assert top.author_handle == "fan_a" and top.author_name == "name fan_a" and top.lang == "ja"
    assert top.created_at.startswith("2023-11-14")
    assert posts[1].in_reply_to_id == "101"


def test_parse_comments_does_not_descend_into_reply_preview():
    payload = {"comments": [_comment("201", reply_comment=[_comment("202", reply_id="201")])]}
    posts = parse_tiktok_comments(payload, video_id=VIDEO_ID, video_handle="brand_x")
    assert [p.id for p in posts] == ["201"]


def test_source_query_and_pick_kind():
    assert tt_related.build_source_query(VIDEO_ID) == f"tt-related:{VIDEO_ID}:comment"
    post = parse_tiktok_comments({"comments": [_comment("301")]}, video_id=VIDEO_ID, video_handle="b",
                                 source_query=tt_related.build_source_query(VIDEO_ID))[0]
    assert pick.post_kind(post) == "comment"


def test_find_video_tab():
    class P:
        def __init__(self, url):
            self.url = url
    pages = [P("https://www.tiktok.com/@b"), P(f"https://www.tiktok.com/@b/video/{VIDEO_ID}?is_from_webapp=1")]
    assert tt_related.find_video_tab(pages, VIDEO_ID) is pages[1]
    assert tt_related.find_video_tab(pages[:1], VIDEO_ID) is None


def test_wait_for_video_tab_pumps_playwright_events():
    class P:
        def __init__(self, url):
            self.url = url
            self.waits = 0

        def wait_for_timeout(self, ms):
            self.waits += 1
            if self.waits == 2:
                ctx.pages.append(P(f"https://www.tiktok.com/@b/video/{VIDEO_ID}"))

    class Ctx:
        pages = [P("https://www.tiktok.com/@b")]

    ctx = Ctx()
    found = tt_related.wait_for_video_tab(ctx, VIDEO_ID, timeout_s=30, poll_s=0.01)
    assert found is ctx.pages[1]
    assert ctx.pages[0].waits == 2


def test_find_video_tab_matches_whole_id():
    class P:
        def __init__(self, url):
            self.url = url

    longer = P("https://www.tiktok.com/@example_t/video/12345")
    exact = P("https://www.tiktok.com/@example_t/video/1234?lang=ja")
    assert tt_related.find_video_tab([longer], "1234") is None
    assert tt_related.find_video_tab([longer, exact], "1234") is exact
