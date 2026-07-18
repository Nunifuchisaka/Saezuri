"""saezuri トレンド把握

x.com/explore/tabs/for-you を開き、トレンド系オペレーション(ExplorePage等、
capture.TREND_OPERATION_KEYWORDSで緩めに判定)のGraphQLレスポンスを傍受して
TrendEntryへ正規化する。既定ではconfig.yamlのinterest_keywordsを含むトレンド
だけに絞る(--no-filterで全件)。結果は data/raw/trends_<日付>.jsonl に追記する。

使用例:
    python scripts/trends.py               # interest_keywordsで絞り込み(既定)
    python scripts/trends.py --no-filter   # 全件

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
    TREND_OPERATION_KEYWORDS,
    ChallengeDetectedError,
    GraphQLCapture,
    RateLimitedError,
    get_max_items,
    get_wait_seconds_range,
    now_iso,
    parse_trends_payload,
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

logger = logging.getLogger(__name__)

EXPLORE_URL = "https://x.com/explore/tabs/for-you"

EXIT_ERROR = 1
EXIT_SESSION_EXPIRED = 2
EXIT_RATE_LIMITED = 3
EXIT_CHALLENGE = 4


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="トレンドの収集(ExplorePage系傍受)")
    parser.add_argument("--filter", action=argparse.BooleanOptionalAction, default=True,
                        help="config.yamlのinterest_keywordsを含むトレンドだけに絞る(既定: ON)")
    parser.add_argument("--max", type=int, default=None,
                        help="取得上限件数(省略時はconfig.yamlのmax_items)")
    parser.add_argument("--config", default="config.yaml", help="設定ファイルのパス(既定: config.yaml)")
    parser.add_argument("--browser", choices=SUPPORTED_BROWSERS, default=None,
                        help="起動するブラウザエンジン(省略時はconfig.yamlのbrowser)")
    parser.add_argument("--headed", action="store_true", help="ブラウザを可視状態で起動する(デバッグ用)")
    return parser


def filter_trends(entries: list, keywords: list[str]) -> list:
    """trend_wordにkeywordsのいずれかを含む(大文字小文字無視)エントリだけを残す。"""
    lowered = [str(k).lower() for k in keywords if str(k).strip()]
    if not lowered:
        return list(entries)
    return [e for e in entries if any(k in e.trend_word.lower() for k in lowered)]


def dedupe_by_word(entries: list) -> list:
    """trend_word重複を除いたリストを返す(中断時の途中経過保存用)。"""
    seen: set[str] = set()
    unique = []
    for entry in entries:
        if entry.trend_word in seen:
            continue
        seen.add(entry.trend_word)
        unique.append(entry)
    return unique


def run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    raw_dir = Path(config["output"]["raw_dir"])
    max_items = args.max if args.max is not None else get_max_items(config)
    wait_range = get_wait_seconds_range(config)
    browser_name = args.browser or get_browser_name(config)
    keywords = list(config.get("interest_keywords") or [])
    collected_at = now_iso()

    # scroll_and_collect内で例外中断してもparse済み分を保存できるよう、外側にも溜める
    buffer: list = []

    def parse(payload: dict, operation: str) -> list:
        entries = parse_trends_payload(payload, operation, collected_at=collected_at)
        buffer.extend(entries)
        return entries

    entries: list = []
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
                capture = GraphQLCapture(
                    operations=(), operation_keywords=TREND_OPERATION_KEYWORDS
                )
                capture.attach(page)
                page.goto(EXPLORE_URL, wait_until="domcontentloaded")
                entries = scroll_and_collect(
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
        entries = dedupe_by_word(buffer)
        exit_code = EXIT_RATE_LIMITED
        error_message = str(exc)
    except ChallengeDetectedError as exc:
        entries = dedupe_by_word(buffer)
        exit_code = EXIT_CHALLENGE
        error_message = str(exc)

    selected = entries
    if args.filter:
        if keywords:
            selected = filter_trends(entries, keywords)
        else:
            print("注意: interest_keywordsが空のため絞り込みせず全件保存します。")

    written = 0
    out_path = raw_dir / f"trends_{datetime.now().strftime('%Y%m%d')}.jsonl"
    if selected:
        written = storage.append_jsonl(out_path, [entry.to_dict() for entry in selected])

    filter_note = "ON" if (args.filter and keywords) else "OFF"
    print(f"トレンド取得: {len(entries)}件 / 絞り込み({filter_note})後: {len(selected)}件 / 保存: {written}件")
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
