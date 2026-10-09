"""saezuri Instagram 起点投稿のコメント収集・いいね数上位抽出

起点となるInstagram投稿(URL指定)のページを開き、コメント(初回表示分はページ埋め込みJSON、
追加分はコメント欄スクロール時のPolarisPostCommentsPaginationQuery)を収集して
実行ごとに data/raw/ig_related_<日付>_<連番3桁>.jsonl へ全件保存する。
あわせて、起点投稿への直接のコメント(返信への返信・起点投稿者本人のコメントは除く)を
いいね数の降順で上位--top件 digest_dir/top_ig_<shortcode>_<日付>.md に書き出す。

Web版には並び替えUIが無いため、既定の並び(人気順)のまま集めて手元でいいね数順に並べ直す。
返信(子コメント)を展開する追加リクエストはしない。

事前に ig_login.py で手動ログインしておくこと。

使用例:
    python scripts/ig_related.py --url https://www.instagram.com/p/XXXXXXXXXXX/
    python scripts/ig_related.py --url https://www.instagram.com/p/XXXXXXXXXXX/ --max 50 --top 20

終了コード: 0=正常 / 1=設定・環境エラー / 2=セッション失効 /
3=レートリミット(429)検知 / 4=チャレンジ画面検知
(2(収集途中の失効)・3・4の場合も検知時点までの収集分は保存してから終了する)
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

import ranking
import storage
from capture import (
    ChallengeDetectedError,
    RateLimitedError,
    get_max_items,
    get_wait_seconds_range,
    now_iso,
    scroll_and_collect,
)
from instagram import (
    InstagramCapture,
    load_embedded_json,
    new_instagram_context,
    parse_instagram_comments,
    parse_instagram_payload,
    parse_instagram_post_url,
    require_instagram_login,
    shortcode_to_pk,
)
from related import next_related_out_path
from schema import Post
from session import (
    SUPPORTED_BROWSERS,
    SessionExpiredError,
    get_browser_executable_path,
    get_browser_name,
    launch_browser,
    load_config,
)
from user_tweets import dedupe_by_id

logger = logging.getLogger(__name__)

EXIT_ERROR = 1
EXIT_SESSION_EXPIRED = 2
EXIT_RATE_LIMITED = 3
EXIT_CHALLENGE = 4

# ページ表示後、埋め込みJSONを読む前に待つ時間(描画・ハイドレーション待ち)
EMBEDDED_WAIT_MS = 3000
# 投稿ページの埋め込みJSONのうち、投稿本体(shortcode__web_info)とコメント一覧を含むものの目印
EMBEDDED_JSON_MARKER = "xdt_api__v1__media__"

# コメント欄はウィンドウではなく内部のスクロールコンテナで追加読み込みされるため、
# スクロール量が最大の要素を探して末尾まで送る(見つからなければウィンドウをスクロール)
COMMENT_SCROLL_SCRIPT = """
() => {
  let best = null, bestExtra = 0;
  for (const el of document.querySelectorAll('div, ul, section')) {
    const style = getComputedStyle(el);
    if (!['auto', 'scroll'].includes(style.overflowY)) continue;
    const extra = el.scrollHeight - el.clientHeight;
    if (extra < 50 || el.clientHeight < 150) continue;
    if (extra > bestExtra) { best = el; bestExtra = extra; }
  }
  if (!best) { window.scrollBy(0, window.innerHeight * 2); return 'window'; }
  best.scrollTop = best.scrollHeight;
  return 'scroller';
}
"""


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Instagram投稿1件のコメントを収集し、いいね数上位を抽出する"
    )
    parser.add_argument("--url", required=True, help="起点投稿のURL(https://www.instagram.com/p/<shortcode>/)")
    parser.add_argument("--max", type=int, default=None,
                        help="コメントの取得上限件数(省略時はconfig.yamlのmax_items)")
    parser.add_argument("--top", type=int, default=ranking.DEFAULT_TOP,
                        help=f"Markdownに出す上位件数(いいね数順。既定: {ranking.DEFAULT_TOP})")
    parser.add_argument("--config", default="config.yaml", help="設定ファイルのパス(既定: config.yaml)")
    parser.add_argument("--browser", choices=SUPPORTED_BROWSERS, default=None,
                        help="起動するブラウザエンジン(省略時はconfig.yamlのbrowser)")
    parser.add_argument("--headed", action="store_true", help="ブラウザを可視状態で起動する(デバッグ用)")
    return parser


def build_source_query(code: str) -> str:
    return f"ig-related:{code}:comment"


def find_origin_post(payloads: list[dict], code: str) -> Post | None:
    """埋め込みJSONから起点投稿(shortcodeが一致する投稿ノード)を探す。無ければNone。"""
    for payload in payloads:
        for post in parse_instagram_payload(payload, "embedded"):
            if post.url.rstrip("/").endswith(f"/p/{code}"):
                return post
    return None


def collect_comments(
    context,
    code: str,
    max_items: int,
    wait_range: tuple[float, float],
) -> tuple[Post | None, str, list[Post], Exception | None]:
    """投稿ページのコメントを収集する。戻り値は(起点投稿, 起点media pk, コメント, ブロック検知例外)。"""
    collected_at = now_iso()
    source_query = build_source_query(code)
    buffer: list[Post] = []
    origin: Post | None = None
    media_pk = shortcode_to_pk(code)

    page = context.new_page()
    capture = InstagramCapture()
    capture.attach(page)
    try:
        page.goto(f"https://www.instagram.com/p/{code}/", wait_until="domcontentloaded")
        page.wait_for_timeout(EMBEDDED_WAIT_MS)
        capture.raise_if_blocked(page)
        payloads = load_embedded_json(page, EMBEDDED_JSON_MARKER)
        origin = find_origin_post(payloads, code)
        if origin is None:
            logger.warning("起点投稿の情報がページから取れなかったため、投稿者本人のコメント除外は行いません")
        else:
            media_pk = origin.id

        def parse(payload: dict, endpoint: str) -> list:
            comments = parse_instagram_comments(
                payload, endpoint, media_pk=media_pk, code=code,
                source_query=source_query, collected_at=collected_at,
            )
            buffer.extend(comments)
            return comments

        for payload in payloads:
            capture.inject("embedded", payload)
        logger.info("Instagram(Web)のコメントは並び替えUIが無いため、既定の並び(人気順)のまま収集します")
        comments = scroll_and_collect(
            page, capture, parse, max_items=max_items, wait_seconds_range=wait_range,
            scroll_script=COMMENT_SCROLL_SCRIPT,
        )
        return origin, media_pk, comments, None
    except (RateLimitedError, ChallengeDetectedError, SessionExpiredError) as exc:
        return origin, media_pk, dedupe_by_id(buffer)[:max_items], exc
    finally:
        try:
            capture.detach(page)
        except Exception as exc:
            logger.warning("InstagramCaptureのdetachに失敗しました: %s", exc)
        page.close()


def run(args: argparse.Namespace) -> int:
    try:
        code = parse_instagram_post_url(args.url)
    except ValueError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return EXIT_ERROR
    if args.top < 1:
        print("エラー: --topは1以上で指定してください", file=sys.stderr)
        return EXIT_ERROR

    config = load_config(args.config)
    raw_dir = Path(config["output"]["raw_dir"])
    digest_dir = config["output"].get("digest_dir") or "digest"
    max_items = args.max if args.max is not None else get_max_items(config)
    wait_range = get_wait_seconds_range(config)
    browser_name = args.browser or get_browser_name(config)

    try:
        with sync_playwright() as playwright:
            browser = launch_browser(
                playwright, browser_name, headless=not args.headed,
                executable_path=get_browser_executable_path(config),
            )
            try:
                context = new_instagram_context(browser, config)
                require_instagram_login(context)
                origin, media_pk, comments, blocked = collect_comments(context, code, max_items, wait_range)
            finally:
                browser.close()
    except FileNotFoundError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except SessionExpiredError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return EXIT_SESSION_EXPIRED

    out_path = next_related_out_path(raw_dir, prefix="ig_related")
    written = storage.append_jsonl(out_path, [c.to_dict() for c in comments]) if comments else 0

    origin_handle = origin.author_handle if origin else None
    sections = ranking.build_sections(
        comments, [("コメント", lambda p: p.in_reply_to_id == media_pk)], media_pk, origin_handle, args.top
    )
    notes = ["ブロック検知で収集を途中終了しています(収集済み分のみで集計)"] if blocked else None
    text = ranking.render_top_markdown(
        title=f"Instagram 人気コメント 上位{args.top}件(いいね数順)",
        origin_url=f"https://www.instagram.com/p/{code}/",
        origin_handle=origin_handle,
        generated_at=now_iso(),
        sections=sections,
        notes=notes,
    )
    md_path = ranking.write_top_markdown(ranking.top_markdown_path(digest_dir, "ig", code), text)

    print(f"起点投稿: {code}" + (f" (@{origin_handle})" if origin_handle else ""))
    print(f"取得: {len(comments)}件 / 保存: {written}件")
    if written:
        print(f"保存先: {out_path}")
    for label, ranked, total in sections:
        print(f"{label}: 対象{total}件 / 上位{len(ranked)}件")
    print(f"上位Markdown: {md_path}")

    if blocked is not None:
        print(f"エラー: {blocked}", file=sys.stderr)
        print("検知時点までの収集分は保存済みです。", file=sys.stderr)
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
