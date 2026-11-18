"""gui_api.Api（GUI画面とPythonの橋渡し）の単体テスト。実際のスクリプトは起動しない。値はすべて架空。"""

import io
import subprocess
import time
from pathlib import Path

import yaml

import gui_api

CONFIG = """watch_accounts: ["example_a"]
interest_keywords: ["foo"]
session:
  storage_state_path: session/x_storage_state.json
  cdp_url: http://127.0.0.1:9222
instagram:
  watch_accounts: []
  storage_state_path: session/instagram_storage_state.json
threads:
  watch_accounts: []
  storage_state_path: session/threads_storage_state.json
tiktok:
  watch_accounts: []
  storage_state_path: session/tiktok_storage_state.json
  cdp_url:
browser: brave
"""


class FakeProc:
    def __init__(self, lines, code=0):
        self.stdout = io.StringIO("".join(line + "\n" for line in lines))
        self._code = code
        self.terminated = False

    def wait(self):
        return self._code

    def terminate(self):
        self.terminated = True


class FakePopen:
    def __init__(self, lines=(), code=0):
        self.calls = []
        self.lines = list(lines)
        self.code = code

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        return FakeProc(self.lines, self.code)


class FakeDialogs:
    def __init__(self, open_result=(), save_result=None):
        self.open_result = list(open_result)
        self.save_result = save_result

    def open_files(self, initial_dir):
        return self.open_result

    def save_file(self, initial_dir, filename):
        return self.save_result


def make_api(tmp_path, popen=None, dialogs=None, alive=False, **kwargs):
    (tmp_path / "config.yaml").write_text(CONFIG, encoding="utf-8")
    opened = []
    api = gui_api.Api(root=tmp_path, dialogs=dialogs, popen=popen or FakePopen(),
                      opener=opened.append, cdp_check=lambda: alive, **kwargs)
    return api, opened


def wait_done(api, since=0):
    for _ in range(100):
        res = api.poll(since)
        if res.get("done"):
            return res
        time.sleep(0.01)
    raise AssertionError("終わらない")


def test_get_state(tmp_path):
    api, _ = make_api(tmp_path, alive=True)
    state = api.get_state()
    assert state["ok"] and state["config_found"]
    assert state["accounts"]["x"] == ["example_a"]
    assert state["keywords"] == ["foo"]
    assert state["use_cdp"] is True and state["browser"] == "brave"
    login = {s["sns"]: s["state"] for s in state["login"]}
    assert login == {"x": "ok", "instagram": "off", "threads": "off", "tiktok": "off"}
    assert state["running"] is False


def test_run_builds_command_and_collects_output(tmp_path):
    popen = FakePopen(["検索クエリ: 新作", "取得: 2件", "保存先: data/raw/search_20260101_001.jsonl"])
    api, _ = make_api(tmp_path, popen=popen)
    res = api.run("search", {"query": "新作", "mode": "top", "max": "20"})
    assert res == {"ok": True, "title": "Xを検索"}
    cmd, kwargs = popen.calls[0]
    assert Path(cmd[2]).name == "search.py"
    assert cmd[3:] == ["--query=新作", "--mode", "top", "--max", "20"]
    assert kwargs["cwd"] == tmp_path and kwargs["env"]["PYTHONIOENCODING"] == "utf-8"
    done = wait_done(api)
    assert done["code"] == 0 and done["status"] == "完了しました"
    assert done["lines"][-1].startswith("保存先:")
    assert done["outputs"] == ["data/raw/search_20260101_001.jsonl"]
    assert done["last_files"] == [str(tmp_path / "data/raw/search_20260101_001.jsonl")]
    assert api.get_state()["last_files"] == done["last_files"]
    # 差分取得
    assert api.poll(done["next"])["lines"] == []


def test_run_input_error_and_unknown(tmp_path):
    popen = FakePopen()
    api, _ = make_api(tmp_path, popen=popen)
    assert api.run("search", {"query": ""}) == {"ok": False, "error": "キーワードか投稿者のどちらかを入力してください。"}
    assert api.run("nope", {})["ok"] is False
    assert api.run("login", {"sns": "bad"})["ok"] is False
    assert popen.calls == []


def test_run_login_title(tmp_path):
    popen = FakePopen()
    api, _ = make_api(tmp_path, popen=popen)
    assert api.run("login", {"sns": "instagram"})["title"] == "Instagramにログイン"
    assert Path(popen.calls[0][0][2]).name == "ig_login.py"


def test_exit_code_message(tmp_path):
    api, _ = make_api(tmp_path, popen=FakePopen(["エラー: x"], code=3))
    api.run("related", {"url": "https://x.com/example_a/status/1"})
    done = wait_done(api)
    assert done["status"] == "アクセス過多のため止めました"
    assert "保存済み" in done["message"]


def test_one_job_at_a_time_and_stop(tmp_path):
    class SlowProc(FakeProc):
        def __init__(self):
            super().__init__([])
            self.stdout = self
            self.closed = False

        def __iter__(self):
            while not self.terminated:
                time.sleep(0.01)
            return iter(())

        def wait(self):
            return -15

    proc = SlowProc()
    api, _ = make_api(tmp_path, popen=lambda cmd, **kw: proc)
    assert api.run("account", {"sns": "x", "kind": "posts"})["ok"]
    second = api.run("account", {"sns": "x", "kind": "posts"})
    assert second["ok"] is False and "実行中" in second["error"]
    assert api.save_settings({})["ok"] is False
    assert api.stop() == {"ok": True}
    done = wait_done(api)
    assert done["code"] is None and done["status"] == "中止しました"
    assert api.stop()["ok"] is False


def test_export_csv_and_open(tmp_path):
    src = tmp_path / "a.jsonl"
    src.write_text('{"id": "1", "author_handle": "example_a", "text": "t", "created_at": "2026-01-01T00:00:00Z"}\n',
                   encoding="utf-8")
    out = tmp_path / "out.csv"
    api, opened = make_api(tmp_path, dialogs=FakeDialogs(save_result=str(out)))
    res = api.export_csv([str(src)])
    assert res == {"ok": True, "path": str(out), "rows": 1}
    assert opened == [out]
    cancelled, _ = make_api(tmp_path, dialogs=FakeDialogs(save_result=None))
    assert cancelled.export_csv([str(src)]) == {"ok": False, "cancelled": True}
    assert api.export_csv([])["ok"] is False


def test_open_path_and_folder(tmp_path):
    api, opened = make_api(tmp_path)
    assert api.open_path("missing.md")["ok"] is False
    (tmp_path / "digest").mkdir()
    assert api.open_folder("digest") == {"ok": True}
    assert opened == [tmp_path / "digest"]
    assert api.open_folder("etc")["ok"] is False


def test_choose_files(tmp_path):
    api, _ = make_api(tmp_path, dialogs=FakeDialogs(open_result=["a.jsonl"]))
    assert api.choose_files() == {"ok": True, "files": ["a.jsonl"]}
    no_dialog, _ = make_api(tmp_path)
    assert no_dialog.choose_files()["ok"] is False


def test_save_settings(tmp_path):
    api, _ = make_api(tmp_path)
    res = api.save_settings({
        "accounts": {"x": "@x_one\nx_two", "instagram": "https://www.instagram.com/ig_one/", "threads": "", "tiktok": ""},
        "keywords": "新作\nキャンペーン",
        "use_cdp": False,
        "browser": "chromium",
    })
    assert res["ok"]
    data = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    assert data["watch_accounts"] == ["x_one", "x_two"]
    assert data["instagram"]["watch_accounts"] == ["ig_one"]
    assert data["interest_keywords"] == ["新作", "キャンペーン"]
    assert data["session"]["cdp_url"] is None and data["browser"] == "chromium"
    assert (tmp_path / "config.yaml.bak").read_text(encoding="utf-8") == CONFIG
    assert res["state"]["use_cdp"] is False
    assert api.save_settings({"browser": "netscape"})["ok"] is False


def test_launch_chrome(tmp_path, monkeypatch):
    popen = FakePopen()
    api, _ = make_api(tmp_path, popen=popen)
    monkeypatch.setattr(gui_api.g, "find_chrome", lambda: None)
    assert api.launch_chrome()["ok"] is False
    monkeypatch.setattr(gui_api.g, "find_chrome", lambda: "chrome")
    # すでにCDP設定ありなら設定は変えない
    assert api.launch_chrome() == {"ok": True, "enabled_cdp": False}
    cmd = popen.calls[-1][0]
    assert cmd[0] == "chrome" and "--remote-debugging-port=9222" in cmd
    assert f"--user-data-dir={tmp_path / 'chrome-profile'}" in cmd
    # CDP設定なしなら有効にする
    text = (tmp_path / "config.yaml").read_text(encoding="utf-8").replace("cdp_url: http://127.0.0.1:9222", "cdp_url:")
    (tmp_path / "config.yaml").write_text(text, encoding="utf-8")
    assert api.launch_chrome() == {"ok": True, "enabled_cdp": True}
    assert api.get_state()["use_cdp"] is True


def test_popen_oserror(tmp_path):
    def boom(cmd, **kw):
        raise OSError("no python")

    api, _ = make_api(tmp_path, popen=boom)
    res = api.run("search", {"query": "a"})
    assert res["ok"] is False and "no python" in res["error"]
    assert api.poll(0) == {"ok": True, "idle": True}


def test_no_public_attributes_besides_methods(tmp_path):
    """pywebviewは公開属性を画面に出すので、内部状態は全て_付きにしておく。"""
    api, _ = make_api(tmp_path)
    public = [k for k in vars(api) if not k.startswith("_")]
    assert public == []
    assert isinstance(subprocess.PIPE, int)


def test_untrusted_output_lines_are_ignored(tmp_path):
    # 投稿本文など他人が書いた文字列が「出力:」「保存先:」に見せかけても、出力先フォルダの外は覚えない
    popen = FakePopen([
        "出力: \\\\example.invalid\\share\\a.lnk",
        "保存先: \\\\example.invalid\\share\\a.jsonl",
        "出力: C:\\Windows\\System32\\calc.exe",
        "上位Markdown: ../outside.md",
        "  出力: digest/indented.md",
        "保存先: data/raw/fx_20260101_001.jsonl",
    ])
    api, opened = make_api(tmp_path, popen=popen)
    api.run("fx", {"urls": "https://x.com/example_a/status/1234567890"})
    done = wait_done(api)
    assert done["outputs"] == ["data/raw/fx_20260101_001.jsonl"]
    assert done["last_files"] == [str(tmp_path / "data/raw/fx_20260101_001.jsonl")]


def test_open_path_only_inside_output_dirs(tmp_path):
    api, opened = make_api(tmp_path)
    (tmp_path / "digest").mkdir()
    (tmp_path / "digest" / "a.md").write_text("x", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("x", encoding="utf-8")
    (tmp_path / "digest" / "a.exe").write_text("x", encoding="utf-8")
    assert api.open_path("digest/a.md")["ok"] is True
    for bad in ("secret.txt", "config.yaml", "digest/../secret.txt", "digest/a.exe", str(tmp_path / "secret.txt"),
                "\\\\example.invalid\\share\\a.md"):
        res = api.open_path(bad)
        assert res["ok"] is False and "外" in res["error"], bad
    assert opened == [tmp_path / "digest" / "a.md"]
    assert api.open_folder("digest")["ok"] is True


def test_cooldown_after_rate_limit(tmp_path):
    now = [1000.0]
    popen = FakePopen(["エラー: x"], code=3)
    api, _ = make_api(tmp_path, popen=popen, clock=lambda: now[0])
    api.run("search", {"query": "a"})
    wait_done(api)
    # 止められた直後は、SNSを開く収集はどれも実行できない
    for task, params in (("search", {"query": "a"}), ("account", {}),
                         ("related", {"url": "https://x.com/example_a/status/1"})):
        res = api.run(task, params)
        assert res["ok"] is False and "30分" in res["error"], task
    assert len(popen.calls) == 1
    # まとめ作成などSNSに触れない操作は止めない
    assert api.run("digest", {"files": ["a.jsonl"]})["ok"] is True
    wait_done(api)
    now[0] += gui_api.COOLDOWN_SECONDS - 61
    assert "2分" in api.run("search", {"query": "a"})["error"]
    now[0] += 61
    popen.code = 0
    assert api.run("search", {"query": "a"})["ok"] is True


def test_no_cooldown_after_normal_exit(tmp_path):
    popen = FakePopen(["エラー: x"], code=1)
    api, _ = make_api(tmp_path, popen=popen)
    api.run("search", {"query": "a"})
    wait_done(api)
    assert api.run("search", {"query": "a"})["ok"] is True


def test_save_settings_rejects_odd_account(tmp_path):
    api, _ = make_api(tmp_path)
    res = api.save_settings({"accounts": {"x": "../../x"}, "keywords": "", "use_cdp": False, "browser": "chromium"})
    assert res["ok"] is False and "アカウント名" in res["error"]
    assert (tmp_path / "config.yaml").read_text(encoding="utf-8") == CONFIG
