"""saezuri 関連ポスト収集

起点となる1つのPost(URLまたはID指定)から、それに関連するPostを収集し、
実行ごとに data/raw/related_<日付>_<連番3桁>.jsonl という新規ファイルへ保存する。
台本・記事執筆は範囲外(既存方針どおり収集のみ)。

収集する「関連」3種(いずれも未指定なら全部ON):
    ①会話スレッド(reply) : x.com/i/status/<id> を開き TweetDetail を傍受
    ②引用ポスト(quote)   : SearchTimeline を quoted_tweet_id:<id> で回す
    ③同一投稿者(author)  : UserTweets を起点Postの投稿者ハンドルで回す

①のTweetDetailペイロードから起点Postの投稿者ハンドルを得て、③(ハンドル未知のID指定時)
の材料にする。起点Post自身(会話スレッドのfocal等)は保存対象から除外する。

使用例:
    python scripts/related.py --url https://x.com/foo/status/1234567890
    python scripts/related.py --id 1234567890 --replies --quotes
    python scripts/related.py --id 1234567890 --unseen-only

終了コード: 0=正常 / 1=設定・環境エラー / 2=セッション失効 /
3=レートリミット(429)検知 / 4=チャレンジ画面検知
(3・4の場合も検知時点までの収集分は保存してから終了する)
"""
from __future__ import annotations

import argparse
import logging
import re
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
from schema import Post
from search import build_search_url
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

# 関係種別(source_queryのサフィックスにも使う)
RELATION_REPLY = "reply"
RELATION_QUOTE = "quote"
RELATION_AUTHOR = "author"

# --replies/--quotes/--author フラグ名と関係種別の対応
_FLAG_TO_RELATION = {
    "replies": RELATION_REPLY,
    "quotes": RELATION_QUOTE,
    "author": RELATION_AUTHOR,
}

# XのポストURL(x.com/<handle>/status/<id> または i/status/<id>)からハンドル・IDを取り出す
STATUS_URL_RE = re.compile(r"(?:twitter|x)\.com/([^/?#]+)/status/(\d+)")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="起点ポスト1件から関連ポストを収集する(会話スレッド/引用/同一投稿者)"
    )
    parser.add_argument("--url", default=None, help="起点ポストのURL(--idとどちらか必須)")
    parser.add_argument("--id", default=None, help="起点ポストの数値ID(--urlとどちらか必須)")
    parser.add_argument("--replies", action="store_true", help="①会話スレッド(親+リプライ)を収集")
    parser.add_argument("--quotes", action="store_true", help="②引用ポストを収集")
    parser.add_argument("--author", action="store_true", help="③同一投稿者の他の投稿を収集")
    parser.add_argument("--unseen-only", action="store_true",
                        help="state/seen_ids.jsonで既読を除外し、保存分を書き戻す(既定は読み書きしない)")
    parser.add_argument("--max", type=int, default=None,
                        help="1種別あたりの取得上限件数(省略時はconfig.yamlのmax_items)")
    parser.add_argument("--config", default="config.yaml", help="設定ファイルのパス(既定: config.yaml)")
    parser.add_argument("--browser", choices=SUPPORTED_BROWSERS, default=None,
                        help="起動するブラウザエンジン(省略時はconfig.yamlのbrowser)")
    parser.add_argument("--headed", action="store_true", help="ブラウザを可視状態で起動する(デバッグ用)")
    return parser


# --- Playwright非依存の純関数(単体importで確認可能) -------------------------

def parse_status_url(url: str) -> tuple[str, str | None]:
    """XのポストURLから(ツイートID, 投稿者ハンドル)を取り出す。

    x.com/<handle>/status/<id> はハンドル付き、x.com/i/status/<id> は
    ハンドル不明(None)として返す。解釈できないURLはValueError。
    """
    match = STATUS_URL_RE.search(url)
    if match is None:
        raise ValueError(f"XのポストURLとして解釈できません: {url!r}")
    handle, tweet_id = match.group(1), match.group(2)
    if handle.lower() == "i":
        handle = None
    return tweet_id, handle


def resolve_start(url: str | None, tweet_id: str | None) -> tuple[str, str | None]:
    """--url / --id から起点の(ツイートID, ハンドルまたはNone)を決める。

    両方指定・両方未指定・不正なIDはいずれもValueError。
    """
    if url and tweet_id:
        raise ValueError("--url と --id は同時に指定できません。どちらか一方にしてください。")
    if not url and not tweet_id:
        raise ValueError("--url または --id のどちらかを指定してください。")
    if url:
        return parse_status_url(url)
    tid = tweet_id.strip()
    if not tid.isdigit():
        raise ValueError(f"--id は数字のツイートIDを指定してください: {tweet_id!r}")
    return tid, None


def resolve_relations(replies: bool, quotes: bool, author: bool) -> set[str]:
    """関係選択フラグから収集対象の関係種別集合を決める(いずれも未指定なら全部ON)。"""
    selected: set[str] = set()
    if replies:
        selected.add(RELATION_REPLY)
    if quotes:
        selected.add(RELATION_QUOTE)
    if author:
        selected.add(RELATION_AUTHOR)
    if not selected:
        return set(_FLAG_TO_RELATION.values())
    return selected


def build_source_query(start_id: str, relation: str) -> str:
    """source_query文字列 related:<起点id>:<関係種別> を組み立てる。"""
    return f"related:{start_id}:{relation}"


def build_status_url(start_id: str, handle: str | None = None) -> str:
    """起点ポストの詳細ページURLを組み立てる。ハンドル不明時は i/status/<id> を使う。"""
    who = handle if handle else "i"
    return f"https://x.com/{who}/status/{start_id}"


def next_related_out_path(raw_dir: Path, today: str | None = None) -> Path:
    """raw_dir内のrelated_<今日の日付>_<連番>.jsonlを走査し、次の連番のパスを返す。"""
    today = today or datetime.now().strftime("%Y%m%d")
    max_seq = 0
    for path in Path(raw_dir).glob(f"related_{today}_*.jsonl"):
        suffix = path.stem.rsplit("_", 1)[-1]
        if suffix.isdigit():
            max_seq = max(max_seq, int(suffix))
    return Path(raw_dir) / f"related_{today}_{max_seq + 1:03d}.jsonl"


def merge_posts(
    collected: dict[str, Post], posts: list[Post], exclude_id: str | None = None
) -> int:
    """postsをcollected(id→Post)へ初出優先でマージする。追加した件数を返す。

    既にcollectedに存在するidはスキップ(最初に出た種別のsource_queryを維持する)。
    exclude_id(起点Post自身のid)に一致するPostは関連ではないため保存対象から除外する。
    """
    added = 0
    for post in posts:
        if exclude_id is not None and post.id == exclude_id:
            continue
        if post.id in collected:
            continue
        collected[post.id] = post
        added += 1
    return added


def find_post(posts: list[Post], start_id: str) -> Post | None:
    """postsからidがstart_idに一致する最初のPostを返す。なければNone。"""
    for post in posts:
        if post.id == start_id:
            return post
    return None


# --- Playwright依存の収集ステップ ------------------------------------------

def collect_thread(
    context,
    goto_url: str,
    start_id: str,
    source_query: str,
    save_replies: bool,
    max_items: int,
    wait_range: tuple[float, float],
    collected: dict[str, Post],
) -> tuple[Post | None, Exception | None]:
    """①TweetDetailを傍受して会話スレッドを収集し、起点Postを特定する。

    save_repliesがTrueなら収集分(起点Post自身=focalを除く)をcollectedへマージする
    (reply種別)。Falseの場合は起点Postのハンドル取得のみ行いマージしない。
    戻り値は(起点Post または None, ブロック検知例外 または None)。
    """
    collected_at = now_iso()
    buffer: list[Post] = []

    def parse(payload: dict, op: str) -> list:
        posts = parse_timeline_payload(
            payload, op, source_query=source_query, collected_at=collected_at
        )
        buffer.extend(posts)
        return posts

    page = context.new_page()
    capture = GraphQLCapture(operations=("TweetDetail",))
    capture.attach(page)
    try:
        page.goto(goto_url, wait_until="domcontentloaded")
        posts = scroll_and_collect(
            page, capture, parse, max_items=max_items, wait_seconds_range=wait_range
        )
        if save_replies:
            merge_posts(collected, posts, exclude_id=start_id)
        return find_post(posts, start_id), None
    except (RateLimitedError, ChallengeDetectedError) as exc:
        if save_replies:
            merge_posts(collected, buffer, exclude_id=start_id)
        return find_post(buffer, start_id), exc
    finally:
        try:
            capture.detach(page)
        except Exception as exc:
            logger.warning("GraphQLCaptureのdetachに失敗しました: %s", exc)
        page.close()


def collect_timeline_relation(
    context,
    goto_url: str,
    operation: str,
    source_query: str,
    start_id: str,
    max_items: int,
    wait_range: tuple[float, float],
    collected: dict[str, Post],
) -> Exception | None:
    """②③共通: SearchTimeline/UserTweetsを傍受し収集分をcollectedへマージする。

    起点Post自身(start_id)は関連ではないため除外する。
    戻り値はブロック検知例外(RateLimited/Challenge)、正常時はNone。
    """
    collected_at = now_iso()
    buffer: list[Post] = []

    def parse(payload: dict, op: str) -> list:
        posts = parse_timeline_payload(
            payload, op, source_query=source_query, collected_at=collected_at
        )
        buffer.extend(posts)
        return posts

    page = context.new_page()
    capture = GraphQLCapture(operations=(operation,))
    capture.attach(page)
    try:
        page.goto(goto_url, wait_until="domcontentloaded")
        posts = scroll_and_collect(
            page, capture, parse, max_items=max_items, wait_seconds_range=wait_range
        )
        merge_posts(collected, posts, exclude_id=start_id)
        return None
    except (RateLimitedError, ChallengeDetectedError) as exc:
        merge_posts(collected, buffer, exclude_id=start_id)
        return exc
    finally:
        try:
            capture.detach(page)
        except Exception as exc:
            logger.warning("GraphQLCaptureのdetachに失敗しました: %s", exc)
        page.close()


def run(args: argparse.Namespace) -> int:
    try:
        start_id, start_handle = resolve_start(args.url, args.id)
    except ValueError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return EXIT_ERROR

    relations = resolve_relations(args.replies, args.quotes, args.author)

    config = load_config(args.config)
    raw_dir = Path(config["output"]["raw_dir"])
    state_dir = Path(config["output"]["state_dir"])
    max_items = args.max if args.max is not None else get_max_items(config)
    wait_range = get_wait_seconds_range(config)
    browser_name = args.browser or get_browser_name(config)

    # id→Post(初出優先)。中断時もここに溜まった分を保存する。
    collected: dict[str, Post] = {}
    blocked: Exception | None = None

    # 起点情報(ハンドル)が必要ならTweetDetailを開く。
    # ①replies選択、または③author選択かつハンドル未知(ID指定)のいずれか。
    need_detail = (
        RELATION_REPLY in relations
        or (RELATION_AUTHOR in relations and not start_handle)
    )

    try:
        with sync_playwright() as playwright:
            browser = launch_browser(
                playwright, browser_name, headless=not args.headed,
                executable_path=get_browser_executable_path(config),
            )
            try:
                context = new_context(browser, config)
                require_login(context)

                start_post: Post | None = None
                if need_detail:
                    start_post, blocked = collect_thread(
                        context,
                        build_status_url(start_id, start_handle),
                        start_id,
                        build_source_query(start_id, RELATION_REPLY),
                        save_replies=RELATION_REPLY in relations,
                        max_items=max_items,
                        wait_range=wait_range,
                        collected=collected,
                    )

                eff_handle = start_handle or (start_post.author_handle if start_post else None)

                # ②引用ポスト
                if blocked is None and RELATION_QUOTE in relations:
                    quote_url = build_search_url(f"quoted_tweet_id:{start_id}", "live")
                    blocked = collect_timeline_relation(
                        context, quote_url, "SearchTimeline",
                        build_source_query(start_id, RELATION_QUOTE),
                        start_id, max_items, wait_range, collected,
                    )

                # ③同一投稿者
                if blocked is None and RELATION_AUTHOR in relations:
                    if eff_handle:
                        blocked = collect_timeline_relation(
                            context, f"https://x.com/{eff_handle}", "UserTweets",
                            build_source_query(start_id, RELATION_AUTHOR),
                            start_id, max_items, wait_range, collected,
                        )
                    else:
                        logger.warning(
                            "起点の投稿者ハンドルが特定できなかったため、同一投稿者の収集(author)をスキップします"
                        )
            finally:
                browser.close()
    except FileNotFoundError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except SessionExpiredError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return EXIT_SESSION_EXPIRED

    records = [post.to_dict() for post in collected.values()]

    seen_ids: set[str] = set()
    if args.unseen_only:
        seen_ids = storage.load_seen_ids(state_dir)
        new_records = storage.filter_unseen(records, seen_ids)
    else:
        new_records = records

    out_path = next_related_out_path(raw_dir)
    written = 0
    if new_records:
        written = storage.append_jsonl(out_path, new_records)
        if args.unseen_only:
            storage.save_seen_ids(state_dir, seen_ids | {r["id"] for r in new_records})

    print(f"起点ポスト: {start_id}"
          + (f" (@{start_handle})" if start_handle else "")
          + f" / 関係種別: {','.join(sorted(relations))}")
    if args.unseen_only:
        print(f"取得: {len(records)}件 / 新規保存: {written}件 /"
              f" 既読スキップ: {len(records) - len(new_records)}件")
    else:
        print(f"取得: {len(records)}件 / 保存: {written}件")
    if written:
        print(f"保存先: {out_path}")

    exit_code = 0
    if blocked is not None:
        exit_code = EXIT_RATE_LIMITED if isinstance(blocked, RateLimitedError) else EXIT_CHALLENGE
        print(f"エラー: {blocked}", file=sys.stderr)
        print("検知時点までの収集分は保存済みです。残りの関係種別は処理していません。", file=sys.stderr)
    return exit_code


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = build_arg_parser().parse_args(argv)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
