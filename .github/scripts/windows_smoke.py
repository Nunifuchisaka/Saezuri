"""Windowsランナーでの動作確認（CI専用。利用者が実行するものではない）。

setup.bat でセットアップした直後の新しい環境で、次を確かめる。SNSにはログインせず、収集もしない。

1. GUIの経路（Api.run）で子プロセスのスクリプト（digest.py、合成データ）を動かし、終了コードと出力パスを拾えるか
2. 「ログイン用Chromeを開く」（Api.launch_chrome）でChromeを見つけて起動し、CDPでつながるか
3. GUI（pywebview＋Edge WebView2）を実際に開き、画面のJSからPython側（gui_api.Api）を呼べるか。
   各メニューの画面のスクリーンショットを撮る（手順書 docs/guide-windows.md の画像の元）

失敗したら終了コード1。
使い方: .venv\\Scripts\\python.exe .github\\scripts\\windows_smoke.py <スクリーンショットの保存先フォルダ>
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import gui  # noqa: E402
import gui_api  # noqa: E402

OUT_DIR = Path(sys.argv[1] if len(sys.argv) > 1 else "smoke-out").resolve()
WINDOW_TITLE = "saezuri - SNS投稿の収集"
results: dict[str, str] = {}

PAGES = ["setup", "search", "account", "fx", "related", "report", "settings"]


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


def window_rect(title: str):
    user32 = ctypes.windll.user32
    hwnd = user32.FindWindowW(None, title)
    if not hwnd:
        return None
    rect = wintypes.RECT()
    # 影の分を含まない見た目どおりの枠（DWMWA_EXTENDED_FRAME_BOUNDS=9）を優先する
    try:
        ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(rect), ctypes.sizeof(rect))
    except OSError:
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top


def capture(path: Path, rect=None) -> bool:
    """画面（rect指定時はその範囲）をPNGで保存する。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    if rect is None:
        box = "$b=[System.Windows.Forms.Screen]::PrimaryScreen.Bounds; $x=$b.X; $y=$b.Y; $w=$b.Width; $h=$b.Height"
    else:
        x, y, w, h = rect
        box = f"$x={x}; $y={y}; $w={w}; $h={h}"
    ps = ("Add-Type -AssemblyName System.Windows.Forms,System.Drawing; " + box + "; "
          "$bmp=New-Object System.Drawing.Bitmap $w,$h; $g=[System.Drawing.Graphics]::FromImage($bmp); "
          "$g.CopyFromScreen($x,$y,0,0,$bmp.Size); "
          f"$bmp.Save('{path}',[System.Drawing.Imaging.ImageFormat]::Png)")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=False, timeout=60)
    return path.exists()


def sample_jsonl() -> Path:
    raw = ROOT / "data" / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    sample = raw / "search_20261118_001.jsonl"
    posts = []
    for i, (text, likes) in enumerate([("新作リップの色がかわいい（合成データ）", 42),
                                       ("店頭で試してきました（合成データ）", 18),
                                       ("限定パッケージ届いた（合成データ）", 7)], start=1):
        posts.append({"id": f"100000000000000000{i}", "author_handle": f"example_user{i}",
                      "author_name": f"Example {i}", "text": text,
                      "created_at": f"Mon Nov 16 0{i}:28:32 +0000 2026",
                      "url": f"https://x.com/example_user{i}/status/100000000000000000{i}",
                      "like_count": likes, "repost_count": i, "reply_count": 1, "quote_count": 0,
                      "collected_at": "2026-11-18T12:00:00+09:00", "media": [], "source_query": "新作 リップ"})
    sample.write_text("".join(json.dumps(p, ensure_ascii=False) + "\n" for p in posts), encoding="utf-8")
    return sample


def run_job_check(sample: Path) -> None:
    api = gui_api.Api()
    res = api.run("digest", {"files": [str(sample)], "title": "Windows動作確認"})
    check("Api.runで子プロセス起動", bool(res.get("ok")), str(res))
    snap = wait_for(lambda: (lambda s: s if s.get("done") else None)(api.poll(0)), 60) or api.poll(0)
    lines = snap.get("lines") or []
    check("digest.pyが正常終了", snap.get("code") == 0, f"code={snap.get('code')} lines={lines}")
    outputs = snap.get("outputs") or []
    check("出力パスを拾える", any(str(o).endswith(".md") for o in outputs), str(outputs))
    csv_path = sample.with_suffix(".csv")
    api2 = gui_api.Api(opener=lambda p: None)
    res = api2.export_csv([str(sample)], open_after=False)
    check("CSVを書き出せる", bool(res.get("ok")) and csv_path.exists(), str(res))


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
        time.sleep(5)
        capture(OUT_DIR / "chrome_login.png")
    except Exception as exc:  # noqa: BLE001
        check("Chromeの確認", False, repr(exc))
    finally:
        subprocess.run(["taskkill", "/F", "/IM", "chrome.exe", "/T"], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def gui_check(sample: Path) -> None:
    import webview

    original_start = webview.start

    def start_with_probe(*args, **kwargs):
        def probe():
            try:
                window = webview.windows[0]
                window.events.loaded.wait(60)
                window.move(0, 0)
                api_ready = wait_for(lambda: window.evaluate_js(
                    "!!(window.pywebview && window.pywebview.api && window.pywebview.api.get_state)"), 30)
                check("画面からPython側を呼べる", bool(api_ready))
                login_text = wait_for(lambda: window.evaluate_js(
                    "document.getElementById('login-box').textContent.trim()"), 30)
                check("ログイン状況の表示", bool(login_text), repr((login_text or "")[:80]))
                title = window.evaluate_js("document.querySelector('#page-setup h1').textContent")
                check("日本語の見出しが出る", title == "準備（ログイン）", repr(title))
                rect = window_rect(WINDOW_TITLE)
                check("ウィンドウの位置", rect is not None, str(rect))
                for page in PAGES:
                    window.evaluate_js(f"show({json.dumps(page)})")
                    time.sleep(1.2)
                    capture(OUT_DIR / f"gui_{page}.png", rect)
                # 実行後の「実行状況」（まとめを作る）
                window.evaluate_js("show('report'); setFiles(" + json.dumps([str(sample)]) + ");"
                                   "document.getElementById('digest-title').value='11月第3週 新作リップの反応';"
                                   "document.getElementById('btn-digest').click();")
                done = wait_for(lambda: window.evaluate_js(
                    "document.getElementById('status-chip').textContent.includes('完了')"), 60)
                check("画面から実行して完了表示", bool(done))
                time.sleep(1.0)
                capture(OUT_DIR / "gui_report_done.png", rect)
                shots = sorted(p.name for p in OUT_DIR.glob("gui_*.png"))
                check("スクリーンショット", len(shots) >= len(PAGES), ", ".join(shots))
            except Exception as exc:  # noqa: BLE001
                check("GUIの確認", False, repr(exc))
            finally:
                for w in list(webview.windows):
                    w.destroy()

        kwargs["func"] = probe
        return original_start(*args, **kwargs)

    webview.start = start_with_probe
    threading.Timer(240, lambda: (check("GUIが時間内に終わらない", False), os._exit(1))).start()
    code = gui.main()
    check("gui.main()の終了コード", code == 0, str(code))


def main() -> int:
    # 手元で動かすと、Chromeを終了させ、data/raw・digestに合成データを残してしまう
    if not os.environ.get("CI"):
        print("これはCI専用です（Chromeを終了させ、data/rawに試験データを書き込むため）。", file=sys.stderr)
        return 1
    sample = sample_jsonl()
    run_job_check(sample)
    chrome_check()
    gui_check(sample)  # webview.start はメインスレッドを占有するので最後に行う
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    failed = [k for k, v in results.items() if v.startswith("NG")]
    print("失敗: " + ", ".join(failed) if failed else "すべてOK", flush=True)
    sys.stdout.flush()
    os._exit(1 if failed else 0)


if __name__ == "__main__":
    sys.exit(main())
