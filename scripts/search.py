"""saezuri キーワード/ハッシュタグ検索収集

x.com/search?q=<クエリ>&f=live|top を開き、SearchTimelineオペレーションの
GraphQLレスポンスを傍受して収集する。結果は実行ごとに
data/raw/search_<日付>_<連番3桁>.jsonl という新規ファイルに保存し、
既読IDは state/seen_ids.json でグローバル管理する。

使用例:
    python scripts/search.py --query "VRChat アップデート" --mode live --max 100
    python scripts/search.py --query "#VRChat" --since-days 7

終了コード: 0=正常 / 1=設定・環境エラー / 2=セッション失効 /
3=レートリミット(429)検知 / 4=チャレンジ画面検知
(3・4の場合も検知時点までの収集分は保存してから終了する)
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

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

logger = logging.getLogger(__name__)

SEARCH_URL = "https://x.com/search"

EXIT_ERROR = 1
EXIT_SESSION_EXPIRED = 2
EXIT_RATE_LIMITED = 3
EXIT_CHALLENGE = 4


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="キーワード/ハッシュタグ検索の収集(SearchTimeline傍受)")
    parser.add_argument("--query", required=True, help="検索クエリ(ハッシュタグ・検索演算子も可)")
    parser.add_argument("--mode", choices=("live", "top"), default="live",
                        help="検索タブ(live=最新, top=話題)。既定: live")
    parser.add_argument("--since-days", type=int, default=None, metavar="N",
                        help="N日前以降に絞る(クエリにsince:YYYY-MM-DDを付加)")
    parser.add_argument("--max", type=int, default=None,
                        help="取得上限件数(省略時はconfig.yamlのmax_items)")
    parser.add_argument("--config", default="config.yaml", help="設定ファイルのパス(既定: config.yaml)")
    parser.add_argument("--browser", choices=SUPPORTED_BROWSERS, default=None,
                        help="起動するブラウザエンジン(省略時はconfig.yamlのbrowser)")
    parser.add_argument("--headed", action="store_true", help="ブラウザを可視状態で起動する(デバッグ用)")
    return parser


def build_search_query(query: str, since_days: int | None) -> str:
    """--since-days指定時にXの検索演算子since:YYYY-MM-DDをクエリへ付加する。"""
    if since_days is not None:
        since = date.today() - timedelta(days=since_days)
        query = f"{query} since:{since.isoformat()}"
    return query


def build_search_url(query: str, mode: str) -> str:
    """検索URLを組み立てる(クエリはURLエンコードする)。"""
    return f"{SEARCH_URL}?{urlencode({'q': query, 'f': mode})}"


def dedupe_by_id(posts: list) -> list:
    """id重複を除いたリストを返す(中断時の途中経過保存用)。"""
    seen: set[str] = set()
    unique = []
    for post in posts:
        if post.id in seen:
            continue
        seen.add(post.id)
        unique.append(post)
    return unique


def next_search_out_path(raw_dir: Path, today: str | None = None) -> Path:
    """raw_dir内のsearch_<今日の日付>_<連番>.jsonlを走査し、次の連番のパスを返す。"""
    today = today or datetime.now().strftime("%Y%m%d")
    max_seq = 0
    for path in raw_dir.glob(f"search_{today}_*.jsonl"):
        suffix = path.stem.rsplit("_", 1)[-1]
        if suffix.isdigit():
            max_seq = max(max_seq, int(suffix))
    return raw_dir / f"search_{today}_{max_seq + 1:03d}.jsonl"


def run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    raw_dir = Path(config["output"]["raw_dir"])
    state_dir = Path(config["output"]["state_dir"])
    max_items = args.max if args.max is not None else get_max_items(config)
    wait_range = get_wait_seconds_range(config)
    browser_name = args.browser or get_browser_name(config)

    query = build_search_query(args.query, args.since_days)
    url = build_search_url(query, args.mode)
    collected_at = now_iso()

    # scroll_and_collect内で例外中断してもparse済み分を保存できるよう、外側にも溜める
    buffer: list = []

    def parse(payload: dict, operation: str) -> list:
        posts = parse_timeline_payload(
            payload, operation, source_query=query, collected_at=collected_at
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
                capture = GraphQLCapture(operations=("SearchTimeline",))
                capture.attach(page)
                page.goto(url, wait_until="domcontentloaded")
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

    seen_ids = storage.load_seen_ids(state_dir)
    records = [post.to_dict() for post in posts]
    new_records = storage.filter_unseen(records, seen_ids)

    out_path = next_search_out_path(raw_dir)
    written = 0
    if new_records:
        written = storage.append_jsonl(out_path, new_records)
        storage.save_seen_ids(state_dir, seen_ids | {r["id"] for r in new_records})

    print(f"検索クエリ: {query} (mode={args.mode})")
    print(f"取得: {len(records)}件 / 新規保存: {written}件 / 既読スキップ: {len(records) - len(new_records)}件")
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
