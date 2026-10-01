"""GraphQLCaptureのオペレーション名判定(別名対応)の単体テスト。Playwright非依存。"""
from capture import GraphQLCapture


def test_UserTweets指定でUserOriginalsTimelineも捕捉対象になる():
    capture = GraphQLCapture(operations=("UserTweets",))
    assert capture.matches("UserTweets")
    assert capture.matches("UserOriginalsTimeline")


def test_別名対象外のオペレーションは捕捉しない():
    capture = GraphQLCapture(operations=("UserTweets",))
    assert not capture.matches("UserByScreenName")
    assert not capture.matches("ProfileSpotlightsQuery")


def test_SearchTimeline指定に別名は混ざらない():
    capture = GraphQLCapture(operations=("SearchTimeline",))
    assert not capture.matches("UserOriginalsTimeline")
