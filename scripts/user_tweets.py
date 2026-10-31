"""saezuri 特定アカウント投稿追跡

config.yamlのwatch_accountsに列挙したハンドルそれぞれについて x.com/<handle> を
開き、UserTweets(--include-replies指定時はUserTweetsAndReplies)オペレーションの
GraphQLレスポンスを傍受して収集する。state/<handle>_last_id.jsonに前回取得済み
最新IDを保存し、それより新しい投稿のみを差分保存する。
結果は data/raw/user_tweets_<日付>.jsonl に追記する。

使用例:
    python scripts/user_tweets.py                     # watch_accounts全件
    python scripts/user_tweets.py --handle some_user  # 特定の1アカウントのみ
    python scripts/user_tweets.py --include-replies

終了コード: 0=正常 / 1=設定・環境エラー / 2=セッション失効 /
3=レートリミット(429)検知 / 4=チャレンジ画面検知
(3・4の場合も検知時点までの収集分は保存し、残りのアカウントは処理しない)
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
    CdpConnectionError,
    SessionExpiredError,
    get_browser_name,
    get_x_cdp_url,
    load_config,
    open_x_context,
)

logger = logging.getLogger(__name__)

EXIT_ERROR = 1
EXIT_SESSION_EXPIRED = 2
EXIT_RATE_LIMITED = 3
EXIT_CHALLENGE = 4


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="特定アカウント投稿の差分収集(UserTweets傍受)")
    parser.add_argument("--handle", default=None,
                        help="特定の1アカウントだけ実行(@は不要。省略時はwatch_accounts全件)")
    parser.add_argument("--include-replies", action="store_true",
                        help="返信も含めて収集する(UserTweetsAndRepliesを傍受)")
    parser.add_argument("--max", type=int, default=None,
                        help="1アカウントあたりの取得上限件数(省略時はconfig.yamlのmax_items)")
    parser.add_argument("--config", default="config.yaml", help="設定ファイルのパス(既定: config.yaml)")
    parser.add_argument("--browser", choices=SUPPORTED_BROWSERS, default=None,
                        help="起動するブラウザエンジン(省略時はconfig.yamlのbrowser)")
    parser.add_argument("--cdp", default=None, metavar="URL",
                        help="管理人が起動・ログイン済みのChromeにCDPで接続する(例: http://127.0.0.1:9222。"
                             "省略時はconfig.yamlのsession.cdp_url、それも無ければstorage_state方式)")
    parser.add_argument("--headed", action="store_true", help="ブラウザを可視状態で起動する(デバッグ用)")
    return parser


def resolve_handles(args: argparse.Namespace, config: dict) -> list[str]:
    """対象ハンドルのリストを決める(--handle指定が優先、@は除去)。"""
    if args.handle:
        handles = [args.handle]
    else:
        handles = list(config.get("watch_accounts") or [])
    return [str(h).lstrip("@") for h in handles if str(h).strip()]


def filter_new_posts(posts: list, last_id: str | None) -> list:
    """last_idよりIDが大きい(=新しい)投稿だけを残す。ID比較不能なものは残す。"""
    if last_id is None:
        return list(posts)
    try:
        threshold = int(last_id)
    except ValueError:
        return list(posts)
    result = []
    for post in posts:
        try:
            if int(post.id) > threshold:
                result.append(post)
        except ValueError:
            result.append(post)
    return result


def max_post_id(posts: list) -> str | None:
    """postsの中で数値として最大のID(=最新)を返す。数値IDがなければNone。"""
    numeric = []
    for post in posts:
        try:
            numeric.append(int(post.id))
        except ValueError:
            continue
    return str(max(numeric)) if numeric else None


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


def collect_handle(
    context,
    handle: str,
    operation: str,
    max_items: int,
    wait_range: tuple[float, float],
) -> tuple[list, Exception | None]:
    """1ハンドル分を収集する。ブロック検知時は(途中までの収集分, 例外)を返す。"""
    collected_at = now_iso()
    buffer: list = []

    def parse(payload: dict, op: str) -> list:
        posts = parse_timeline_payload(
            payload, op, source_query=handle, collected_at=collected_at
        )
        buffer.extend(posts)
        return posts

    page = context.new_page()
    capture = GraphQLCapture(operations=(operation,))
    capture.attach(page)
    try:
        page.goto(f"https://x.com/{handle}", wait_until="domcontentloaded")
        posts = scroll_and_collect(
            page,
            capture,
            parse,
            max_items=max_items,
            wait_seconds_range=wait_range,
        )
        return posts, None
    except (RateLimitedError, ChallengeDetectedError) as exc:
        return dedupe_by_id(buffer), exc
    finally:
        try:
            capture.detach(page)
        except Exception as exc:
            logger.warning("GraphQLCaptureのdetachに失敗しました: %s", exc)
        page.close()


def run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    raw_dir = Path(config["output"]["raw_dir"])
    state_dir = Path(config["output"]["state_dir"])
    max_items = args.max if args.max is not None else get_max_items(config)
    wait_range = get_wait_seconds_range(config)
    browser_name = args.browser or get_browser_name(config)
    cdp_url = get_x_cdp_url(args.cdp, config)
    operation = "UserTweetsAndReplies" if args.include_replies else "UserTweets"

    handles = resolve_handles(args, config)
    if not handles:
        print("エラー: 対象アカウントがありません。config.yamlのwatch_accountsを設定するか--handleを指定してください。",
              file=sys.stderr)
        return EXIT_ERROR

    out_path = raw_dir / f"user_tweets_{datetime.now().strftime('%Y%m%d')}.jsonl"
    total_written = 0
    blocked: Exception | None = None

    try:
        with sync_playwright() as playwright:
            with open_x_context(
                playwright, config, cdp_url, browser_name, args.headed
            ) as context:
                for handle in handles:
                    last_id = storage.load_last_id(state_dir, handle)
                    posts, blocked = collect_handle(
                        context, handle, operation, max_items, wait_range
                    )
                    new_posts = filter_new_posts(posts, last_id)
                    written = 0
                    if new_posts:
                        written = storage.append_jsonl(
                            out_path, [post.to_dict() for post in new_posts]
                        )
                        total_written += written
                        newest = max_post_id(new_posts)
                        if newest is not None:
                            storage.save_last_id(state_dir, handle, newest)
                    print(f"@{handle}: 取得 {len(posts)}件 / 新規保存 {written}件"
                          f" (前回最新ID: {last_id or 'なし'})")
                    if blocked is not None:
                        break
    except (FileNotFoundError, CdpConnectionError) as exc:
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
        return EXIT_RATE_LIMITED if isinstance(blocked, RateLimitedError) else EXIT_CHALLENGE
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = build_arg_parser().parse_args(argv)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
