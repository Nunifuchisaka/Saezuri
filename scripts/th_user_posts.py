"""saezuri Threads 特定アカウント投稿追跡

config.yamlのthreads.watch_accountsに列挙したユーザー名それぞれについて
threads.com/@<username> を開き、ページ埋め込みJSON(初回表示分)とスクロール時の
内部APIレスポンス(graphql/query等)から投稿を収集する。
state/threads_<username>_last_id.jsonに前回取得済み最新IDを保存し、
それより新しい投稿のみを差分保存する(ThreadsのIDはInstagramと同じく投稿時刻順に増える)。
結果は data/raw/threads_posts_<日付>.jsonl に追記する。

事前に th_login.py で手動ログインしておくこと(X用・Instagram用とはセッションが別)。

使用例:
    python scripts/th_user_posts.py                      # threads.watch_accounts全件
    python scripts/th_user_posts.py --user some_brand    # 特定の1アカウントのみ

終了コード: 0=正常 / 1=設定・環境エラー / 2=セッション失効 /
3=レートリミット(429)検知 / 4=チャレンジ画面検知
(2(収集途中の失効)・3・4の場合も検知時点までの収集分は保存し、残りのアカウントは処理しない)
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
    RateLimitedError,
    get_max_items,
    get_wait_seconds_range,
    now_iso,
    scroll_and_collect,
)
from session import (
    SUPPORTED_BROWSERS,
    SessionExpiredError,
    get_browser_executable_path,
    get_browser_name,
    launch_browser,
    load_config,
)
from threads import (
    ThreadsCapture,
    get_threads_watch_accounts,
    load_embedded_payloads,
    new_threads_context,
    parse_threads_payload,
    require_threads_login,
)
from user_tweets import dedupe_by_id, filter_new_posts, max_post_id

logger = logging.getLogger(__name__)

EXIT_ERROR = 1
EXIT_SESSION_EXPIRED = 2
EXIT_RATE_LIMITED = 3
EXIT_CHALLENGE = 4

# state/<key>_last_id.json のキー接頭辞(X側・Instagram側と衝突させないため)
STATE_KEY_PREFIX = "threads_"

# ページ表示後、埋め込みJSONを読む前に待つ時間(描画・ハイドレーション待ち)
EMBEDDED_WAIT_MS = 2000


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Threads 特定アカウント投稿の差分収集")
    parser.add_argument("--user", default=None,
                        help="特定の1アカウントだけ実行(@は不要。省略時はthreads.watch_accounts全件)")
    parser.add_argument("--max", type=int, default=None,
                        help="1アカウントあたりの取得上限件数(省略時はconfig.yamlのmax_items)")
    parser.add_argument("--config", default="config.yaml", help="設定ファイルのパス(既定: config.yaml)")
    parser.add_argument("--browser", choices=SUPPORTED_BROWSERS, default=None,
                        help="起動するブラウザエンジン(省略時はconfig.yamlのbrowser)")
    parser.add_argument("--headed", action="store_true", help="ブラウザを可視状態で起動する(デバッグ用)")
    return parser


def resolve_users(args: argparse.Namespace, config: dict) -> list[str]:
    """対象ユーザー名のリストを決める(--user指定が優先、@は除去)。"""
    if args.user:
        return [args.user.strip().lstrip("@")]
    return get_threads_watch_accounts(config)


def collect_user(
    context,
    username: str,
    max_items: int,
    wait_range: tuple[float, float],
) -> tuple[list, Exception | None]:
    """1アカウント分を収集する。ブロック・失効検知時は(途中までの収集分, 例外)を返す。"""
    collected_at = now_iso()
    source_query = f"threads:{username}"
    buffer: list = []

    def parse(payload: dict, endpoint: str) -> list:
        posts = parse_threads_payload(
            payload, endpoint, source_query=source_query,
            collected_at=collected_at, target_handle=username,
        )
        buffer.extend(posts)
        return posts

    page = context.new_page()
    capture = ThreadsCapture()
    capture.attach(page)
    try:
        page.goto(f"https://www.threads.com/@{username}", wait_until="domcontentloaded")
        page.wait_for_timeout(EMBEDDED_WAIT_MS)
        capture.raise_if_blocked(page)
        for payload in load_embedded_payloads(page):
            capture.inject("embedded", payload)
        posts = scroll_and_collect(
            page,
            capture,
            parse,
            max_items=max_items,
            wait_seconds_range=wait_range,
        )
        return posts, None
    except (RateLimitedError, ChallengeDetectedError, SessionExpiredError) as exc:
        return dedupe_by_id(buffer)[:max_items], exc
    finally:
        try:
            capture.detach(page)
        except Exception as exc:
            logger.warning("ThreadsCaptureのdetachに失敗しました: %s", exc)
        page.close()


def run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    raw_dir = Path(config["output"]["raw_dir"])
    state_dir = Path(config["output"]["state_dir"])
    max_items = args.max if args.max is not None else get_max_items(config)
    wait_range = get_wait_seconds_range(config)
    browser_name = args.browser or get_browser_name(config)

    users = resolve_users(args, config)
    if not users:
        print("エラー: 対象アカウントがありません。config.yamlのthreads.watch_accountsを設定するか"
              "--userを指定してください。", file=sys.stderr)
        return EXIT_ERROR

    out_path = raw_dir / f"threads_posts_{datetime.now().strftime('%Y%m%d')}.jsonl"
    total_written = 0
    blocked: Exception | None = None

    try:
        with sync_playwright() as playwright:
            browser = launch_browser(
                playwright, browser_name, headless=not args.headed,
                executable_path=get_browser_executable_path(config),
            )
            try:
                context = new_threads_context(browser, config)
                require_threads_login(context)
                for username in users:
                    state_key = f"{STATE_KEY_PREFIX}{username}"
                    last_id = storage.load_last_id(state_dir, state_key)
                    posts, blocked = collect_user(context, username, max_items, wait_range)
                    new_posts = filter_new_posts(posts, last_id)
                    written = 0
                    if new_posts:
                        written = storage.append_jsonl(
                            out_path, [post.to_dict() for post in new_posts]
                        )
                        total_written += written
                        newest = max_post_id(new_posts)
                        if newest is not None:
                            storage.save_last_id(state_dir, state_key, newest)
                    print(f"@{username}: 取得 {len(posts)}件 / 新規保存 {written}件"
                          f" (前回最新ID: {last_id or 'なし'})")
                    if blocked is not None:
                        break
            finally:
                browser.close()
    except FileNotFoundError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except SessionExpiredError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return EXIT_SESSION_EXPIRED

    print(f"合計 新規保存: {total_written}件")
    if total_written:
        print(f"保存先: {out_path}")
    if blocked is not None:
        print(f"エラー: {blocked}", file=sys.stderr)
        print("検知時点までの収集分は保存済みです。残りのアカウントは処理していません。", file=sys.stderr)
        if isinstance(blocked, SessionExpiredError):
            return EXIT_SESSION_EXPIRED
        return EXIT_RATE_LIMITED if isinstance(blocked, RateLimitedError) else EXIT_CHALLENGE
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = build_arg_parser().parse_args(argv)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
