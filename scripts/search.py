"""saezuri キーワード/ハッシュタグ検索収集

x.com/search?q=<クエリ>&f=live|top を開き、SearchTimelineオペレーションの
GraphQLレスポンスを傍受して収集する。結果は実行ごとに
data/raw/search_<日付>_<連番3桁>.jsonl という新規ファイルに保存し、
既読IDは state/seen_ids.json でグローバル管理する。

使用例:
    python scripts/search.py --query "VRChat アップデート" --mode live --max 100
    python scripts/search.py --query "#VRChat" --since-days 7
    python scripts/search.py --from someone --since 2026-01-01 --split-days 30 --max 100

終了コード: 0=正常 / 1=設定・環境エラー / 2=セッション失効 /
3=レートリミット(429)検知 / 4=チャレンジ画面検知
(3・4の場合も検知時点までの収集分は保存してから終了する)
"""
from __future__ import annotations

import argparse
import logging
import random
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


def parse_date_arg(value: str) -> date:
    """YYYY-MM-DD形式の日付文字列をdateに変換する(argparseのtype用)。"""
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise argparse.ArgumentTypeError(f"日付はYYYY-MM-DD形式で指定してください: {value}")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="キーワード/ハッシュタグ検索の収集(SearchTimeline傍受)")
    parser.add_argument("--query", default="", help="検索クエリ(ハッシュタグ・検索演算子も可。--from指定時は省略可)")
    parser.add_argument("--from", dest="from_handle", default=None, metavar="HANDLE",
                        help="指定アカウントの投稿に絞る(クエリにfrom:HANDLEを付加。先頭の@は除去)")
    parser.add_argument("--since", type=parse_date_arg, default=None, metavar="YYYY-MM-DD",
                        help="この日以降に絞る(since:を付加。--since-daysより優先)")
    parser.add_argument("--until", type=parse_date_arg, default=None, metavar="YYYY-MM-DD",
                        help="この日より前に絞る(until:を付加。Xの仕様で当日は含まない)")
    parser.add_argument("--split-days", type=int, default=None, metavar="N",
                        help="--since〜--untilをN日幅の窓に分割し、窓ごとに順次検索する(--maxは窓ごとの上限。--until省略時は明日まで)")
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


def normalize_handle(handle: str) -> str:
    """ハンドルから先頭の@を除去する。"""
    return handle.strip().lstrip("@")


def resolve_date_range(
    since: date | None,
    until: date | None,
    since_days: int | None,
    today: date | None = None,
) -> tuple[date | None, date | None]:
    """--since/--until/--since-daysから実際に使う(since, until)を決める。--sinceを--since-daysより優先する。"""
    today = today or date.today()
    if since is None and since_days is not None:
        since = today - timedelta(days=since_days)
    return since, until


def build_query(
    query: str,
    handle: str | None = None,
    since: date | None = None,
    until: date | None = None,
) -> str:
    """クエリへfrom:/since:/until:演算子を付加する。"""
    parts = [query.strip()] if query.strip() else []
    if handle:
        parts.append(f"from:{normalize_handle(handle)}")
    if since is not None:
        parts.append(f"since:{since.isoformat()}")
    if until is not None:
        parts.append(f"until:{until.isoformat()}")
    return " ".join(parts)


def split_windows(since: date, until: date, days: int) -> list[tuple[date, date]]:
    """[since, until)をdays日幅に分割した(開始, 終了)のリストを返す。終了はuntil:に使う排他的な日付。"""
    if days < 1:
        raise ValueError("--split-daysは1以上で指定してください")
    windows = []
    start = since
    while start < until:
        end = min(start + timedelta(days=days), until)
        windows.append((start, end))
        start = end
    return windows


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

    since, until = resolve_date_range(args.since, args.until, args.since_days)
    if args.split_days is not None:
        if since is None:
            print("エラー: --split-daysには--since(または--since-days)が必要です", file=sys.stderr)
            return EXIT_ERROR
        if until is None:
            until = date.today() + timedelta(days=1)
        try:
            windows = split_windows(since, until, args.split_days)
        except ValueError as exc:
            print(f"エラー: {exc}", file=sys.stderr)
            return EXIT_ERROR
        if not windows:
            print("エラー: --sinceが--until以降になっています", file=sys.stderr)
            return EXIT_ERROR
        targets = [
            build_query(args.query, args.from_handle, w_since, w_until)
            for w_since, w_until in windows
        ]
    else:
        targets = [build_query(args.query, args.from_handle, since, until)]
    if not targets[0].strip():
        print("エラー: --queryまたは--fromを指定してください", file=sys.stderr)
        return EXIT_ERROR
    query = targets[0] if len(targets) == 1 else f"{args.query} (窓分割{len(targets)}件)".strip()
    collected_at = now_iso()

    # scroll_and_collect内で例外中断してもparse済み分を保存できるよう、外側にも溜める
    buffer: list = []
    current_query = targets[0]

    def parse(payload: dict, operation: str) -> list:
        posts = parse_timeline_payload(
            payload, operation, source_query=current_query, collected_at=collected_at
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
                for index, current_query in enumerate(targets):
                    if index > 0:
                        page.wait_for_timeout(random.uniform(*wait_range) * 1000)
                    capture.clear()
                    page.goto(build_search_url(current_query, args.mode), wait_until="domcontentloaded")
                    window_posts = scroll_and_collect(
                        page,
                        capture,
                        parse,
                        max_items=max_items,
                        wait_seconds_range=wait_range,
                    )
                    posts.extend(window_posts)
                    if len(targets) > 1:
                        logger.info("窓 %d/%d: %d件取得 (%s)", index + 1, len(targets), len(window_posts), current_query)
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
        exit_code = EXIT_RATE_LIMITED
        error_message = str(exc)
    except ChallengeDetectedError as exc:
        exit_code = EXIT_CHALLENGE
        error_message = str(exc)
    if exit_code:
        posts = buffer
    posts = dedupe_by_id(posts)

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
