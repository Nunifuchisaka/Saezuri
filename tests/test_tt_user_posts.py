"""tt_user_posts.pyの対象ユーザー解決と作者フィルタ流用の単体テスト(Playwright非依存)。"""
from schema import Post
from tiktok import parse_tiktok_payload
from tt_user_posts import STATE_KEY_PREFIX, filter_by_author, resolve_users


class _Args:
    def __init__(self, user=None):
        self.user = user


def test_resolve_users():
    config = {"tiktok": {"watch_accounts": ["@a", "b"]}}
    assert resolve_users(_Args(), config) == ["a", "b"]
    assert resolve_users(_Args("@Foo"), config) == ["Foo"]
    assert resolve_users(_Args(), {}) == []


def test_other_authors_are_filtered_out():
    def item(vid, uid):
        return {"id": vid, "createTime": 1767225600, "desc": "", "stats": {},
                "author": {"uniqueId": uid, "nickname": uid}}

    posts = parse_tiktok_payload({"itemList": [item("1", "Brand"), item("2", "other")]}, "item_list")
    assert all(isinstance(p, Post) for p in posts)
    assert [p.id for p in filter_by_author(posts, "brand")] == ["1"]


def test_state_key_prefix_is_distinct():
    assert STATE_KEY_PREFIX == "tiktok_"
