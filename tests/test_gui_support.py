"""gui_support.py（GUIのコマンド組み立て・config書き換え）の単体テスト。値はすべて架空。"""

from pathlib import Path

import pytest
import yaml

import gui_support as g

SAMPLE_CONFIG = """# saezuri 設定ファイル

# 監視対象アカウント
watch_accounts: ["example_a","example_b"]

# 関心キーワード
interest_keywords: ["foo"]

max_items: 100

session:
  storage_state_path: session/x_storage_state.json  # 保存先
  # コメント行
  cdp_url: http://127.0.0.1:9222

instagram:
  # 追跡対象
  watch_accounts: []
  storage_state_path: session/instagram_storage_state.json  # 保存先

threads:
  watch_accounts:
  - old_user
  - old_user2
  storage_state_path: session/threads_storage_state.json

tiktok:
  watch_accounts: []
  storage_state_path: session/tiktok_storage_state.json
  # 例: cdp_url: http://127.0.0.1:9222
  cdp_url:

browser: chromium
"""


def test_exit_messages():
    assert g.exit_message(0) == "完了しました。"
    assert "ログイン" in g.exit_message(2)
    assert "保存済み" in g.exit_message(3)
    assert "確認画面" in g.exit_message(4)
    assert "99" in g.exit_message(99)
    assert g.exit_message(None) == "中止しました。"


def test_script_command():
    cmd = g.script_command("py", Path("scripts"), "search.py", ["--query", "a"])
    assert cmd[0] == "py" and cmd[1] == "-u"
    assert Path(cmd[2]) == Path("scripts/search.py")
    assert cmd[3:] == ["--query", "a"]


@pytest.mark.parametrize("raw,expected", [
    ("@example_a", "example_a"),
    ("  example_a ", "example_a"),
    ("https://x.com/example_a", "example_a"),
    ("https://www.instagram.com/example.b/", "example.b"),
    ("https://www.threads.com/@example_c", "example_c"),
    ("", ""),
])
def test_normalize_account(raw, expected):
    assert g.normalize_account(raw) == expected


def test_build_search_args_full():
    args = g.build_search_args("新作 コスメ", "@example_a", "2026/11/01", "2026-11-10", "top", "50")
    assert args == ["--query=新作 コスメ", "--from=example_a", "--since", "2026-11-01",
                    "--until", "2026-11-10", "--mode", "top", "--max", "50"]


def test_build_search_args_minimal():
    assert g.build_search_args("abc") == ["--query=abc", "--mode", "live"]


@pytest.mark.parametrize("kwargs", [
    {"query": "", "from_handle": ""},
    {"query": "a", "since": "11月1日"},
    {"query": "a", "since": "2026-11-10", "until": "2026-11-01"},
    {"query": "a", "max_items": "0"},
    {"query": "a", "max_items": "abc"},
])
def test_build_search_args_errors(kwargs):
    with pytest.raises(g.InputError):
        g.build_search_args(**kwargs)


def test_build_account_command():
    assert g.build_account_command(g.SNS_X, g.KIND_POSTS, "@example_a", "20") == (
        "user_tweets.py", ["--handle=example_a", "--max", "20"])
    assert g.build_account_command(g.SNS_THREADS, g.KIND_PROFILES, "", "20") == ("th_profiles.py", [])
    assert g.build_account_command(g.SNS_TIKTOK, g.KIND_POSTS, "example_t") == (
        "tt_user_posts.py", ["--user=example_t"])
    with pytest.raises(g.InputError):
        g.build_account_command(g.SNS_INSTAGRAM, g.KIND_PROFILES)


@pytest.mark.parametrize("url,sns", [
    ("https://x.com/example_a/status/1", g.SNS_X),
    ("https://twitter.com/example_a/status/1", g.SNS_X),
    ("https://www.instagram.com/p/AAA/", g.SNS_INSTAGRAM),
    ("https://www.threads.com/@example_c/post/AAA", g.SNS_THREADS),
    ("https://www.threads.net/@example_c/post/AAA", g.SNS_THREADS),
    ("https://www.tiktok.com/@example_t/video/1", g.SNS_TIKTOK),
    ("https://example.com/", None),
    ("not a url", None),
])
def test_detect_sns_from_url(url, sns):
    assert g.detect_sns_from_url(url) == sns


def test_build_related_command():
    assert g.build_related_command("https://x.com/example_a/status/1", "5") == (
        "related.py", ["--url=https://x.com/example_a/status/1", "--top", "5"])
    assert g.build_related_command("https://www.instagram.com/p/AAA/")[0] == "ig_related.py"
    assert g.build_related_command("https://www.tiktok.com/@example_t/video/1")[0] == "tt_related.py"
    for bad in ("", "https://example.com/"):
        with pytest.raises(g.InputError):
            g.build_related_command(bad)


def test_build_digest_and_media_args():
    assert g.build_digest_args(["a.jsonl", "b.jsonl"], " 今週 ") == ["a.jsonl", "b.jsonl", "--title=今週"]
    assert g.build_digest_args(["a.jsonl"]) == ["a.jsonl"]
    assert g.build_media_args(["a.jsonl"]) == ["a.jsonl"]
    with pytest.raises(g.InputError):
        g.build_digest_args([])
    with pytest.raises(g.InputError):
        g.build_media_args([])


def test_parse_account_lines():
    assert g.parse_account_lines("@example_a\n\nexample_b, example_a\nhttps://x.com/example_c\n") == [
        "example_a", "example_b", "example_c"]


def test_set_watch_accounts_keeps_comments_and_other_keys():
    text = g.set_watch_accounts(SAMPLE_CONFIG, g.SNS_X, ["new_a", "new_b"])
    text = g.set_watch_accounts(text, g.SNS_INSTAGRAM, ["ig_a"])
    text = g.set_watch_accounts(text, g.SNS_THREADS, ["th_a"])
    text = g.set_watch_accounts(text, g.SNS_TIKTOK, [])
    text = g.set_interest_keywords(text, ['キーワード "引用"'])
    data = yaml.safe_load(text)
    assert data["watch_accounts"] == ["new_a", "new_b"]
    assert data["instagram"]["watch_accounts"] == ["ig_a"]
    assert data["threads"]["watch_accounts"] == ["th_a"]
    assert data["tiktok"]["watch_accounts"] == []
    assert data["interest_keywords"] == ['キーワード "引用"']
    assert data["threads"]["storage_state_path"] == "session/threads_storage_state.json"
    assert data["max_items"] == 100 and data["browser"] == "chromium"
    assert "# 監視対象アカウント" in text and "# 追跡対象" in text
    assert "old_user" not in text


def test_set_cdp_urls_toggle():
    off = g.set_cdp_urls(SAMPLE_CONFIG, False)
    data = yaml.safe_load(off)
    assert data["session"]["cdp_url"] is None and data["tiktok"]["cdp_url"] is None
    assert not g.cdp_enabled(data)
    assert "# 例: cdp_url: http://127.0.0.1:9222" in off  # コメント行は書き換えない
    on = yaml.safe_load(g.set_cdp_urls(off, True))
    assert on["session"]["cdp_url"] == g.DEFAULT_CDP_URL
    assert on["tiktok"]["cdp_url"] == g.DEFAULT_CDP_URL
    assert g.cdp_enabled(on)


def test_set_config_value_adds_missing_key_and_section():
    text = "session:\n  storage_state_path: a.json\n\nbrowser: chromium\n"
    out = g.set_config_value(text, "session", "cdp_url", "http://127.0.0.1:9222")
    data = yaml.safe_load(out)
    assert data["session"] == {"storage_state_path": "a.json", "cdp_url": "http://127.0.0.1:9222"}
    out2 = g.set_config_value("browser: chromium\n", "tiktok", "cdp_url", "")
    assert yaml.safe_load(out2) == {"browser": "chromium", "tiktok": {"cdp_url": None}}


def test_set_config_value_crlf():
    text = SAMPLE_CONFIG.replace("\n", "\r\n")
    out = g.set_watch_accounts(text, g.SNS_THREADS, ["th_a"])
    assert "\r\n" in out and "\n" not in out.replace("\r\n", "")
    assert yaml.safe_load(out)["threads"]["watch_accounts"] == ["th_a"]


def test_get_watch_accounts():
    data = yaml.safe_load(SAMPLE_CONFIG)
    assert g.get_watch_accounts(data, g.SNS_X) == ["example_a", "example_b"]
    assert g.get_watch_accounts(data, g.SNS_THREADS) == ["old_user", "old_user2"]
    assert g.get_watch_accounts({}, g.SNS_INSTAGRAM) == []


def test_find_chrome_and_command():
    env = {"PROGRAMFILES": r"C:\PF", "LOCALAPPDATA": r"C:\LA"}
    target = str(Path(r"C:\LA") / "Google" / "Chrome" / "Application" / "chrome.exe")
    assert g.find_chrome("win32", env, exists=lambda p: p == target) == target
    assert g.find_chrome("linux", {}, exists=lambda p: False) is None
    cmd = g.chrome_command("chrome", "prof", start_urls=["https://x.com/login"])
    assert cmd == ["chrome", "--remote-debugging-port=9222", "--user-data-dir=prof",
                   "--no-first-run", "--no-default-browser-check", "https://x.com/login"]


def test_find_chrome_macos():
    app = "Google Chrome.app/Contents/MacOS/Google Chrome"
    assert g.find_chrome("darwin", {"HOME": "/Users/example"}, exists=lambda p: True) == f"/Applications/{app}"
    # 管理者権限なしでホームの「アプリケーション」に入れた場合も見つける
    user_app = f"/Users/example/Applications/{app}"
    assert g.find_chrome("darwin", {"HOME": "/Users/example"}, exists=lambda p: p == user_app) == user_app


def test_extract_output_path():
    assert g.extract_output_path("保存先: data/raw/search_20260101_001.jsonl") == "data/raw/search_20260101_001.jsonl"
    assert g.extract_output_path("上位Markdown: digest/top_x_1_20260101.md\n") == "digest/top_x_1_20260101.md"
    assert g.extract_output_path("出力: digest/a.md") == "digest/a.md"
    assert g.extract_output_path("取得: 3件") is None
    assert g.extract_output_path("保存先:") is None
    # 字下げされた行（投稿本文の表示など）は出力先として拾わない
    assert g.extract_output_path("  出力: digest/a.md") is None
    assert g.extract_output_path("  本文: 出力: digest/a.md") is None


def test_write_csv(tmp_path):
    import csv
    import json

    src = tmp_path / "a.jsonl"
    posts = [
        {"id": "1", "url": "https://x.com/example_a/status/1", "author_handle": "example_a",
         "author_name": "例A", "created_at": "Mon Sep 21 03:28:32 +0000 2026", "text": "本文1\n2行目",
         "like_count": 3, "repost_count": 1, "reply_count": 0, "quote_count": 0,
         "collected_at": "2026-09-21T04:00:00+00:00", "media": [{"type": "photo", "url": "https://img.example/1.jpg"}],
         "is_repost": False, "source_query": "q"},
        {"id": "2", "url": "https://x.com/example_b/status/2", "author_handle": "example_b",
         "author_name": "例B", "created_at": "2026-09-22T00:00:00Z", "text": "本文2",
         "like_count": 5, "repost_count": 0, "reply_count": 2, "quote_count": 1,
         "collected_at": "2026-09-22T01:00:00+00:00", "media": [], "is_repost": True, "source_query": "q"},
    ]
    src.write_text("\n".join(json.dumps(p, ensure_ascii=False) for p in posts) + "\nnot json\n", encoding="utf-8")
    out = g.default_csv_path([src])
    assert out == tmp_path / "a.csv"
    assert g.write_csv([src], out) == 2
    raw = out.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    rows = list(csv.reader(out.open(encoding="utf-8-sig")))
    head = rows[0]
    assert head[:4] == ["投稿日時(日本時間)", "アカウント", "表示名", "本文"]
    assert "created_at" not in head
    r1 = dict(zip(head, rows[1]))
    assert r1["投稿日時(日本時間)"] == "2026-09-21 12:28"
    assert r1["本文"] == "本文1\n2行目"
    assert r1["画像・動画URL"] == "https://img.example/1.jpg"
    assert r1["is_repost"] == ""
    r2 = dict(zip(head, rows[2]))
    assert r2["投稿日時(日本時間)"] == "2026-09-22 09:00"
    assert r2["is_repost"] == "はい"
    with pytest.raises(g.InputError):
        g.write_csv([], out)


def test_default_csv_path_multiple(tmp_path):
    assert g.default_csv_path([tmp_path / "a.jsonl", tmp_path / "b.jsonl"]) == tmp_path / "a_ほか1件.csv"


def test_login_status(tmp_path):
    (tmp_path / "session").mkdir()
    (tmp_path / "session" / "instagram_storage_state.json").write_text("{}", encoding="utf-8")
    config = {
        "session": {"cdp_url": "http://127.0.0.1:9222"},
        "instagram": {"storage_state_path": "session/instagram_storage_state.json"},
        "threads": {"storage_state_path": "session/threads_storage_state.json"},
        "tiktok": {"cdp_url": None},
    }
    status = {s["sns"]: s for s in g.login_status(config, tmp_path, chrome_alive=False)}
    assert status["x"]["state"] == "warn"
    assert status["instagram"]["state"] == "ok"
    assert status["threads"]["state"] == "off"
    assert status["tiktok"]["state"] == "off"
    alive = {s["sns"]: s for s in g.login_status(config, tmp_path, chrome_alive=True)}
    assert alive["x"]["state"] == "ok" and alive["x"]["text"] == "ログイン用Chrome"


def test_build_fx_args():
    text = "https://x.com/example_a/status/1234567890123\n\n9876543210, https://twitter.com/b/status/1234567890123\n"
    assert g.build_fx_args(text) == ["--id", "1234567890123", "--id", "9876543210", "--id", "1234567890123"]
    for bad in ("", "https://example.com/", "\n".join(str(10**12 + i) for i in range(g.FX_MAX_URLS + 1))):
        with pytest.raises(g.InputError):
            g.build_fx_args(bad)


def test_dash_leading_values_stay_one_argument():
    # 「-」で始まる検索語がオプションと誤認されないよう、--flag=value の1引数で渡す
    assert g.build_search_args("-filter:replies 新作")[0] == "--query=-filter:replies 新作"
    import search

    parsed = search.build_arg_parser().parse_args(g.build_search_args("-filter:replies 新作"))
    assert parsed.query == "-filter:replies 新作"


@pytest.mark.parametrize("value", ["-example", "../../x", "a/b", "a b", "例", "a" * 65, "--cdp=http://example.invalid"])
def test_normalize_account_rejects_odd_names(value):
    with pytest.raises(g.InputError):
        g.normalize_account(value)


def test_normalize_account_accepts_usual_names():
    assert g.normalize_account("@example_a") == "example_a"
    assert g.normalize_account("https://www.threads.com/@example.b") == "example.b"
    assert g.normalize_account("") == ""


@pytest.mark.parametrize("kwargs", [
    {"query": "a", "max_items": str(g.MAX_ITEMS_LIMIT + 1)},
    {"query": "a", "max_items": "１０"},
])
def test_max_items_limit(kwargs):
    with pytest.raises(g.InputError):
        g.build_search_args(**kwargs)
    assert g.build_search_args("a", max_items=str(g.MAX_ITEMS_LIMIT))[-1] == str(g.MAX_ITEMS_LIMIT)
    with pytest.raises(g.InputError):
        g.build_related_command("https://x.com/example_a/status/1", top=str(g.TOP_LIMIT + 1))


def test_is_allowed_output(tmp_path):
    dirs = g.output_dirs({"output": {"raw_dir": "data/raw", "digest_dir": "digest"}}, tmp_path)
    assert dirs == [str(tmp_path / "data/raw"), str(tmp_path / "digest"), str(tmp_path / "data/media")]

    def ok(path):
        return g.is_allowed_output(path, tmp_path, dirs)

    assert ok("data/raw/search_20260101_001.jsonl")
    assert ok(str(tmp_path / "digest" / "top_x_1_20260101.md"))
    assert ok("data/raw") and ok("data/media/search_20260101_001")
    assert ok("data/raw/a.csv")
    # 出力先フォルダの外・想定外の種類・ネットワーク上のパスは開かない
    for bad in ("", "config.yaml", "data/raw/../../config.yaml", "/etc/passwd", "data/raw/a.exe",
                "data/raw/a.lnk", "digest/a.md.bat", r"\\example.invalid\share\a.md",
                r"C:\Windows\System32\calc.exe", "data/rawx/a.md", "data/raw/a\x00.md"):
        assert not ok(bad), bad
    assert not g.is_allowed_output(None, tmp_path, dirs)


@pytest.mark.parametrize("value,expected", [
    ("=HYPERLINK(\"https://example.invalid\")", "'=HYPERLINK(\"https://example.invalid\")"),
    ("+1", "'+1"), ("-1", "'-1"), ("@example_a", "'@example_a"), ("  =1+1", "'  =1+1"), ("\t=1", "'\t=1"),
    ("本文", "本文"), ("12", "12"), ("", ""), ("a=b", "a=b"),
])
def test_csv_safe(value, expected):
    assert g.csv_safe(value) == expected


def test_write_csv_neutralizes_formulas(tmp_path):
    import csv
    import json

    src = tmp_path / "a.jsonl"
    src.write_text(json.dumps({"id": "1", "author_handle": "example_a", "author_name": "=cmd|' /C calc'!A0",
                               "text": "@SUM(1+1)", "like_count": 3, "=key": "v"}, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    out = tmp_path / "a.csv"
    g.write_csv([src], out)
    with open(out, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    for row in rows:
        for cell in row:
            assert not cell.startswith(("=", "+", "-", "@")), cell
    assert "'@SUM(1+1)" in rows[1] and "3" in rows[1]


def test_ensure_config_copies_example_once(tmp_path):
    assert g.ensure_config(tmp_path) is False  # ひな形が無ければ何もしない
    (tmp_path / "config.example.yaml").write_text("watch_accounts: []\n", encoding="utf-8")
    assert g.ensure_config(tmp_path) is True
    (tmp_path / "config.yaml").write_text("watch_accounts: [\"example_a\"]\n", encoding="utf-8")
    assert g.ensure_config(tmp_path) is False  # 既にある設定は上書きしない
    assert "example_a" in (tmp_path / "config.yaml").read_text(encoding="utf-8")


def test_config_example_is_neutral():
    import yaml
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    config = yaml.safe_load((root / "config.example.yaml").read_text(encoding="utf-8"))
    assert config["watch_accounts"] == [] and config["interest_keywords"] == []
    assert config["browser"] == "chromium"
    assert not config["session"]["cdp_url"] and not config["tiktok"]["cdp_url"]
    for sns in ("instagram", "threads", "tiktok"):
        assert config[sns]["watch_accounts"] == []
