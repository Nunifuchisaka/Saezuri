"""saezuri 単一ツイート取得

URLまたはIDで指定した1件のツイートを x.com/<handle>/status/<id> (ハンドル不明時は
x.com/i/status/<id>) で開き、TweetDetailオペレーションのGraphQLレスポンスを傍受して
本文を取得する。会話スレッド全体は集めず、起点ツイート1件が見つかった時点で打ち切る
(related.pyの①会話スレッド収集と違い、リプライ等の関連ポストは保存しない)。

結果は標準出力に表示するとともに、実行ごとに
data/raw/tweet_<日付>_<連番3桁>.jsonl という新規ファイルへ1件保存する。

使用例:
    python scripts/tweet.py --url https://x.com/foo/status/1234567890
    python scripts/tweet.py --id 1234567890

終了コード: 0=正常 / 1=設定・環境エラーまたは対象ツイート未発見 / 2=セッション失効 /
3=レートリミット(429)検知 / 4=チャレンジ画面検知
"""
from __future__ import annotations

import argparse
import logging
import random
import sys
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

import storage
from capture import (
    ChallengeDetectedError,
    GraphQLCapture,
    RateLimitedError,
    get_wait_seconds_range,
    now_iso,
    parse_timeline_payload,
)
from related import build_status_url, find_post, resolve_start
from schema import Post
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

EXIT_ERROR = 1
EXIT_SESSION_EXPIRED = 2
EXIT_RATE_LIMITED = 3
EXIT_CHALLENGE = 4

# TweetDetailのレスポンスは複数回に分かれて届くことがあるため、起点ツイートが
# 見つかるまで「ドレイン→少し待機」をこの回数を上限に繰り返す(スクロールはしない)
DEFAULT_MAX_ROUNDS = 5


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="URLまたはIDを指定して単一ツイート1件の本文を取得する(TweetDetail傍受)"
    )
    parser.add_argument("--url", default=None, help="対象ツイートのURL(--idとどちらか必須)")
    parser.add_argument("--id", default=None, help="対象ツイートの数値ID(--urlとどちらか必須)")
    parser.add_argument("--max-rounds", type=int, default=DEFAULT_MAX_ROUNDS,
                        help=f"起点ツイート発見までのドレイン試行回数の上限(既定: {DEFAULT_MAX_ROUNDS})")
    parser.add_argument("--config", default="config.yaml", help="設定ファイルのパス(既定: config.yaml)")
    parser.add_argument("--browser", choices=SUPPORTED_BROWSERS, default=None,
                        help="起動するブラウザエンジン(省略時はconfig.yamlのbrowser)")
    parser.add_argument("--headed", action="store_true", help="ブラウザを可視状態で起動する(デバッグ用)")
    return parser


def next_tweet_out_path(raw_dir: Path, today: str | None = None) -> Path:
    """raw_dir内のtweet_<今日の日付>_<連番>.jsonlを走査し、次の連番のパスを返す。"""
    today = today or datetime.now().strftime("%Y%m%d")
    max_seq = 0
    for path in Path(raw_dir).glob(f"tweet_{today}_*.jsonl"):
        suffix = path.stem.rsplit("_", 1)[-1]
        if suffix.isdigit():
            max_seq = max(max_seq, int(suffix))
    return Path(raw_dir) / f"tweet_{today}_{max_seq + 1:03d}.jsonl"


def fetch_tweet(
    context,
    goto_url: str,
    tweet_id: str,
    wait_range: tuple[float, float],
    max_rounds: int,
) -> tuple[Post | None, Exception | None]:
    """TweetDetailを傍受し、tweet_idに一致するPostが見つかるまでドレインを繰り返す。

    スクロールはせず(会話スレッド全体の収集は範囲外)、起点ツイートが見つかった
    時点で即座に打ち切る。戻り値は(見つかったPost または None, ブロック検知例外 または None)。
    """
    collected_at = now_iso()
    buffer: list[Post] = []

    def parse(payload: dict, op: str) -> list:
        posts = parse_timeline_payload(
            payload, op, source_query=f"tweet:{tweet_id}", collected_at=collected_at
        )
        buffer.extend(posts)
        return posts

    page = context.new_page()
    capture = GraphQLCapture(operations=("TweetDetail",))
    capture.attach(page)
    try:
        page.goto(goto_url, wait_until="domcontentloaded")
        for _ in range(max_rounds):
            capture.raise_if_blocked(page)
            for operation, payload in capture.drain():
                parse(payload, operation)
            capture.raise_if_blocked(page)
            found = find_post(buffer, tweet_id)
            if found is not None:
                return found, None
            page.wait_for_timeout(random.uniform(*wait_range) * 1000)
        return find_post(buffer, tweet_id), None
    except (RateLimitedError, ChallengeDetectedError) as exc:
        return find_post(buffer, tweet_id), exc
    finally:
        try:
            capture.detach(page)
        except Exception as exc:
            logger.warning("GraphQLCaptureのdetachに失敗しました: %s", exc)
        page.close()


def run(args: argparse.Namespace) -> int:
    try:
        tweet_id, handle = resolve_start(args.url, args.id)
    except ValueError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return EXIT_ERROR

    config = load_config(args.config)
    raw_dir = Path(config["output"]["raw_dir"])
    wait_range = get_wait_seconds_range(config)
    browser_name = args.browser or get_browser_name(config)

    post: Post | None = None
    blocked: Exception | None = None

    try:
        with sync_playwright() as playwright:
            browser = launch_browser(
                playwright, browser_name, headless=not args.headed,
                executable_path=get_browser_executable_path(config),
            )
            try:
                context = new_context(browser, config)
                require_login(context)
                post, blocked = fetch_tweet(
                    context,
                    build_status_url(tweet_id, handle),
                    tweet_id,
                    wait_range,
                    args.max_rounds,
                )
            finally:
                browser.close()
    except FileNotFoundError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except SessionExpiredError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return EXIT_SESSION_EXPIRED

    out_path = None
    if post is not None:
        out_path = next_tweet_out_path(raw_dir)
        storage.append_jsonl(out_path, [post.to_dict()])
        print(f"@{post.author_handle} ({post.author_name}) {post.created_at}")
        print(post.text)
        print(f"いいね: {post.like_count} / RT: {post.repost_count} /"
              f" 返信: {post.reply_count} / 引用: {post.quote_count}")
        print(f"URL: {post.url}")
        print(f"保存先: {out_path}")
    else:
        print(f"エラー: ツイートが見つかりませんでした(id={tweet_id})。"
              "削除・非公開・鍵アカウントの可能性があります。", file=sys.stderr)

    if blocked is not None:
        exit_code = EXIT_RATE_LIMITED if isinstance(blocked, RateLimitedError) else EXIT_CHALLENGE
        print(f"エラー: {blocked}", file=sys.stderr)
        return exit_code

    return 0 if post is not None else EXIT_ERROR


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = build_arg_parser().parse_args(argv)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
