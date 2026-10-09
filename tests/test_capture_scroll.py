"""scroll_and_collectのスクロールスクリプト差し替えの単体テスト(偽のpage/captureを使いPlaywright非依存)。"""
from capture import SCROLL_SCRIPT, scroll_and_collect


class FakePage:
    url = "https://example.com/post"

    def __init__(self):
        self.scripts = []

    def evaluate(self, script):
        self.scripts.append(script)

    def wait_for_timeout(self, ms):
        pass


class FakeCapture:
    def raise_if_blocked(self, page=None):
        pass

    def drain(self):
        return []


def test_default_scroll_script_is_window_scroll():
    page = FakePage()
    scroll_and_collect(page, FakeCapture(), lambda p, o: [], wait_seconds_range=(0, 0), max_idle_rounds=2)
    assert page.scripts == [SCROLL_SCRIPT]


def test_custom_scroll_script_is_used():
    page = FakePage()
    scroll_and_collect(page, FakeCapture(), lambda p, o: [], wait_seconds_range=(0, 0),
                       max_idle_rounds=2, scroll_script="custom()")
    assert page.scripts == ["custom()"]
