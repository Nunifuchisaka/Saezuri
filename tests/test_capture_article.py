"""capture.pyの記事(X Article)埋め込みデータ抽出ロジックの単体テスト。

Playwright非依存。TweetDetail等のペイロード内、tweet_results.resultに
article.article_results.resultとして記事本文が同梱されているケースを模した
サンプルJSONを使う(実機確認済みの構造)。
"""
from capture import parse_timeline_payload


def _payload_with_tweet(tweet_result: dict) -> dict:
    """tweet_result 1件を含む最小限のタイムラインペイロードを組み立てる。"""
    return {
        "data": {
            "search_by_raw_query": {
                "search_timeline": {
                    "timeline": {
                        "instructions": [
                            {
                                "type": "TimelineAddEntries",
                                "entries": [
                                    {
                                        "entryId": "tweet-1",
                                        "content": {
                                            "entryType": "TimelineTimelineItem",
                                            "itemContent": {
                                                "itemType": "TimelineTweet",
                                                "tweet_results": {"result": tweet_result},
                                            },
                                        },
                                    }
                                ],
                            }
                        ]
                    }
                }
            }
        }
    }


def _tweet_result_with_article(
    tweet_id: str,
    text: str = "https://t.co/dummy",
    title: str | None = None,
    blocks: list | None = None,
) -> dict:
    """legacy情報を持つ最小限のtweet_results.result風dictを組み立てる(記事同梱可)。"""
    result: dict = {
        "__typename": "Tweet",
        "rest_id": tweet_id,
        "core": {
            "user_results": {
                "result": {"legacy": {"screen_name": "alice", "name": "Alice"}}
            }
        },
        "legacy": {
            "id_str": tweet_id,
            "full_text": text,
            "created_at": "Sat Aug 08 00:00:00 +0000 2026",
            "favorite_count": 1,
            "retweet_count": 2,
            "reply_count": 3,
            "quote_count": 4,
        },
    }
    if title is not None or blocks is not None:
        article_result: dict = {}
        if title is not None:
            article_result["title"] = title
        if blocks is not None:
            article_result["content_state"] = {"blocks": blocks}
        result["article"] = {"article_results": {"result": article_result}}
    return result


def _parse(tweet_result: dict):
    payload = _payload_with_tweet(tweet_result)
    posts = parse_timeline_payload(
        payload, "SearchTimeline", source_query="q", collected_at="2026-08-08T00:00:00+09:00"
    )
    assert len(posts) == 1
    return posts[0]


def test_parse_tweet_result_extracts_article_title_and_text():
    blocks = [{"text": "第1段落"}, {"text": "第2段落"}]
    post = _parse(_tweet_result_with_article("100", title="記事タイトル", blocks=blocks))
    assert post.article_title == "記事タイトル"
    assert post.article_text == "第1段落\n第2段落"


def test_parse_tweet_result_article_fields_none_when_no_article():
    post = _parse(_tweet_result_with_article("100"))
    assert post.article_title is None
    assert post.article_text is None


def test_parse_tweet_result_article_text_none_when_blocks_missing():
    post = _parse(_tweet_result_with_article("100", title="タイトルのみ"))
    assert post.article_title == "タイトルのみ"
    assert post.article_text is None


def test_parse_tweet_result_article_text_skips_non_dict_blocks():
    blocks = [{"text": "有効な段落"}, "不正な要素", {"no_text_key": True}]
    post = _parse(_tweet_result_with_article("100", title="タイトル", blocks=blocks))
    assert post.article_text == "有効な段落\n"


def test_parse_tweet_result_article_text_treats_none_as_empty():
    blocks = [{"text": "段落1"}, {"text": None}, {"text": "段落2"}]
    post = _parse(_tweet_result_with_article("100", title="タイトル", blocks=blocks))
    assert post.article_text == "段落1\n\n段落2"
