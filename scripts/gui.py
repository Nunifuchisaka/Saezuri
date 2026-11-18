"""saezuri GUI（非エンジニア向けの操作画面）

画面は scripts/webui/ のHTML/CSS/JS、処理は gui_api.Api（既存スクリプトを1本ずつ呼ぶだけ）。
pywebview で専用ウィンドウとして開く（WindowsはEdge WebView2を使う。Windows 10/11には標準で入っている）。
収集のやり方（オンデマンド実行・自動ログインなし・429やCAPTCHA検知で即中断）は各スクリプトのまま。

macOSはOS標準のWebKit（WKWebView）を使う（pywebviewがpyobjc経由で呼ぶ。追加インストール不要）。
起動: プロジェクトルートの `start_gui.bat`（Windows）／`start_gui.command`（macOS）、または `python scripts/gui.py`
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import gui_api

WEBUI_DIR = Path(__file__).resolve().parent / "webui"


def _alert(text: str) -> None:
    """pywebviewが無いときなど、画面を出せない場合の通知（pythonw起動でも見えるように）。"""
    if sys.platform.startswith("win"):
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, text, "saezuri", 0x10)
        return
    print(text, file=sys.stderr)
    if sys.platform == "darwin":
        # ターミナルを閉じた後でも見えるように、OS標準のダイアログでも出す
        import subprocess

        # 文字列はAppleScriptに埋め込まず引数で渡す（引用符などで壊れないように）
        cmd = ["osascript", "-e", "on run argv",
               "-e", 'display dialog (item 1 of argv) with title "saezuri" buttons {"OK"} with icon caution',
               "-e", "end run", text]
        try:
            subprocess.run(cmd, timeout=600, check=False)
        except (OSError, subprocess.SubprocessError):
            pass


class WebviewDialogs:
    """gui_api.Dialogs の pywebview 実装。"""

    def __init__(self, webview_module, window):
        self._webview = webview_module
        self._window = window

    def _kind(self, name: str):
        dialog = getattr(self._webview, "FileDialog", None)
        if dialog is not None:
            return getattr(dialog, name)
        return getattr(self._webview, f"{name}_DIALOG")

    def open_files(self, initial_dir: str) -> list[str]:
        result = self._window.create_file_dialog(
            self._kind("OPEN"), directory=initial_dir, allow_multiple=True,
            file_types=("収集データ (*.jsonl)", "すべてのファイル (*.*)"))
        return list(result or [])

    def save_file(self, initial_dir: str, filename: str) -> Optional[str]:
        result = self._window.create_file_dialog(
            self._kind("SAVE"), directory=initial_dir, save_filename=filename,
            file_types=("CSV (*.csv)",))
        if not result:
            return None
        return result if isinstance(result, str) else result[0]


def main() -> int:
    try:
        import webview
    except ImportError:
        _alert("画面の部品（pywebview）が入っていません。saezuriのフォルダの setup.bat（Macは setup.command）をダブルクリックしてから、"
               "もう一度起動してください。")
        return 1
    try:
        gui_api.g.ensure_config(gui_api.ROOT)  # 初回は config.example.yaml から作る
    except OSError:
        pass
    if not (gui_api.ROOT / "config.yaml").exists():
        _alert(f"設定ファイルが見つかりません: {gui_api.ROOT / 'config.yaml'}")
        return 1

    api = gui_api.Api()
    window = webview.create_window(
        "saezuri - SNS投稿の収集", str(WEBUI_DIR / "index.html"), js_api=api,
        width=1200, height=820, min_size=(900, 640), background_color="#eef3f1")
    api._dialogs = WebviewDialogs(webview, window)

    def on_closing():
        # 実行中に閉じたら、収集中のスクリプトも止める（途中分は保存されないことがある）
        job = api._job
        if job is not None and not job.done:
            job.stopped = True
            job.proc.terminate()

    window.events.closing += on_closing
    webview.start()
    return 0


if __name__ == "__main__":
    sys.exit(main())
