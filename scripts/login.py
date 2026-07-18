"""saezuri 手動ログイン・storage_state保存スクリプト

指定ブラウザ(既定: config.yamlのbrowser、既定値chromium)をheadless=Falseで起動し、
ユーザーが手動でサブ垢にログインするのを待つ。
ログイン成功(x.com/homeへの遷移)を検知後、storage_state(Cookie+localStorage)を
config.yamlで指定されたパスに保存してブラウザを閉じる。ID/PWの自動入力は行わない
(自動ログイン試行自体がBANトリガーになりやすいため)。CLIとして直接実行する。
"""
from __future__ import annotations

import argparse
import sys

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import Page, sync_playwright

from session import (
    SUPPORTED_BROWSERS,
    X_HOME_URL,
    get_browser_executable_path,
    get_browser_name,
    get_storage_state_path,
    launch_browser,
    load_config,
)

LOGIN_URL = "https://x.com/login"
LOGIN_WAIT_TIMEOUT_MS = 5 * 60 * 1000  # 5分


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="手動ログイン・storage_state保存")
    parser.add_argument("--config", default="config.yaml", help="設定ファイルのパス(既定: config.yaml)")
    parser.add_argument("--browser", choices=SUPPORTED_BROWSERS, default=None,
                        help="起動するブラウザエンジン(省略時はconfig.yamlのbrowser)")
    return parser


def wait_for_login(page: Page) -> bool:
    """ログイン成功(x.com/homeへの遷移)をポーリングで待つ。タイムアウトしたらFalseを返す。"""
    try:
        page.wait_for_url(f"{X_HOME_URL}*", timeout=LOGIN_WAIT_TIMEOUT_MS)
        return True
    except PlaywrightTimeoutError:
        return False


def run_login(config_path: str = "config.yaml", browser_name: str | None = None) -> None:
    """手動ログインを実行し、成功したらstorage_stateを保存する。"""
    config = load_config(config_path)
    storage_state_path = get_storage_state_path(config)
    storage_state_path.parent.mkdir(parents=True, exist_ok=True)
    browser_name = browser_name or get_browser_name(config)

    with sync_playwright() as playwright:
        browser = launch_browser(
            playwright, browser_name, headless=False,
            executable_path=get_browser_executable_path(config),
        )
        context = browser.new_context()
        page = context.new_page()
        page.goto(LOGIN_URL)

        print("ブラウザでサブ垢に手動ログインしてください(最大5分待機します)...")
        logged_in = wait_for_login(page)

        if not logged_in:
            print(
                "エラー: ログインを検知できませんでした(タイムアウト)。"
                "もう一度login.pyを実行してください。",
                file=sys.stderr,
            )
            browser.close()
            sys.exit(1)

        context.storage_state(path=str(storage_state_path))
        print(f"ログインを確認しました。storage_stateを保存しました: {storage_state_path}")
        browser.close()


if __name__ == "__main__":
    args = build_arg_parser().parse_args()
    run_login(args.config, args.browser)
