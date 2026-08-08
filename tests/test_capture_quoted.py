"""capture.pyの引用元投稿(quoted_status_result)抽出ロジックの単体テスト。

Playwright非依存。TweetDetailペイロードの構造を模したサンプルJSONを使う。
"""
from capture import find_tweet_result, parse_quoted_post


def _payload_with_tweet(tweet_result: dict) -> dict:
    """tweet_result 1件を含む最小限のTweetDetailペイロードを組み立てる。"""
    return {
        "data": {
            "threaded_conversation_with_injections_v2": {
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


def _tweet_result(
    tweet_id: str,
    text: str,
    handle: str = "alice",
    name: str = "Alice",
    quoted: dict | None = None,
    wrap_visibility: bool = False,
) -> dict:
    """legacy情報を持つ最小限のtweet_results.result風dictを組み立てる。"""
    result = {
        "__typename": "Tweet",
        "rest_id": tweet_id,
        "core": {
            "user_results": {
                "result": {"legacy": {"screen_name": handle, "name": name}}
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
    if quoted is not None:
        result["legacy"]["quoted_status_id_str"] = quoted["rest_id"]
        if wrap_visibility:
            result["quoted_status_result"] = {
                "result": {"__typename": "TweetWithVisibilityResults", "tweet": quoted}
            }
        else:
            result["quoted_status_result"] = {"result": quoted}
    return result


def test_find_tweet_result_returns_matching_result():
    payload = _payload_with_tweet(_tweet_result("100", "起点ツイート"))
    result = find_tweet_result(payload, "100")
    assert result is not None
    assert result["rest_id"] == "100"


def test_find_tweet_result_returns_none_when_not_found():
    payload = _payload_with_tweet(_tweet_result("100", "起点ツイート"))
    assert find_tweet_result(payload, "999") is None


def test_parse_quoted_post_returns_post_when_present():
    quoted = _tweet_result("200", "引用元の本文", handle="bob", name="Bob")
    origin = _tweet_result("100", "起点ツイート", quoted=quoted)

    post = parse_quoted_post(origin, origin_id="100", operation="TweetDetail",
                              collected_at="2026-08-08T00:00:00+09:00")

    assert post is not None
    assert post.id == "200"
    assert post.text == "引用元の本文"
    assert post.author_handle == "bob"
    assert post.source_query == "quoted-by:100"
    assert post.collected_at == "2026-08-08T00:00:00+09:00"


def test_parse_quoted_post_returns_none_when_absent():
    origin = _tweet_result("100", "起点ツイート")
    post = parse_quoted_post(origin, origin_id="100", operation="TweetDetail",
                              collected_at="2026-08-08T00:00:00+09:00")
    assert post is None


def test_parse_quoted_post_handles_visibility_results_nesting():
    quoted = _tweet_result("300", "閲覧制限付き引用元", handle="carol", name="Carol")
    origin = _tweet_result("100", "起点ツイート", quoted=quoted, wrap_visibility=True)

    post = parse_quoted_post(origin, origin_id="100", operation="TweetDetail",
                              collected_at="2026-08-08T00:00:00+09:00")

    assert post is not None
    assert post.id == "300"
    assert post.author_handle == "carol"


def test_parse_quoted_post_returns_none_when_quoted_legacy_missing():
    origin = _tweet_result("100", "起点ツイート")
    origin["quoted_status_result"] = {"result": {"__typename": "Tweet", "rest_id": "400"}}

    post = parse_quoted_post(origin, origin_id="100", operation="TweetDetail",
                              collected_at="2026-08-08T00:00:00+09:00")

    assert post is None
