"""ig_user_posts.filter_by_authorの単体テスト(Playwright非依存)。"""
from schema import Post

from ig_user_posts import filter_by_author


def _post(post_id: str, handle: str) -> Post:
    return Post(
        id=post_id,
        url=f"https://www.instagram.com/p/{post_id}/",
        author_handle=handle,
        author_name="",
        created_at="2026-01-01T00:00:00+00:00",
        text="",
        like_count=0,
        repost_count=0,
        reply_count=0,
        quote_count=0,
        media=[],
        source_query="instagram:brand",
        collected_at="2026-01-02T00:00:00+00:00",
    )


def test_keeps_only_target_author():
    posts = [_post("1", "brand"), _post("2", "chomado"), _post("3", "brand"), _post("4", "officenino")]
    assert [p.id for p in filter_by_author(posts, "brand")] == ["1", "3"]


def test_case_insensitive():
    posts = [_post("1", "Brand"), _post("2", "BRAND"), _post("3", "other")]
    assert [p.id for p in filter_by_author(posts, "bRaNd")] == ["1", "2"]


def test_only_others_returns_empty():
    assert filter_by_author([_post("1", "chomado"), _post("2", "base44.app")], "brand") == []
