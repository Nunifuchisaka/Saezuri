"""tt_profiles.pyのプロフィール正規化ロジックの単体テスト。Playwright非依存の合成データで検証する。"""
import json

from tt_profiles import parse_profile_payload, pick_profile_scripts, resolve_users

COLLECTED_AT = "2026-10-01T12:00:00+09:00"


def _user_info(unique_id="sample_user", followers=43783):
    return {
        "user": {
            "id": "6800000000000000001",
            "uniqueId": unique_id,
            "nickname": "サンプル公式",
            "signature": "公式アカウントです",
            "verified": True,
            "privateAccount": False,
            "bioLink": {"link": "https://example.com/", "risk": 0},
        },
        "stats": {"followerCount": followers, "followingCount": 3, "heartCount": 987654, "videoCount": 321,
                  "diggCount": 0},
    }


def _embedded(info):
    return {"__DEFAULT_SCOPE__": {"webapp.user-detail": {"statusCode": 0, "userInfo": info}}}


def test_parse_embedded_payload():
    profile = parse_profile_payload(_embedded(_user_info()), "embedded", handle="sample_user",
                                    collected_at=COLLECTED_AT)
    assert profile is not None
    assert profile.handle == "sample_user"
    assert profile.user_id == "6800000000000000001"
    assert profile.name == "サンプル公式"
    assert profile.description == "公式アカウントです"
    assert (profile.followers_count, profile.following_count) == (43783, 3)
    assert (profile.likes_count, profile.videos_count) == (987654, 321)
    assert profile.is_verified is True and profile.is_private is False
    assert profile.url == "https://example.com/"
    assert profile.profile_url == "https://www.tiktok.com/@sample_user"
    assert profile.collected_at == COLLECTED_AT


def test_parse_api_payload_with_handle_filter_case_insensitive():
    payload = {"statusCode": 0, "userInfo": _user_info("Sample_User")}
    assert parse_profile_payload(payload, "user_detail", handle="sample_user").handle == "Sample_User"
    assert parse_profile_payload(payload, "user_detail", handle="other") is None


def test_missing_or_non_numeric_follower_count_returns_none_with_warning(caplog):
    info = _user_info()
    info["stats"]["followerCount"] = "many"
    with caplog.at_level("WARNING"):
        assert parse_profile_payload({"userInfo": info}, "user_detail") is None
    assert "followerCount" in caplog.text
    del info["stats"]["followerCount"]
    assert parse_profile_payload({"userInfo": info}, "user_detail") is None


def test_stats_v2_string_counts_are_accepted():
    info = _user_info()
    info["statsV2"] = {"followerCount": "500", "followingCount": "1", "heartCount": "2", "videoCount": "3"}
    del info["stats"]
    profile = parse_profile_payload({"userInfo": info}, "user_detail")
    assert (profile.followers_count, profile.videos_count) == (500, 3)


def test_pick_profile_scripts_and_resolve_users():
    texts = [json.dumps(_embedded(_user_info())), json.dumps({"a": 1}), None]
    assert len(pick_profile_scripts(texts)) == 1

    class Args:
        user = "@Foo"

    assert resolve_users(Args(), {}) == ["Foo"]

    class NoUser:
        user = None

    assert resolve_users(NoUser(), {"tiktok": {"watch_accounts": ["@a", " ", "b"]}}) == ["a", "b"]
