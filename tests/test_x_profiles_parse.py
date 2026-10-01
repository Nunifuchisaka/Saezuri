"""x_profiles.pyのプロフィール正規化ロジックの単体テスト。Playwright非依存の合成データで検証する。"""
from x_profiles import parse_profile_payload, resolve_handles

COLLECTED_AT = "2026-10-01T12:00:00+09:00"


def _new_style_user(handle="sample_user", followers=1234, following=5, tweets=678):
    """2026-10時点で実機確認したUserByScreenNameの構造(relationship_counts/tweet_counts/core形式)を匿名化した合成データ。"""
    return {
        "__typename": "User",
        "rest_id": "1000000000000000001",
        "core": {
            "created_at": "Mon Jul 11 02:19:40 +0000 2016",
            "name": "サンプル公式",
            "screen_name": handle,
        },
        "is_blue_verified": False,
        "location": {"location": "東京"},
        "privacy": {"protected": False},
        "professional": {"category": [{"id": 103, "name": "美容・コスメ"}]},
        "profile_bio": {
            "description": "公式アカウントです",
            "entities": {"url": {"urls": [{"expanded_url": "https://example.com/"}]}},
        },
        "relationship_counts": {"followers": followers, "following": following},
        "tweet_counts": {"media_tweets": 90, "tweets": tweets},
        "verification": {"verified": False, "verified_type": "Business"},
    }


def _payload(user):
    return {"data": {"user": {"result": user}}}


def test_新形式から主要項目を取り出せる():
    profile = parse_profile_payload(
        _payload(_new_style_user()), "UserByScreenName", handle="sample_user", collected_at=COLLECTED_AT
    )
    assert profile is not None
    assert profile.handle == "sample_user"
    assert profile.user_id == "1000000000000000001"
    assert profile.name == "サンプル公式"
    assert profile.description == "公式アカウントです"
    assert profile.location == "東京"
    assert profile.url == "https://example.com/"
    assert profile.followers_count == 1234
    assert profile.following_count == 5
    assert profile.tweet_count == 678
    assert profile.media_count == 90
    assert profile.verified_type == "Business"
    assert profile.category == ["美容・コスメ"]
    assert profile.protected is False
    assert profile.profile_url == "https://x.com/sample_user"
    assert profile.collected_at == COLLECTED_AT


def test_旧形式legacyのfollowers_countにもフォールバックする():
    user = {
        "rest_id": "42",
        "legacy": {
            "screen_name": "old_user",
            "name": "旧形式",
            "description": "bio",
            "followers_count": 999,
            "friends_count": 10,
            "statuses_count": 20,
            "media_count": 3,
            "protected": True,
        },
    }
    profile = parse_profile_payload(_payload(user), handle="old_user", collected_at=COLLECTED_AT)
    assert profile is not None
    assert profile.followers_count == 999
    assert profile.following_count == 10
    assert profile.tweet_count == 20
    assert profile.media_count == 3
    assert profile.protected is True


def test_ルートキーが違っても再帰探索で見つかる():
    payload = {"data": {"renamed_root": {"wrapper": [{"result": _new_style_user(followers=7)}]}}}
    profile = parse_profile_payload(payload, handle="sample_user", collected_at=COLLECTED_AT)
    assert profile is not None
    assert profile.followers_count == 7


def test_ハンドル指定時は大文字小文字を無視して一致するユーザーだけを採る():
    payload = {
        "data": {
            "a": _new_style_user(handle="other_user", followers=1),
            "b": _new_style_user(handle="Sample_User", followers=2),
        }
    }
    profile = parse_profile_payload(payload, handle="sample_user", collected_at=COLLECTED_AT)
    assert profile is not None
    assert profile.followers_count == 2


def test_ハンドル不一致のみならNone():
    payload = _payload(_new_style_user(handle="other_user"))
    assert parse_profile_payload(payload, handle="sample_user", collected_at=COLLECTED_AT) is None


def test_フォロワー数が無ければNone():
    user = _new_style_user()
    del user["relationship_counts"]
    assert parse_profile_payload(_payload(user), handle="sample_user", collected_at=COLLECTED_AT) is None


def test_任意項目が欠けても取得できる():
    user = {
        "rest_id": "1",
        "core": {"screen_name": "minimal"},
        "relationship_counts": {"followers": 10},
    }
    profile = parse_profile_payload(_payload(user), handle="minimal", collected_at=COLLECTED_AT)
    assert profile is not None
    assert profile.followers_count == 10
    assert profile.following_count is None
    assert profile.name == ""
    assert profile.description == ""
    assert profile.category == []


def test_存在しないユーザーの空応答はNone():
    assert parse_profile_payload({"data": {}}, handle="x", collected_at=COLLECTED_AT) is None
    assert parse_profile_payload({"data": {"user": {}}}, handle="x", collected_at=COLLECTED_AT) is None


def test_to_dictはJSONL保存用のキーを持つ():
    profile = parse_profile_payload(
        _payload(_new_style_user()), handle="sample_user", collected_at=COLLECTED_AT
    )
    record = profile.to_dict()
    assert record["handle"] == "sample_user"
    assert record["followers_count"] == 1234
    assert record["collected_at"] == COLLECTED_AT


class _Args:
    def __init__(self, handle=None):
        self.handle = handle


def test_resolve_handlesは引数優先でat記号を除去する():
    config = {"watch_accounts": ["a", "@b"]}
    assert resolve_handles(_Args("@c"), config) == ["c"]
    assert resolve_handles(_Args(), config) == ["a", "b"]
    assert resolve_handles(_Args(), {}) == []
