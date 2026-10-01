"""saezuri セッション管理ユーティリティ

storage_state(手動ログイン結果)のロード・ログイン状態検証・
レートリミット/チャレンジ画面検知の簡易ヘルパーを提供する。
収集モジュール(search.py 等)から import して使う想定。
GraphQLレスポンス傍受の中核実装はここでは扱わない(収集モジュール側の責務)。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

import yaml
from playwright.sync_api import Browser, BrowserContext

logger = logging.getLogger(__name__)

X_HOME_URL = "https://x.com/home"

# 起動可能なブラウザエンジン(Playwrightが提供する3種 + Brave)
SUPPORTED_BROWSERS = ("chromium", "firefox", "webkit", "brave")

# Brave実行ファイルの自動探索候補パス(Windows運用前提のため優先的にWindows候補を並べる)
BRAVE_DEFAULT_PATHS = (
    r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
    r"C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe"),
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/usr/bin/brave-browser",
)

# ログイン画面へのリダイレクトを判定するためのURLキーワード
LOGIN_URL_KEYWORDS = ("/login", "/i/flow/login")

# チャレンジ画面/アクセス制限画面を判定するためのURLキーワード
CHALLENGE_URL_KEYWORDS = ("challenge", "account/access")

RELOGIN_MESSAGE = "セッションが失効しています。login.pyを再実行して手動ログインし直してください。"


class SessionExpiredError(RuntimeError):
    """ログイン状態の検証に失敗した(セッション失効)ことを表す例外"""


class CdpConnectionError(RuntimeError):
    """CDP(--remote-debugging-port)での既存Chromeへの接続に失敗したことを表す例外"""


def load_config(path: str | Path = "config.yaml") -> dict:
    """config.yamlを読み込んで辞書として返す。"""
    path = Path(path)
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def resolve_cdp_url(cli_value: Optional[str], section: Optional[dict]) -> Optional[str]:
    """CDP接続先URLを決める。コマンドライン指定 > configセクションのcdp_url > None(CDPを使わない)。"""
    for value in (cli_value, (section or {}).get("cdp_url")):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def connect_cdp(playwright, cdp_url: str):
    """起動済みChromeにCDPで接続し、(browser, 既存context)を返す。

    接続失敗・contextが無い場合はCdpConnectionError(日本語メッセージ)を送出する。
    呼び出し側はbrowser.close()を呼ばないこと(管理人のChrome自体を閉じないため)。
    """
    try:
        browser = playwright.chromium.connect_over_cdp(cdp_url)
    except Exception as exc:
        raise CdpConnectionError(
            f"Chromeにデバッグポートで接続できません: {cdp_url}\n"
            "Chromeを `chrome.exe --remote-debugging-port=9222 --user-data-dir=<専用プロファイル>` で"
            "起動しているか、URLとポートが合っているか確認してください。"
            f"(詳細: {exc})"
        ) from exc
    if not browser.contexts:
        raise CdpConnectionError(
            f"接続先Chromeに利用できるブラウザコンテキストがありません: {cdp_url}"
        )
    return browser, browser.contexts[0]


def get_storage_state_path(config: dict) -> Path:
    """configからstorage_stateファイルのパスを取り出す。"""
    return Path(config["session"]["storage_state_path"])


def get_browser_name(config: dict) -> str:
    """config.yamlのbrowserキーを返す。欠如・不正時はデフォルト("chromium")。"""
    name = config.get("browser", "chromium")
    if name not in SUPPORTED_BROWSERS:
        logger.warning(
            "config: browserの値が不正のためデフォルト'chromium'を使います(指定値: %r)", name
        )
        return "chromium"
    return name


def get_browser_executable_path(config: dict) -> Optional[str]:
    """Brave実行ファイルのパスを決定する。

    config.yamlのbrowser_executable_pathキーが指定されていればそれを優先する。
    未指定ならBRAVE_DEFAULT_PATHSを順に確認し、最初に存在するものを返す。
    どれも見つからなければNoneを返す(例外は投げない)。
    """
    explicit = config.get("browser_executable_path")
    if explicit:
        return explicit
    for candidate in BRAVE_DEFAULT_PATHS:
        if candidate and Path(candidate).exists():
            return candidate
    return None


def launch_browser(
    playwright, name: str, headless: bool, executable_path: Optional[str] = None
) -> Browser:
    """指定したエンジン名(chromium/firefox/webkit/brave)でブラウザを起動する。

    name="brave"の場合はChromiumエンジンでexecutable_pathを指定して起動する
    (Braveのplaywright専用APIは存在しないため)。executable_pathが未指定(None)の
    場合はFileNotFoundErrorを送出する。
    """
    if name == "brave":
        if not executable_path:
            raise FileNotFoundError(
                "Braveの実行ファイルが見つかりません。"
                "config.yamlのbrowser_executable_pathで明示的にパスを指定してください。"
            )
        return playwright.chromium.launch(headless=headless, executable_path=executable_path)
    return getattr(playwright, name).launch(headless=headless)


def new_context(browser: Browser, config: dict, **kwargs) -> BrowserContext:
    """storage_stateをロードしてBrowserContextを生成する。

    login.py未実行でstorage_stateファイルが存在しない場合はFileNotFoundErrorを送出する。
    kwargsはbrowser.new_context()にそのまま渡す(viewport指定等の拡張用)。
    """
    storage_state_path = get_storage_state_path(config)
    if not storage_state_path.exists():
        raise FileNotFoundError(
            f"storage_stateファイルが見つかりません: {storage_state_path}\n"
            "先にlogin.pyを実行して手動ログインしてください。"
        )
    return browser.new_context(storage_state=str(storage_state_path), **kwargs)


def verify_login(context: BrowserContext) -> bool:
    """軽くログイン状態を検証する。

    x.com/home を開き、ログイン画面へリダイレクトされていないかをURLで判定する簡易版。
    厳密なAPI呼び出しでの検証はGraphQL傍受を実装する収集モジュール側で必要に応じて強化する。
    """
    page = context.new_page()
    try:
        page.goto(X_HOME_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
        current_url = page.url
    finally:
        page.close()
    return not _is_login_url(current_url)


def _is_login_url(url: str) -> bool:
    return any(keyword in url for keyword in LOGIN_URL_KEYWORDS)


def require_login(context: BrowserContext) -> None:
    """verify_login()がFalseの場合にSessionExpiredErrorを送出する。

    収集モジュールの冒頭で呼び出し、失効時は即座に処理を中断する想定。
    自動での再ログインは行わない。
    """
    if not verify_login(context):
        raise SessionExpiredError(RELOGIN_MESSAGE)


def is_rate_limited(status_code: int) -> bool:
    """レスポンスstatusがレートリミット(429)かどうかを判定する。"""
    return status_code == 429


def is_challenge_url(url: str) -> bool:
    """URLがチャレンジ画面/アクセス制限画面らしきものかを簡易判定する。"""
    return any(keyword in url for keyword in CHALLENGE_URL_KEYWORDS)
