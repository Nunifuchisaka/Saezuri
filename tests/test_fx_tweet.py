"""fx_tweet.py（FxTwitter APIでの投稿取得）の単体テスト。通信はしない。値はすべて架空。"""

import io
import json
import urllib.error
from argparse import Namespace

import pytest

import fx_tweet as fx

STATUS = {
    "code": 200,
    "status": {
        "type": "status",
        "id": "1234567890123",
        "url": "https://x.com/example_a/status/1234567890123",
        "text": "新作リップの発売日です",
        "created_at": "Mon Nov 17 12:04:00 +0000 2026",
        "likes": 1284, "reposts": 312, "replies": 45, "quotes": 7, "views": 98000, "bookmarks": 20,
        "lang": "ja",
        "author": {"type": "profile", "id": "1", "name": "例A", "screen_name": "example_a"},
        "media": {"all": [
            {"type": "photo", "url": "https://pbs.example/1.jpg"},
            {"type": "gif", "url": "https://video.example/2.mp4"},
            {"type": "video", "url": "http://insecure.example/3.mp4"},
        ]},
        "replying_to": {"screen_name": "example_b", "status": "1111111111"},
        "quote": {
            "id": "2222222222", "text": "引用元の本文", "created_at": "Sun Nov 16 00:00:00 +0000 2026",
            "likes": 5, "reposts": 0, "replies": 1, "quotes": 0,
            "author": {"name": "例C", "screen_name": "example_c"},
        },
        "article": {"title": "記事タイトル", "preview_text": "記事の冒頭"},
    },
}


@pytest.mark.parametrize("value,expected", [
    ("1234567890123", "1234567890123"),
    ("https://x.com/example_a/status/1234567890123", "1234567890123"),
    ("https://twitter.com/example_a/status/1234567890123?s=20", "1234567890123"),
    ("https://mobile.x.com/example_a/status/1234567890123/photo/1", "1234567890123"),
    ("https://x.com/i/web/status/1234567890123", "1234567890123"),
    ("https://fxtwitter.com/example_a/status/1234567890123", "1234567890123"),
    ("https://example.com/example_a/status/1234567890123", None),
    ("https://x.com/example_a", None),
    ("abc", None),
    ("", None),
])
def test_parse_status_id(value, expected):
    assert fx.parse_status_id(value) == expected


def test_parse_fx_payload():
    posts, views = fx.parse_fx_payload(STATUS, "2026-11-18T00:00:00+00:00")
    assert views == 98000
    main, quoted = posts
    assert main.id == "1234567890123" and main.author_handle == "example_a" and main.author_name == "例A"
    assert main.url == "https://x.com/example_a/status/1234567890123"
    assert (main.like_count, main.repost_count, main.reply_count, main.quote_count) == (1284, 312, 45, 7)
    assert [(m.type, m.url) for m in main.media] == [
        ("photo", "https://pbs.example/1.jpg"), ("animated_gif", "https://video.example/2.mp4")]
    assert main.quoted_id == "2222222222" and main.in_reply_to_id == "1111111111"
    assert main.article_title == "記事タイトル" and main.article_text == "記事の冒頭"
    assert main.source_query == "fxtwitter" and main.lang == "ja"
    assert quoted.id == "2222222222" and quoted.author_handle == "example_c"
    assert quoted.source_query == "fxtwitter:quoted-by:1234567890123"
    assert quoted.media == [] and quoted.quoted_id is None


@pytest.mark.parametrize("payload", [
    {}, {"code": 404}, {"status": "x"},
    {"status": {"id": "1234567890123", "text": "a", "author": {}}},
    {"status": {"id": "../etc", "text": "a", "author": {"screen_name": "example_a"}}},
    {"status": {"id": "1234567890123", "author": {"screen_name": "example_a"}}},
])
def test_parse_fx_payload_invalid(payload):
    assert fx.parse_fx_payload(payload, "t") == ([], None)


def test_counts_default_to_zero_for_bad_types():
    payload = {"status": {"id": "1234567890123", "text": "a", "likes": "9", "reposts": True,
                          "author": {"screen_name": "example_a"}}}
    posts, views = fx.parse_fx_payload(payload, "t")
    assert posts[0].like_count == 0 and posts[0].repost_count == 0 and views is None


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_fetch_status_json_ok_and_request():
    seen = {}

    def opener(request, timeout):
        seen["url"] = request.full_url
        seen["ua"] = request.get_header("User-agent")
        seen["timeout"] = timeout
        return FakeResponse(json.dumps(STATUS).encode())

    assert fx.fetch_status_json("1234567890123", opener)["status"]["id"] == "1234567890123"
    assert seen["url"] == "https://api.fxtwitter.com/2/status/1234567890123"
    assert seen["ua"] == fx.USER_AGENT and seen["timeout"] == fx.TIMEOUT_SECONDS


def _http_error(code):
    def opener(request, timeout):
        raise urllib.error.HTTPError(request.full_url, code, "x", {}, None)
    return opener


def test_fetch_status_json_errors():
    with pytest.raises(fx.RateLimitedError):
        fx.fetch_status_json("1234567890123", _http_error(429))
    for code in (401, 404, 500):
        with pytest.raises(fx.FetchError):
            fx.fetch_status_json("1234567890123", _http_error(code))
    with pytest.raises(fx.FetchError):
        fx.fetch_status_json("1234567890123", lambda r, timeout: FakeResponse(b"<html>"))
    with pytest.raises(fx.FetchError):
        fx.fetch_status_json("1234567890123", lambda r, timeout: FakeResponse(b"[1]"))
    with pytest.raises(fx.FetchError):
        fx.fetch_status_json("1234567890123", lambda r, timeout: FakeResponse(b"x" * (fx.MAX_RESPONSE_BYTES + 2)))
    with pytest.raises(fx.FetchError):
        fx.fetch_status_json("abc")

    def offline(request, timeout):
        raise urllib.error.URLError("offline")

    with pytest.raises(fx.FetchError):
        fx.fetch_status_json("1234567890123", offline)


def _args(tmp_path, urls=(), ids=()):
    config = tmp_path / "config.yaml"
    config.write_text(f"output:\n  raw_dir: {tmp_path / 'raw'}\n", encoding="utf-8")
    return Namespace(url=list(urls), id=list(ids), config=str(config))


def test_run_saves_and_dedupes(tmp_path, capsys):
    calls, sleeps = [], []

    def fetch(status_id):
        calls.append(status_id)
        return STATUS

    code = fx.run(_args(tmp_path, urls=["https://x.com/example_a/status/1234567890123"],
                        ids=["1234567890123", "1234567890123"]), fetch=fetch, sleep=sleeps.append)
    assert code == 0 and calls == ["1234567890123"] and sleeps == []
    files = list((tmp_path / "raw").glob("fx_*_001.jsonl"))
    assert len(files) == 1
    rows = [json.loads(l) for l in files[0].read_text(encoding="utf-8").splitlines()]
    assert [r["id"] for r in rows] == ["1234567890123", "2222222222"]
    out = capsys.readouterr().out
    assert "表示 98,000" in out and "保存先:" in out and "取得: 1件 / 失敗: 0件 / 保存: 2件" in out


def test_run_rate_limited_keeps_collected(tmp_path, capsys):
    other = json.loads(json.dumps(STATUS))
    other["status"]["id"] = "3333333333"
    other["status"].pop("quote")

    def fetch(status_id):
        if status_id == "3333333333":
            return other
        if status_id == "4444444444":
            raise fx.FetchError("消えた投稿")
        raise fx.RateLimitedError("429")

    sleeps = []
    code = fx.run(_args(tmp_path, ids=["3333333333", "4444444444", "5555555555", "6666666666"]),
                  fetch=fetch, sleep=sleeps.append)
    assert code == 3 and len(sleeps) == 2 and all(1.0 <= s <= 3.0 for s in sleeps)
    rows = list((tmp_path / "raw").glob("fx_*.jsonl"))[0].read_text(encoding="utf-8").splitlines()
    assert [json.loads(r)["id"] for r in rows] == ["3333333333"]
    captured = capsys.readouterr()
    assert "取得: 1件 / 失敗: 1件" in captured.out and "消えた投稿" in captured.err


def test_run_input_errors(tmp_path):
    assert fx.run(_args(tmp_path), fetch=lambda i: STATUS) == 1
    assert fx.run(_args(tmp_path, urls=["https://example.com/"]), fetch=lambda i: STATUS) == 1


def test_run_all_failed(tmp_path):
    def fetch(status_id):
        raise fx.FetchError("x")

    assert fx.run(_args(tmp_path, ids=["1234567890123"]), fetch=fetch, sleep=lambda s: None) == 1
    assert not (tmp_path / "raw").exists()


def test_next_out_path(tmp_path):
    (tmp_path / "fx_20261118_001.jsonl").write_text("")
    (tmp_path / "fx_20261118_009.jsonl").write_text("")
    assert fx.next_out_path(tmp_path, "20261118").name == "fx_20261118_010.jsonl"
