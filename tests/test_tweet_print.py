"""tweet.pyの_print_post()(標準出力への整形表示)の単体テスト。Playwright非依存。"""
from schema import Post
from tweet import _print_post


def _post(**overrides) -> Post:
    base = dict(
        id="1", url="https://x.com/alice/status/1", author_handle="alice",
        author_name="Alice", created_at="Sat Aug 08 00:00:00 +0000 2026",
        text="本文", like_count=1, repost_count=2, reply_count=3, quote_count=4,
        collected_at="2026-08-08T00:00:00+09:00",
    )
    base.update(overrides)
    return Post(**base)


def test_print_post_without_article(capsys):
    _print_post(_post())
    out = capsys.readouterr().out
    assert "@alice (Alice) Sat Aug 08 00:00:00 +0000 2026" in out
    assert "本文" in out
    assert "記事:" not in out
    assert "URL: https://x.com/alice/status/1" in out


def test_print_post_with_article(capsys):
    _print_post(_post(article_title="記事タイトル", article_text="第1段落\n第2段落"))
    out = capsys.readouterr().out
    assert "--- 記事: 記事タイトル ---" in out
    assert "第1段落\n第2段落" in out


def test_print_post_with_article_title_but_no_text(capsys):
    _print_post(_post(article_title="タイトルのみ", article_text=None))
    out = capsys.readouterr().out
    assert "--- 記事: タイトルのみ ---" in out
    assert "(本文取得失敗)" in out
