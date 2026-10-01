"""tiktok.pyのエンドポイント判定・CAPTCHA検知・投稿正規化ロジックの単体テスト。

Playwright非依存。TikTok内部API(/api/post/item_list/)の構造を模した合成JSONを使う。
"""
import pytest

from capture import ChallengeDetectedError
from session import SessionExpiredError
from tiktok import (
    TikTokCapture,
    extract_tiktok_endpoint_name,
    has_tiktok_session_cookie,
    is_captcha_request_url,
    is_tiktok_challenge_url,
    is_tiktok_login_url,
    parse_tiktok_payload,
)

NOW = 1_800_000_000


def _item(vid: str = "7400000000000000001", unique_id: str = "brand", **overrides) -> dict:
    node = {
        "id": vid,
        "desc": "新作リップを試してみた #コスメ",
        "createTime": 1767225600,  # 2026-01-01T00:00:00Z
        "textLanguage": "ja",
        "author": {"id": "555", "uniqueId": unique_id, "nickname": "Brand Official"},
        "stats": {"diggCount": 1200, "shareCount": 30, "commentCount": 45, "playCount": 98765, "collectCount": 7},
        "video": {
            "id": vid,
            "cover": "https://cdn/cover.jpg",
            "originCover": "https://cdn/origin.jpg",
            "dynamicCover": "https://cdn/dynamic.webp",
            "playAddr": "https://cdn/play.mp4",
        },
    }
    node.update(overrides)
    return node


def _payload(*items: dict) -> dict:
    return {"statusCode": 0, "hasMore": True, "cursor": "1", "itemList": list(items)}


def test_extract_tiktok_endpoint_name():
    assert extract_tiktok_endpoint_name("https://www.tiktok.com/api/post/item_list/?aid=1988&count=35") == "item_list"
    assert extract_tiktok_endpoint_name("https://www.tiktok.com/api/user/detail/?uniqueId=brand") == "user_detail"
    assert extract_tiktok_endpoint_name("https://www.tiktok.com/api/repost/item_list/?x=1") is None
    assert extract_tiktok_endpoint_name("https://www.instagram.com/api/post/item_list/") is None
    assert extract_tiktok_endpoint_name("https://www.tiktok.com/@brand") is None


def test_parse_basic_post():
    posts = parse_tiktok_payload(
        _payload(_item()), "item_list", source_query="tiktok:brand",
        collected_at="2026-10-01T00:00:00+09:00",
    )
    assert len(posts) == 1
    post = posts[0]
    assert post.id == "7400000000000000001"
    assert post.url == "https://www.tiktok.com/@brand/video/7400000000000000001"
    assert (post.author_handle, post.author_name) == ("brand", "Brand Official")
    assert post.created_at == "2026-01-01T00:00:00+00:00"
    assert post.text == "新作リップを試してみた #コスメ"
    assert post.lang == "ja"
    assert (post.like_count, post.repost_count, post.reply_count, post.quote_count) == (1200, 30, 45, 0)
    assert post.is_repost is False
    assert post.source_query == "tiktok:brand"
    assert post.collected_at == "2026-10-01T00:00:00+09:00"


def test_media_prefers_origin_cover_and_adds_video():
    post = parse_tiktok_payload(_payload(_item()), "item_list")[0]
    assert [(m.type, m.url) for m in post.media] == [
        ("photo", "https://cdn/origin.jpg"),
        ("video", "https://cdn/play.mp4"),
    ]


def test_media_falls_back_to_cover_then_dynamic_cover():
    item = _item()
    item["video"] = {"cover": "https://cdn/cover.jpg", "dynamicCover": "https://cdn/dynamic.webp"}
    assert [(m.type, m.url) for m in parse_tiktok_payload(_payload(item), "x")[0].media] == [
        ("photo", "https://cdn/cover.jpg")]
    item["video"] = {"originCover": "", "cover": "", "dynamicCover": "https://cdn/dynamic.webp"}
    assert [(m.type, m.url) for m in parse_tiktok_payload(_payload(item), "x")[0].media] == [
        ("photo", "https://cdn/dynamic.webp")]


def test_image_post_yields_each_image_as_photo():
    item = _item()
    item["imagePost"] = {"images": [
        {"imageURL": {"urlList": ["https://cdn/i1.jpg", "https://cdn/i1b.jpg"]}},
        {"imageURL": {"urlList": ["https://cdn/i2.jpg"]}},
        {"imageURL": {"urlList": []}},
    ]}
    media = parse_tiktok_payload(_payload(item), "item_list")[0].media
    assert [(m.type, m.url) for m in media] == [("photo", "https://cdn/i1.jpg"), ("photo", "https://cdn/i2.jpg")]


def test_stats_v2_string_values_are_used_when_stats_missing():
    item = _item()
    del item["stats"]
    item["statsV2"] = {"diggCount": "321", "shareCount": "4", "commentCount": "5", "playCount": "6"}
    post = parse_tiktok_payload(_payload(item), "item_list")[0]
    assert (post.like_count, post.repost_count, post.reply_count) == (321, 4, 5)


def test_missing_stats_and_desc_default_to_zero_and_empty():
    item = _item(desc=None)
    del item["stats"]
    post = parse_tiktok_payload(_payload(item), "item_list")[0]
    assert post.text == ""
    assert (post.like_count, post.repost_count, post.reply_count) == (0, 0, 0)


def test_item_without_author_is_skipped_with_warning(caplog):
    no_author = _item("7400000000000000002")
    del no_author["author"]
    with caplog.at_level("WARNING"):
        posts = parse_tiktok_payload(_payload(no_author, _item("7400000000000000003")), "item_list")
    assert [p.id for p in posts] == ["7400000000000000003"]
    assert "7400000000000000002" in caplog.text


def test_item_found_under_unknown_root_keys_and_non_item_dicts_ignored():
    payload = {"data": {"whatever": [{"item": _item("7400000000000000004")}, {"id": "1", "desc": "x"}]}}
    assert [p.id for p in parse_tiktok_payload(payload, "item_list")] == ["7400000000000000004"]


def test_numeric_id_is_stringified():
    assert parse_tiktok_payload(_payload(_item(vid=7400000000000000005)), "item_list")[0].id == "7400000000000000005"


def test_session_cookie():
    ok = {"name": "sessionid", "value": "x", "domain": ".tiktok.com", "expires": NOW + 100}
    assert has_tiktok_session_cookie([ok], now=NOW)
    assert has_tiktok_session_cookie([{**ok, "expires": -1}], now=NOW)
    assert not has_tiktok_session_cookie([{**ok, "expires": NOW - 1}], now=NOW)
    assert not has_tiktok_session_cookie([{**ok, "value": ""}], now=NOW)
    assert not has_tiktok_session_cookie([{**ok, "domain": ".instagram.com"}], now=NOW)
    assert not has_tiktok_session_cookie([{**ok, "name": "tt_chain_token"}], now=NOW)


def test_url_classifiers():
    assert is_tiktok_login_url("https://www.tiktok.com/login?redirect_url=x")
    assert not is_tiktok_login_url("https://www.tiktok.com/@brand")
    assert is_tiktok_challenge_url("https://www.tiktok.com/captcha/page")
    assert not is_tiktok_challenge_url("https://www.tiktok.com/@brand")
    assert is_captcha_request_url("https://verification-va.tiktok.com/captcha/get?lang=ja")
    assert is_captcha_request_url("https://www.tiktok.com/captcha/verify")
    assert not is_captcha_request_url("https://www.tiktok.com/api/post/item_list/")
    assert not is_captcha_request_url("https://example.com/captcha/get")


class _FakePage:
    def __init__(self, url="https://www.tiktok.com/@brand", present=()):
        self.url = url
        self._present = set(present)

    def query_selector(self, selector):
        return object() if selector in self._present else None


def test_raise_if_blocked_detects_captcha_dom():
    from tiktok import CAPTCHA_SELECTORS
    capture = TikTokCapture()
    capture.raise_if_blocked(_FakePage())
    with pytest.raises(ChallengeDetectedError):
        capture.raise_if_blocked(_FakePage(present=[CAPTCHA_SELECTORS[0]]))


def test_raise_if_blocked_detects_captcha_url_and_login_redirect():
    capture = TikTokCapture()
    with pytest.raises(ChallengeDetectedError):
        capture.raise_if_blocked(_FakePage(url="https://www.tiktok.com/captcha/page"))
    with pytest.raises(SessionExpiredError):
        TikTokCapture().raise_if_blocked(_FakePage(url="https://www.tiktok.com/login"))


def test_raise_if_blocked_after_captcha_request_flag():
    capture = TikTokCapture()
    capture.mark_captcha_request("https://verification-va.tiktok.com/captcha/get")
    with pytest.raises(ChallengeDetectedError):
        capture.raise_if_blocked(_FakePage())
