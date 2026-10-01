"""saezuri TikTok 手動ログイン・storage_state保存スクリプト

login.py(X用)のTikTok版。指定ブラウザ(既定: config.yamlのbrowser)をheadless=Falseで
起動し、ユーザーが手動でTikTokにログインするのを待つ。ログイン成功(tiktok.comの
sessionid Cookieの付与)を検知したら、storage_stateをconfig.yamlのtiktok.storage_state_path
(既定: session/tiktok_storage_state.json)に保存してブラウザを閉じる。
ログイン中にCAPTCHAが出た場合は人間が手動で解く(自動突破はしない)。
ID/PWの自動入力は行わない。

使用例:
    python scripts/tt_login.py
"""
from __future__ import annotations

import argparse
import sys
import time

from playwright.sync_api import sync_playwright

from session import (
    SUPPORTED_BROWSERS,
    get_browser_executable_path,
    get_browser_name,
    launch_browser,
    load_config,
)
from tiktok import TIKTOK_LOGIN_URL, get_tiktok_storage_state_path, has_tiktok_session_cookie

LOGIN_WAIT_TIMEOUT_SECONDS = 5 * 60  # 5分
POLL_INTERVAL_MS = 2000
# sessionid付与直後は追加の確認画面が続くことがあるため、検知後に少し待ってから保存する
SETTLE_WAIT_MS = 3000


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="TikTok 手動ログイン・storage_state保存")
    parser.add_argument("--config", default="config.yaml", help="設定ファイルのパス(既定: config.yaml)")
    parser.add_argument("--browser", choices=SUPPORTED_BROWSERS, default=None,
                        help="起動するブラウザエンジン(省略時はconfig.yamlのbrowser)")
    return parser


def wait_for_login(context, page) -> bool:
    """sessionid Cookieが付与されるまでポーリングで待つ。タイムアウトしたらFalseを返す。"""
    deadline = time.monotonic() + LOGIN_WAIT_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if has_tiktok_session_cookie(context.cookies()):
            return True
        page.wait_for_timeout(POLL_INTERVAL_MS)
    return False


def run_login(config_path: str = "config.yaml", browser_name: str | None = None) -> int:
    config = load_config(config_path)
    storage_state_path = get_tiktok_storage_state_path(config)
    storage_state_path.parent.mkdir(parents=True, exist_ok=True)
    browser_name = browser_name or get_browser_name(config)

    with sync_playwright() as playwright:
        browser = launch_browser(
            playwright, browser_name, headless=False,
            executable_path=get_browser_executable_path(config),
        )
        try:
            context = browser.new_context()
            page = context.new_page()
            page.goto(TIKTOK_LOGIN_URL)

            print("ブラウザでTikTokのサブ垢に手動ログインしてください(最大5分待機します。CAPTCHAが出たら手動で対応してください)...")
            if not wait_for_login(context, page):
                print(
                    "エラー: ログインを検知できませんでした(タイムアウト)。"
                    "もう一度tt_login.pyを実行してください。",
                    file=sys.stderr,
                )
                return 1

            page.wait_for_timeout(SETTLE_WAIT_MS)
            context.storage_state(path=str(storage_state_path))
            print(f"ログインを確認しました。storage_stateを保存しました: {storage_state_path}")
            return 0
        finally:
            browser.close()


if __name__ == "__main__":
    args = build_arg_parser().parse_args()
    sys.exit(run_login(args.config, args.browser))
