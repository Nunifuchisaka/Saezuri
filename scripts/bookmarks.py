"""saezuri サブ垢自身のブックマーク収集

x.com/i/bookmarks を開き、BookmarksオペレーションのGraphQLレスポンスを傍受して
収集する。state/bookmarks_last_id.jsonに前回取得済み最新IDを保存し、それより
新しい投稿のみを差分保存する。結果は data/raw/bookmarks_<日付>.jsonl に追記する。

制約(設計書「リスク・留意点」参照):
    取得できるのはこのプログラムでログインしているサブ垢自身がブックマーク
    したものに限る。本垢の実ブックマークは対象外。

使用例:
    python scripts/bookmarks.py

終了コード: 0=正常 / 1=設定・環境エラー / 2=セッション失効 /
3=レートリミット(429)検知 / 4=チャレンジ画面検知
(3・4の場合も検知時点までの収集分は保存してから終了する)
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

import storage
from capture import (
    ChallengeDetectedError,
    GraphQLCapture,
    RateLimitedError,
    get_max_items,
    get_wait_seconds_range,
    now_iso,
    parse_timeline_payload,
    scroll_and_collect,
)
from session import (
    SUPPORTED_BROWSERS,
    SessionExpiredError,
    get_browser_executable_path,
    get_browser_name,
    launch_browser,
    load_config,
    new_context,
    require_login,
)
from user_tweets import dedupe_by_id, filter_new_posts, max_post_id

logger = logging.getLogger(__name__)

BOOKMARKS_URL = "https://x.com/i/bookmarks"

# state/<account>_last_id.json のaccount部に使う固定キー
LAST_ID_KEY = "bookmarks"

EXIT_ERROR = 1
EXIT_SESSION_EXPIRED = 2
EXIT_RATE_LIMITED = 3
EXIT_CHALLENGE = 4


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="サブ垢自身のブックマークの差分収集(Bookmarks傍受)。"
                    "ログイン中のサブ垢が付けたブックマークのみ対象(本垢のブックマークは取得不可)。"
    )
    parser.add_argument("--max", type=int, default=None,
                        help="取得上限件数(省略時はconfig.yamlのmax_items)")
    parser.add_argument("--config", default="config.yaml", help="設定ファイルのパス(既定: config.yaml)")
    parser.add_argument("--browser", choices=SUPPORTED_BROWSERS, default=None,
                        help="起動するブラウザエンジン(省略時はconfig.yamlのbrowser)")
    parser.add_argument("--headed", action="store_true", help="ブラウザを可視状態で起動する(デバッグ用)")
    return parser


def run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    raw_dir = Path(config["output"]["raw_dir"])
    state_dir = Path(config["output"]["state_dir"])
    max_items = args.max if args.max is not None else get_max_items(config)
    wait_range = get_wait_seconds_range(config)
    browser_name = args.browser or get_browser_name(config)
    collected_at = now_iso()

    # scroll_and_collect内で例外中断してもparse済み分を保存できるよう、外側にも溜める
    buffer: list = []

    def parse(payload: dict, operation: str) -> list:
        posts = parse_timeline_payload(
            payload, operation, source_query=LAST_ID_KEY, collected_at=collected_at
        )
        buffer.extend(posts)
        return posts

    posts: list = []
    exit_code = 0
    error_message = None
    try:
        with sync_playwright() as playwright:
            browser = launch_browser(
                playwright, browser_name, headless=not args.headed,
                executable_path=get_browser_executable_path(config),
            )
            capture = None
            page = None
            try:
                context = new_context(browser, config)
                require_login(context)
                page = context.new_page()
                capture = GraphQLCapture(operations=("Bookmarks",))
                capture.attach(page)
                page.goto(BOOKMARKS_URL, wait_until="domcontentloaded")
                posts = scroll_and_collect(
                    page,
                    capture,
                    parse,
                    max_items=max_items,
                    wait_seconds_range=wait_range,
                )
            finally:
                if capture is not None and page is not None:
                    try:
                        capture.detach(page)
                    except Exception as exc:
                        logger.warning("GraphQLCaptureのdetachに失敗しました: %s", exc)
                browser.close()
    except FileNotFoundError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except SessionExpiredError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return EXIT_SESSION_EXPIRED
    except RateLimitedError as exc:
        posts = dedupe_by_id(buffer)
        exit_code = EXIT_RATE_LIMITED
        error_message = str(exc)
    except ChallengeDetectedError as exc:
        posts = dedupe_by_id(buffer)
        exit_code = EXIT_CHALLENGE
        error_message = str(exc)

    last_id = storage.load_last_id(state_dir, LAST_ID_KEY)
    new_posts = filter_new_posts(posts, last_id)

    written = 0
    out_path = raw_dir / f"bookmarks_{datetime.now().strftime('%Y%m%d')}.jsonl"
    if new_posts:
        written = storage.append_jsonl(out_path, [post.to_dict() for post in new_posts])
        newest = max_post_id(new_posts)
        if newest is not None:
            storage.save_last_id(state_dir, LAST_ID_KEY, newest)

    print(f"ブックマーク取得: {len(posts)}件 / 新規保存: {written}件 (前回最新ID: {last_id or 'なし'})")
    if written:
        print(f"保存先: {out_path}")
    if error_message:
        print(f"エラー: {error_message}", file=sys.stderr)
        print("検知時点までの収集分は保存済みです。", file=sys.stderr)
    return exit_code


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = build_arg_parser().parse_args(argv)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
