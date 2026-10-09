"""capture.pyの返信先ID(in_reply_to_id)抽出の単体テスト。

Playwright非依存。TweetDetailの会話スレッド(conversationthreadモジュール)を模した合成データを使う。
"""
from capture import parse_timeline_payload


def _tweet(tweet_id: str, handle: str, in_reply_to: str | None = None, visibility: bool = False) -> dict:
    legacy = {
        "id_str": tweet_id,
        "full_text": f"本文{tweet_id}",
        "created_at": "Sat Aug 08 00:00:00 +0000 2026",
        "favorite_count": 1,
        "retweet_count": 0,
        "reply_count": 0,
        "quote_count": 0,
    }
    if in_reply_to is not None:
        legacy["in_reply_to_status_id_str"] = in_reply_to
    result = {
        "__typename": "Tweet",
        "rest_id": tweet_id,
        "core": {"user_results": {"result": {"legacy": {"screen_name": handle, "name": handle}}}},
        "legacy": legacy,
    }
    if visibility:
        return {"__typename": "TweetWithVisibilityResults", "tweet": result}
    return result


def _item(result: dict) -> dict:
    return {"itemType": "TimelineTweet", "tweet_results": {"result": result}}


def _payload() -> dict:
    return {
        "data": {
            "threaded_conversation_with_injections_v2": {
                "instructions": [
                    {
                        "type": "TimelineAddEntries",
                        "entries": [
                            {"entryId": "tweet-1", "content": {"itemContent": _item(_tweet("1", "origin"))}},
                            {
                                "entryId": "conversationthread-2",
                                "content": {
                                    "items": [
                                        {"item": {"itemContent": _item(_tweet("2", "alice", in_reply_to="1"))}},
                                        {"item": {"itemContent": _item(_tweet("3", "bob", in_reply_to="2"))}},
                                    ]
                                },
                            },
                            {
                                "entryId": "conversationthread-4",
                                "content": {
                                    "items": [
                                        {"item": {"itemContent": _item(
                                            _tweet("4", "carol", in_reply_to="1", visibility=True))}},
                                    ]
                                },
                            },
                        ],
                    }
                ]
            }
        }
    }


def test_in_reply_to_id_from_legacy():
    posts = {p.id: p for p in parse_timeline_payload(_payload(), "TweetDetail")}
    assert posts["1"].in_reply_to_id is None
    assert posts["2"].in_reply_to_id == "1"
    assert posts["3"].in_reply_to_id == "2"


def test_in_reply_to_id_with_visibility_results():
    posts = {p.id: p for p in parse_timeline_payload(_payload(), "TweetDetail")}
    assert posts["4"].in_reply_to_id == "1"
