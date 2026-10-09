"""schema.pyのPost.to_dict()/from_dict()往復変換のテスト(記事フィールド追加分)。"""
from schema import Post


def _minimal_post(**overrides) -> Post:
    base = dict(
        id="1", url="https://x.com/alice/status/1", author_handle="alice",
        author_name="Alice", created_at="Sat Aug 08 00:00:00 +0000 2026",
        text="本文", like_count=1, repost_count=2, reply_count=3, quote_count=4,
        collected_at="2026-08-08T00:00:00+09:00",
    )
    base.update(overrides)
    return Post(**base)


def test_to_dict_includes_article_fields():
    post = _minimal_post(article_title="記事タイトル", article_text="本文1行目\n本文2行目")
    data = post.to_dict()
    assert data["article_title"] == "記事タイトル"
    assert data["article_text"] == "本文1行目\n本文2行目"


def test_to_dict_article_fields_default_none():
    post = _minimal_post()
    data = post.to_dict()
    assert data["article_title"] is None
    assert data["article_text"] is None


def test_from_dict_roundtrip_article_fields():
    post = _minimal_post(article_title="記事タイトル", article_text="本文")
    restored = Post.from_dict(post.to_dict())
    assert restored.article_title == "記事タイトル"
    assert restored.article_text == "本文"


def test_from_dict_missing_article_keys_defaults_to_none():
    """article_title/article_textキーを持たない旧形式JSONLでも壊れないこと。"""
    post = _minimal_post()
    data = post.to_dict()
    del data["article_title"]
    del data["article_text"]
    restored = Post.from_dict(data)
    assert restored.article_title is None
    assert restored.article_text is None


def test_in_reply_to_id_roundtrip_and_position():
    post = _minimal_post(in_reply_to_id="100", quoted_id="200")
    data = post.to_dict()
    keys = list(data.keys())
    assert keys.index("in_reply_to_id") == keys.index("quoted_id") + 1
    assert Post.from_dict(data).in_reply_to_id == "100"


def test_from_dict_missing_in_reply_to_id_defaults_to_none():
    """in_reply_to_idキーを持たない既存JSONLでも壊れないこと。"""
    data = _minimal_post().to_dict()
    del data["in_reply_to_id"]
    assert Post.from_dict(data).in_reply_to_id is None
