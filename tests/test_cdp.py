"""CDP接続まわり(接続先URLの解決・ログイン判定・接続失敗)の単体テスト。実ブラウザは使わない。"""
import pytest

from session import CdpConnectionError, connect_cdp, resolve_cdp_url
from tiktok import get_tiktok_cdp_url, has_tiktok_session_cookie, open_tiktok_context
from session import SessionExpiredError

NOW = 1_800_000_000


def test_resolve_cdp_url_cli_priority():
    assert resolve_cdp_url("http://127.0.0.1:9222", {"cdp_url": "http://x:1"}) == "http://127.0.0.1:9222"


def test_resolve_cdp_url_falls_back_to_config():
    assert resolve_cdp_url(None, {"cdp_url": " http://x:1 "}) == "http://x:1"


@pytest.mark.parametrize("section", [None, {}, {"cdp_url": None}, {"cdp_url": ""}, {"cdp_url": 5}])
def test_resolve_cdp_url_none(section):
    assert resolve_cdp_url(None, section) is None
    assert resolve_cdp_url("  ", section) is None


def test_get_tiktok_cdp_url():
    assert get_tiktok_cdp_url(None, {}) is None
    assert get_tiktok_cdp_url(None, {"tiktok": {"cdp_url": "http://a:9"}}) == "http://a:9"
    assert get_tiktok_cdp_url("http://b:1", {"tiktok": {"cdp_url": "http://a:9"}}) == "http://b:1"


def _cookie(**kw):
    base = {"name": "sessionid", "value": "abc", "domain": ".tiktok.com", "expires": NOW + 1000}
    base.update(kw)
    return base


def test_cookie_valid_and_invalid():
    assert has_tiktok_session_cookie([_cookie()], now=NOW)
    assert has_tiktok_session_cookie([_cookie(expires=-1)], now=NOW)
    assert not has_tiktok_session_cookie([_cookie(expires=NOW - 1)], now=NOW)
    assert not has_tiktok_session_cookie([_cookie(domain=".example.com")], now=NOW)
    assert not has_tiktok_session_cookie([_cookie(name="other")], now=NOW)
    assert not has_tiktok_session_cookie([], now=NOW)


class _FakeChromium:
    def __init__(self, browser=None, error=None):
        self.browser, self.error, self.url = browser, error, None

    def connect_over_cdp(self, url):
        self.url = url
        if self.error:
            raise self.error
        return self.browser


class _FakePlaywright:
    def __init__(self, chromium):
        self.chromium = chromium


class _FakeContext:
    def __init__(self, cookies):
        self._cookies = cookies

    def cookies(self):
        return self._cookies


class _FakeBrowser:
    def __init__(self, contexts):
        self.contexts = contexts
        self.closed = False

    def close(self):
        self.closed = True


def test_connect_cdp_returns_first_context():
    ctx = _FakeContext([])
    browser = _FakeBrowser([ctx])
    chromium = _FakeChromium(browser)
    got_browser, got_ctx = connect_cdp(_FakePlaywright(chromium), "http://127.0.0.1:9222")
    assert got_ctx is ctx and got_browser is browser
    assert chromium.url == "http://127.0.0.1:9222"


def test_connect_cdp_failure_message():
    pw = _FakePlaywright(_FakeChromium(error=RuntimeError("ECONNREFUSED")))
    with pytest.raises(CdpConnectionError) as exc:
        connect_cdp(pw, "http://127.0.0.1:9222")
    assert "remote-debugging-port" in str(exc.value)


def test_connect_cdp_no_context():
    pw = _FakePlaywright(_FakeChromium(_FakeBrowser([])))
    with pytest.raises(CdpConnectionError):
        connect_cdp(pw, "http://127.0.0.1:9222")


def test_open_tiktok_context_cdp_requires_session_cookie():
    browser = _FakeBrowser([_FakeContext([])])
    pw = _FakePlaywright(_FakeChromium(browser))
    with pytest.raises(SessionExpiredError):
        with open_tiktok_context(pw, {}, "http://127.0.0.1:9222", "chromium", False):
            pass
    assert not browser.closed


def test_open_tiktok_context_cdp_does_not_close_chrome():
    import time
    ctx = _FakeContext([_cookie(expires=time.time() + 1000)])
    browser = _FakeBrowser([ctx])
    pw = _FakePlaywright(_FakeChromium(browser))
    with open_tiktok_context(pw, {}, "http://127.0.0.1:9222", "chromium", False) as got:
        assert got is ctx
    assert not browser.closed
