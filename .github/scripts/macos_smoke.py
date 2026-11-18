"""macOSランナーでの動作確認（CI専用。利用者が実行するものではない）。

1. GUI（pywebview＋WKWebView）を実際に開き、画面のJSからPython側（gui_api.Api）を呼べることを確かめて
   スクリーンショットを撮る
2. GUIの経路（Api.run）で子プロセスのスクリプト（digest.py、合成データ）を動かし、終了コードと出力パスの拾い上げを確かめる
3. 「ログイン用Chromeを開く」（Api.launch_chrome）でMacのChromeを見つけて起動し、CDPでつながることを確かめる

SNSにはログインせず、収集もしない。失敗したら終了コード1。
使い方: .venv/bin/python .github/scripts/macos_smoke.py <スクリーンショットの保存先フォルダ>
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import gui  # noqa: E402
import gui_api  # noqa: E402

OUT_DIR = Path(sys.argv[1] if len(sys.argv) > 1 else "smoke-out").resolve()
results: dict[str, str] = {}


def check(name: str, ok: bool, detail: str = "") -> None:
    results[name] = ("OK" if ok else "NG") + (f": {detail}" if detail else "")
    print(f"[{'OK' if ok else 'NG'}] {name} {detail}", flush=True)


def wait_for(fn, timeout: float, interval: float = 0.5):
    end = time.time() + timeout
    while time.time() < end:
        value = fn()
        if value:
            return value
        time.sleep(interval)
    return fn()


def gui_check() -> None:
    import webview

    original_start = webview.start

    def start_with_probe(*args, **kwargs):
        def probe():
            try:
                window = webview.windows[0]
                window.events.loaded.wait(60)
                api_ready = wait_for(lambda: window.evaluate_js(
                    "!!(window.pywebview && window.pywebview.api && window.pywebview.api.get_state)"), 30)
                check("画面からPython側を呼べる", bool(api_ready))
                # 起動時に get_state の結果で左下のログイン状況が描かれる（JS→Python→JSの往復）
                login_text = wait_for(lambda: window.evaluate_js(
                    "document.getElementById('login-box').textContent.trim()"), 30)
                check("ログイン状況の表示", bool(login_text), repr((login_text or "")[:80]))
                title = window.evaluate_js("document.querySelector('#page-setup h1').textContent")
                check("日本語の見出しが出る", title == "準備（ログイン）", repr(title))
                time.sleep(1.5)
                OUT_DIR.mkdir(parents=True, exist_ok=True)
                shot = OUT_DIR / "gui_macos.png"
                subprocess.run(["screencapture", "-x", str(shot)], check=False, timeout=30)
                check("スクリーンショット", shot.exists(), str(shot))
            except Exception as exc:  # noqa: BLE001
                check("GUIの確認", False, repr(exc))
            finally:
                for w in list(webview.windows):
                    w.destroy()

        kwargs["func"] = probe
        return original_start(*args, **kwargs)

    webview.start = start_with_probe
    threading.Timer(180, lambda: (check("GUIが時間内に終わらない", False), subprocess.run(
        ["kill", "-9", str(__import__("os").getpid())]))).start()
    code = gui.main()
    check("gui.main()の終了コード", code == 0, str(code))


def run_job_check() -> None:
    raw = ROOT / "data" / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    sample = raw / "smoke_sample.jsonl"
    post = {"id": "1000000000000000001", "author_handle": "example_user", "author_name": "Example",
            "text": "合成データの投稿です", "created_at": "Mon Nov 16 03:28:32 +0000 2026",
            "url": "https://x.com/example_user/status/1000000000000000001",
            "like_count": 3, "repost_count": 1, "reply_count": 0, "quote_count": 0,
            "collected_at": "2026-11-16T12:00:00+09:00", "media": [], "source_query": "smoke"}
    sample.write_text(json.dumps(post, ensure_ascii=False) + "\n", encoding="utf-8")
    api = gui_api.Api()
    res = api.run("digest", {"files": [str(sample)], "title": "macOS動作確認"})
    check("Api.runで子プロセス起動", bool(res.get("ok")), str(res))
    snap = wait_for(lambda: (lambda s: s if s.get("done") else None)(api.poll(0)), 60)
    snap = snap or api.poll(0)
    lines = snap.get("lines") or []
    check("digest.pyが正常終了", snap.get("code") == 0 and any("ポスト: 1件" in l for l in lines), f"code={snap.get('code')} lines={snap.get('lines')}")
    outputs = snap.get("outputs") or []
    check("出力パスを拾える", any(str(o).endswith(".md") for o in outputs), str(outputs))


def chrome_check() -> None:
    api = gui_api.Api()
    res = api.launch_chrome()
    check("ログイン用Chromeを起動", bool(res.get("ok")), str(res))
    if not res.get("ok"):
        return
    try:
        alive = wait_for(lambda: gui_api.cdp_alive(timeout=1.0), 60, 1.0)
        check("デバッグポート(9222)で待ち受け", bool(alive))
        from playwright.sync_api import sync_playwright

        from session import connect_cdp
        with sync_playwright() as p:
            browser, context = connect_cdp(p, gui_api.g.DEFAULT_CDP_URL)
            check("PlaywrightからCDP接続", context is not None, f"pages={len(context.pages)}")
    except Exception as exc:  # noqa: BLE001
        check("Chromeの確認", False, repr(exc))
    finally:
        subprocess.run(["pkill", "-f", gui_api.g.CHROME_PROFILE_DIRNAME], check=False)


def main() -> int:
    steps = sys.argv[2].split(",") if len(sys.argv) > 2 else ["job", "chrome", "gui"]
    if "job" in steps:
        run_job_check()
    if "chrome" in steps:
        chrome_check()
    if "gui" in steps:
        gui_check()  # webview.start はメインスレッドを占有するので最後に行う
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    failed = [k for k, v in results.items() if v.startswith("NG")]
    print("失敗: " + ", ".join(failed) if failed else "すべてOK", flush=True)
    sys.stdout.flush()
    import os
    os._exit(1 if failed else 0)  # 残ったタイマースレッドを待たずに終える


if __name__ == "__main__":
    main()
