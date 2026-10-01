"""th_profiles.pyのプロフィール正規化ロジックの単体テスト。Playwright非依存の合成データで検証する。"""
import json

import pytest

from th_profiles import (
    build_profile_from_text,
    find_follower_text,
    parse_follower_text,
    parse_profile_payload,
    pick_profile_scripts,
    resolve_users,
)

COLLECTED_AT = "2026-10-01T12:00:00+09:00"


def _user(username="sample_user", followers=43783):
    """2026-10時点で実機確認したThreadsプロフィール埋め込みJSONのuserノードを匿名化した合成データ。"""
    return {
        "pk": "1000000001",
        "id": "1000000001",
        "username": username,
        "full_name": "サンプル公式",
        "biography": "公式アカウントです\n最新情報をお届けします",
        "follower_count": followers,
        "is_verified": False,
        "text_post_app_is_private": False,
        "bio_links": [{"url": "https://example.com/", "lynx_url": "https://l.threads.com/?u=x"}],
        "friendship_status": {"following": False},
    }


def _payload(user):
    """実機のRelayPrefetchedStreamCache風の深いネストを再現する。"""
    return {"require": [["RelayPrefetchedStreamCache", "next", [], [
        {"__bbox": {"require": [["RelayPrefetchedStreamCache", "next", [],
                                 ["key", {"__bbox": {"result": {"data": {"user": user}}}}]]]}}
    ]]]}


@pytest.mark.parametrize("text, expected", [
    ("4.3万人", 43000),
    ("4.3万", 43000),
    ("1.2億", 120000000),
    ("1,234人", 1234),
    ("500人", 500),
    ("1万人", 10000),
    ("12.5万人", 125000),
    ("1.2K", 1200),
    ("3.4M", 3400000),
    ("999", 999),
])
def test_表示テキストを数値に変換できる(text, expected):
    assert parse_follower_text(text) == expected


@pytest.mark.parametrize("text", ["", "フォロワー", "abc", None, "万人"])
def test_数値にできないテキストはNone(text):
    assert parse_follower_text(text) is None


def test_ページ文中からフォロワー表記を抜き出せる():
    assert find_follower_text("CEZANNEの公式\nフォロワー4.3万人\nフォローする") == "4.3万"
    assert find_follower_text("フォロワー1,234人") == "1,234"
    assert find_follower_text("フォロワー500人") == "500"
    assert find_follower_text("43.7K followers") == "43.7K"
    assert find_follower_text("フォロワーはいません") is None


def test_埋め込みJSONから正確値を取り出せる():
    profile = parse_profile_payload(
        _payload(_user()), "embedded", handle="sample_user", collected_at=COLLECTED_AT
    )
    assert profile is not None
    assert profile.handle == "sample_user"
    assert profile.user_id == "1000000001"
    assert profile.name == "サンプル公式"
    assert profile.description.startswith("公式アカウントです")
    assert profile.followers_count == 43783
    assert profile.followers_is_approx is False
    assert profile.followers_source == "embedded_json"
    assert profile.is_verified is False
    assert profile.is_private is False
    assert profile.url == "https://example.com/"
    assert profile.profile_url == "https://www.threads.com/@sample_user"
    assert profile.collected_at == COLLECTED_AT


def test_handleは大文字小文字を無視して一致するものだけ採る():
    payload = {"a": _user("Other_User", 5), "b": _user("Sample_User", 77)}
    profile = parse_profile_payload(payload, handle="sample_user", collected_at=COLLECTED_AT)
    assert profile is not None
    assert profile.followers_count == 77


def test_対象ユーザーが居なければNone():
    assert parse_profile_payload(_payload(_user("someone")), handle="sample_user") is None
    assert parse_profile_payload({"data": {}}, handle="sample_user") is None


def test_follower_countが無ければNone():
    user = _user()
    del user["follower_count"]
    assert parse_profile_payload(_payload(user), handle="sample_user") is None


def test_follower_countが文字列や真偽値なら採らない():
    user = _user()
    user["follower_count"] = True
    assert parse_profile_payload(_payload(user), handle="sample_user") is None


def test_任意項目が欠けていても落ちない():
    user = {"username": "sample_user", "pk": "1", "follower_count": 10}
    profile = parse_profile_payload(user, handle="sample_user", collected_at=COLLECTED_AT)
    assert profile is not None
    assert profile.name == ""
    assert profile.description == ""
    assert profile.url is None
    assert profile.is_verified is None


def test_表示テキストからは概数として組み立てる():
    text = "セザンヌ化粧品　公式\ncezannecosmetics\nフォロワー4.3万人\nフォローする"
    profile = build_profile_from_text("cezannecosmetics", text, collected_at=COLLECTED_AT)
    assert profile is not None
    assert profile.followers_count == 43000
    assert profile.followers_is_approx is True
    assert profile.followers_source == "page_text"
    assert profile.handle == "cezannecosmetics"
    assert profile.profile_url == "https://www.threads.com/@cezannecosmetics"


def test_表示テキストにフォロワー表記が無ければNone():
    assert build_profile_from_text("x", "何もありません", collected_at=COLLECTED_AT) is None


def test_プロフィールJSONを含むscriptだけを選ぶ():
    mine = json.dumps(_payload(_user()))
    other = json.dumps({"a": 1})
    assert pick_profile_scripts([other, mine, "壊れた{"]) == [mine]


def test_resolve_usersは引数優先で_を除去する():
    class Args:
        user = "@abc"

    class NoArgs:
        user = None

    config = {"threads": {"watch_accounts": ["@a", "b"]}}
    assert resolve_users(Args, config) == ["abc"]
    assert resolve_users(NoArgs, config) == ["a", "b"]
