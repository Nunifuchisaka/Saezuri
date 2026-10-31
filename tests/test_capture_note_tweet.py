"""capture.pyの長文ツイート(Note Tweet)全文抽出ロジックの単体テスト。

Playwright非依存。280字を超える長文ツイートでは legacy.full_text が途中で
切れ、全文は result.note_tweet.note_tweet_results.result.text に入っている。
fixtureは架空のユーザー名・ID・本文の合成データを使う。
"""
import pytest

from capture import _parse_note_tweet_text, parse_timeline_payload

TRUNCATED = "冒頭の本文が途中で切れた長文ツイート… https://t.co/dummy"
FULL = "冒頭の本文が途中で切れた長文ツイート。ここから先が全文の続きで、280字を超える内容になっている。"


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


def _tweet_inner(tweet_id: str, full_text: str = TRUNCATED, note_text=None, with_note: bool = False) -> dict:
    """legacy・core情報を持つツイート本体のdictを組み立てる(note_tweetは任意)。"""
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
            "full_text": full_text,
            "created_at": "Sat Aug 08 00:00:00 +0000 2026",
            "favorite_count": 1,
            "retweet_count": 2,
            "reply_count": 3,
            "quote_count": 4,
        },
    }
    if with_note:
        result["note_tweet"] = {
            "is_expandable": True,
            "note_tweet_results": {"result": {"id": "note-1", "text": note_text}},
        }
    return result


def _parse(tweet_result: dict):
    payload = _payload_with_tweet(tweet_result)
    posts = parse_timeline_payload(
        payload, "SearchTimeline", source_query="q", collected_at="2026-08-08T00:00:00+09:00"
    )
    assert len(posts) == 1
    return posts[0]


def test_note_tweet_full_text_is_used_when_present():
    post = _parse(_tweet_inner("100", note_text=FULL, with_note=True))
    assert post.text == FULL


def test_full_text_is_used_when_note_tweet_absent():
    post = _parse(_tweet_inner("100"))
    assert post.text == TRUNCATED


def test_note_tweet_full_text_is_used_for_visibility_wrapper():
    inner = _tweet_inner("100", note_text=FULL, with_note=True)
    post = _parse({"__typename": "TweetWithVisibilityResults", "tweet": inner})
    assert post.text == FULL
    assert post.author_handle == "alice"


def test_note_tweet_skipped_when_full_text_missing():
    inner = _tweet_inner("100", note_text=FULL, with_note=True)
    del inner["legacy"]["full_text"]
    payload = _payload_with_tweet(inner)
    posts = parse_timeline_payload(payload, "SearchTimeline", source_query="q", collected_at="2026-08-08T00:00:00+09:00")
    assert posts == []


@pytest.mark.parametrize(
    "note_text",
    ["", "   ", None, 123, {"text": FULL}],
)
def test_falls_back_to_full_text_when_note_text_invalid(note_text):
    post = _parse(_tweet_inner("100", note_text=note_text, with_note=True))
    assert post.text == TRUNCATED


def test_note_helper_returns_none_when_structure_is_broken():
    assert _parse_note_tweet_text({}) is None
    assert _parse_note_tweet_text({"note_tweet": "不正な型"}) is None
    assert _parse_note_tweet_text({"note_tweet": {"note_tweet_results": None}}) is None
    assert _parse_note_tweet_text({"note_tweet": {"note_tweet_results": {"result": "不正"}}}) is None


def test_note_helper_reads_nested_tweet_key():
    result = {"tweet": {"note_tweet": {"note_tweet_results": {"result": {"text": FULL}}}}}
    assert _parse_note_tweet_text(result) == FULL


def test_note_helper_prefers_top_level_over_nested_tweet_key():
    result = {
        "note_tweet": {"note_tweet_results": {"result": {"text": FULL}}},
        "tweet": {"note_tweet": {"note_tweet_results": {"result": {"text": "別の本文"}}}},
    }
    assert _parse_note_tweet_text(result) == FULL
