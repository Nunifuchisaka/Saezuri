"""saezuri Threads 起点投稿のリプライ・引用収集・いいね数上位抽出

起点となるThreads投稿(URL指定)について、次の2種を収集して
実行ごとに data/raw/th_related_<日付>_<連番3桁>.jsonl へ全件保存する。
    リプライ(reply): 投稿ページを返信の並び「トップ」(URLの sort_order=top。効かなければ
        並び替えメニューで「トップ」を選ぶ)で開き、ページ埋め込みJSON(初回分)と
        スクロール時の BarcelonaPostPageDirectRepliesRefetchQuery から集める
    引用(quote): 投稿ページの「アクティビティを見る」→「引用」タブの一覧
        (BarcelonaFeedbackHubTabQuery)で引用投稿を特定し、一覧にはいいね数が無いため
        引用投稿を1件ずつ開いて取り直す(逐次・アクセス間にwait_secondsの範囲でランダム待機、--maxが上限)
あわせて、起点への直接のリプライ(reply_level 1で起点投稿者宛て)と引用のうち、起点投稿者本人のものを除いて
いいね数の降順で上位--top件ずつ digest_dir/top_th_<code>_<日付>.md に書き出す。
孫返信を展開する追加リクエストはしない。並びの切り替え・「アクティビティを見る」が
できなかった場合は警告して続行する(ブロック検知時の即中断とは別扱い)。

事前に th_login.py で手動ログインしておくこと。

使用例:
    python scripts/th_related.py --url https://www.threads.com/@some_user/post/XXXXXXXXXXX
    python scripts/th_related.py --url https://www.threads.com/@some_user/post/XXXXXXXXXXX --max 30 --top 20

終了コード: 0=正常 / 1=設定・環境エラー / 2=セッション失効 /
3=レートリミット(429)検知 / 4=チャレンジ画面検知
(2(収集途中の失効)・3・4の場合も検知時点までの収集分は保存し、残りは処理しない)
"""
from __future__ import annotations

import argparse
import logging
import random
import sys
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
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
from instagram import shortcode_to_pk
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
from threads import (
    ThreadsCapture,
    ThreadsQuoteRef,
    build_threads_post_url,
    find_threads_post,
    load_embedded_payloads,
    new_threads_context,
    parse_feedback_quote_items,
    parse_threads_post_url,
    parse_threads_replies,
    require_threads_login,
)
from user_tweets import dedupe_by_id

logger = logging.getLogger(__name__)

EXIT_ERROR = 1
EXIT_SESSION_EXPIRED = 2
EXIT_RATE_LIMITED = 3
EXIT_CHALLENGE = 4

RELATION_REPLY = "reply"
RELATION_QUOTE = "quote"

# ページ表示後、埋め込みJSONを読む前に待つ時間(描画・ハイドレーション待ち)
EMBEDDED_WAIT_MS = 3000
# 画面操作(メニュー・ダイアログ)後の描画待ち
UI_WAIT_MS = 3000

# 返信の並び替えボタン(テキストは現在の並び)と、人気順の項目。英語UIの文言は未確認の想定値
REPLY_SORT_BUTTON_TEXTS = ("トップ", "最近", "Top", "Recent")
REPLY_SORT_TOP_TEXTS = ("トップ", "Top")
ACTIVITY_BUTTON_TEXTS = ("アクティビティを見る", "View activity")
# 「引用」タブのテキストは件数込み(例: "引用\n12")
QUOTE_TAB_PATTERN = r"^(引用|Quotes?)(\s*[\d,.]+\s*[万千KkMm]?)?$"

FIND_REPLY_SORT_BUTTON_JS = """
(texts) => {
  for (const el of document.querySelectorAll('[role="button"][aria-haspopup="menu"]')) {
    const text = (el.innerText || '').trim().split('\\n')[0].trim();
    const rect = el.getBoundingClientRect();
    if (!texts.includes(text) || rect.width === 0 || rect.height === 0) continue;
    el.setAttribute('data-saezuri-sort', '1');
    return text;
  }
  return null;
}
"""

FIND_REPLY_SORT_ITEM_JS = """
(texts) => {
  for (const el of document.querySelectorAll('[role="menuitem"]')) {
    const text = (el.innerText || '').trim();
    if (!texts.includes(text)) continue;
    el.setAttribute('data-saezuri-sort-item', '1');
    return text;
  }
  return null;
}
"""

FIND_ACTIVITY_BUTTON_JS = """
(texts) => {
  for (const el of document.querySelectorAll('[role="button"]')) {
    const text = (el.innerText || '').trim();
    const rect = el.getBoundingClientRect();
    if (!texts.includes(text) || rect.width === 0 || rect.height === 0) continue;
    el.setAttribute('data-saezuri-activity', '1');
    return text;
  }
  return null;
}
"""

# ダイアログ内には「フォローする」等のボタンも並ぶため、文言が「引用(+件数)」のものだけを狙う
FIND_QUOTE_TAB_JS = """
(pattern) => {
  const re = new RegExp(pattern);
  const root = document.querySelector('[role="dialog"]') || document;
  for (const el of root.querySelectorAll('[role="button"], [role="tab"]')) {
    const text = (el.innerText || '').trim();
    if (!re.test(text)) continue;
    el.setAttribute('data-saezuri-quote-tab', '1');
    return text;
  }
  return null;
}
"""

# 引用一覧はダイアログ内のスクロールコンテナで追加読み込みされる。ダイアログが無ければ何もしない
DIALOG_SCROLL_SCRIPT = """
() => {
  const dialog = document.querySelector('[role="dialog"]');
  if (!dialog) return 'none';
  let best = null, bestExtra = 0;
  for (const el of dialog.querySelectorAll('div')) {
    const style = getComputedStyle(el);
    if (!['auto', 'scroll'].includes(style.overflowY)) continue;
    const extra = el.scrollHeight - el.clientHeight;
    if (extra > bestExtra) { best = el; bestExtra = extra; }
  }
  if (!best) return 'none';
  best.scrollTop = best.scrollHeight;
  return 'dialog';
}
"""


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Threads投稿1件のリプライ・引用を収集し、いいね数上位を抽出する"
    )
    parser.add_argument("--url", required=True,
                        help="起点投稿のURL(https://www.threads.com/@<user>/post/<code>)")
    parser.add_argument("--max", type=int, default=None,
                        help="種別ごとの取得上限件数(引用は開く投稿数の上限。省略時はconfig.yamlのmax_items)")
    parser.add_argument("--top", type=int, default=ranking.DEFAULT_TOP,
                        help=f"Markdownに出す種別ごとの上位件数(いいね数順。既定: {ranking.DEFAULT_TOP})")
    parser.add_argument("--config", default="config.yaml", help="設定ファイルのパス(既定: config.yaml)")
    parser.add_argument("--browser", choices=SUPPORTED_BROWSERS, default=None,
                        help="起動するブラウザエンジン(省略時はconfig.yamlのbrowser)")
    parser.add_argument("--headed", action="store_true", help="ブラウザを可視状態で起動する(デバッグ用)")
    return parser


def build_source_query(code: str, relation: str) -> str:
    return f"th-related:{code}:{relation}"


def build_top_sorted_url(post_url: str) -> str:
    """返信の並びを「トップ」に指定した投稿ページURL(画面の並び替えメニューのリンク先と同じクエリ)。"""
    return f"{post_url}?sort_order=top"


def merge_new(collected: dict[str, Post], posts: list[Post]) -> None:
    """collected(id→Post)へ初出優先で追加する。"""
    for post in posts:
        collected.setdefault(post.id, post)


# --- Playwright依存の収集ステップ ------------------------------------------

def ensure_reply_sort_top(page) -> bool:
    """返信の並びが「トップ」でなければ並び替えメニューで選び直す。できなければ警告してFalse。

    メニュー項目はリンクのため選ぶとページ遷移する(遷移後は埋め込みJSONを読み直す必要がある)。
    """
    try:
        current = None
        for _ in range(5):
            current = page.evaluate(FIND_REPLY_SORT_BUTTON_JS, list(REPLY_SORT_BUTTON_TEXTS))
            if current:
                break
            page.wait_for_timeout(1000)
        if not current:
            logger.warning("返信の並び替えメニューが見つからないため、表示されている並びのまま収集します")
            return False
        if current in REPLY_SORT_TOP_TEXTS:
            logger.info("返信の並びは「%s」です", current)
            return True
        page.locator('[data-saezuri-sort="1"]').first.click(timeout=5000)
        page.wait_for_timeout(1000)
        if not page.evaluate(FIND_REPLY_SORT_ITEM_JS, list(REPLY_SORT_TOP_TEXTS)):
            logger.warning("並び替えメニューに「トップ」が見つからないため、表示されている並びのまま収集します")
            page.keyboard.press("Escape")
            return False
        page.locator('[data-saezuri-sort-item="1"]').first.click(timeout=5000)
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(EMBEDDED_WAIT_MS)
        after = page.evaluate(FIND_REPLY_SORT_BUTTON_JS, list(REPLY_SORT_BUTTON_TEXTS))
        if after not in REPLY_SORT_TOP_TEXTS:
            logger.warning("返信の並びが「トップ」に切り替わらなかったため(表示: %s)、そのまま収集します", after)
            return False
        logger.info("返信の並びを「%s」に切り替えました", after)
        return True
    except PlaywrightError as exc:
        logger.warning("返信の並び替えに失敗したため、表示されている並びのまま収集します: %s", exc)
        return False


def open_quote_list(page) -> bool:
    """「アクティビティを見る」→「引用」タブを開く。入口が無ければ警告してFalse。

    ダイアログを開いた時点の一覧(post_activity)にも引用が含まれるため、タブが
    見つからない場合も警告だけ出してTrueを返す(取れた分だけ使う)。
    """
    try:
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(1000)
        if not page.evaluate(FIND_ACTIVITY_BUTTON_JS, list(ACTIVITY_BUTTON_TEXTS)):
            logger.warning("「アクティビティを見る」が見つからないため、引用の収集をスキップします")
            return False
        page.locator('[data-saezuri-activity="1"]').first.click(timeout=5000)
        page.wait_for_timeout(UI_WAIT_MS)
        if not page.evaluate(FIND_QUOTE_TAB_JS, QUOTE_TAB_PATTERN):
            logger.warning("アクティビティに「引用」タブが見つからないため、ダイアログ初期表示の一覧だけを使います")
            return True
        page.locator('[data-saezuri-quote-tab="1"]').first.click(timeout=5000)
        page.wait_for_timeout(UI_WAIT_MS)
        return True
    except PlaywrightError as exc:
        logger.warning("引用一覧を開けなかったため、引用の収集をスキップします: %s", exc)
        return False


def collect_replies_and_quote_refs(
    context,
    post_url: str,
    username: str,
    code: str,
    max_items: int,
    wait_range: tuple[float, float],
) -> tuple[Post | None, str, str, list[Post], list[ThreadsQuoteRef], Exception | None]:
    """投稿ページで返信と引用一覧を集める。

    戻り値は(起点投稿, 起点pk, 起点投稿者, 返信, 引用一覧の参照, ブロック検知例外)。
    """
    collected_at = now_iso()
    source_query = build_source_query(code, RELATION_REPLY)
    buffer: list[Post] = []
    origin: Post | None = None
    origin_pk = shortcode_to_pk(code)
    origin_handle = username
    replies: list[Post] = []
    refs: list[ThreadsQuoteRef] = []
    ref_buffer: list[ThreadsQuoteRef] = []

    page = context.new_page()
    capture = ThreadsCapture()
    capture.attach(page)
    try:
        page.goto(build_top_sorted_url(post_url), wait_until="domcontentloaded")
        page.wait_for_timeout(EMBEDDED_WAIT_MS)
        capture.raise_if_blocked(page)
        ensure_reply_sort_top(page)
        capture.raise_if_blocked(page)
        payloads = load_embedded_payloads(page)
        origin = find_threads_post(payloads, code=code, collected_at=collected_at)
        if origin is None:
            logger.warning("起点投稿の情報がページから取れなかったため、URLのユーザー名・codeから補います")
        else:
            origin_pk, origin_handle = origin.id, origin.author_handle

        def parse(payload: dict, endpoint: str) -> list:
            posts = parse_threads_replies(
                payload, endpoint, origin_pk=origin_pk, origin_handle=origin_handle,
                source_query=source_query, collected_at=collected_at,
            )
            buffer.extend(posts)
            return posts

        for payload in payloads:
            capture.inject("embedded", payload)
        replies = scroll_and_collect(
            page, capture, parse, max_items=max_items, wait_seconds_range=wait_range
        )

        if open_quote_list(page):
            def parse_refs(payload: dict, endpoint: str) -> list:
                found = parse_feedback_quote_items(payload, endpoint)
                ref_buffer.extend(found)
                return found

            refs = scroll_and_collect(
                page, capture, parse_refs, max_items=max_items, wait_seconds_range=wait_range,
                key_fn=lambda ref: ref.pk, scroll_script=DIALOG_SCROLL_SCRIPT,
            )
        return origin, origin_pk, origin_handle, replies, refs, None
    except (RateLimitedError, ChallengeDetectedError, SessionExpiredError) as exc:
        replies = replies or dedupe_by_id(buffer)[:max_items]
        unique_refs = list({ref.pk: ref for ref in refs or ref_buffer}.values())[:max_items]
        return origin, origin_pk, origin_handle, replies, unique_refs, exc
    finally:
        try:
            capture.detach(page)
        except Exception as exc:
            logger.warning("ThreadsCaptureのdetachに失敗しました: %s", exc)
        page.close()


def fetch_quote_posts(
    context,
    refs: list[ThreadsQuoteRef],
    code: str,
    wait_range: tuple[float, float],
) -> tuple[list[Post], Exception | None]:
    """引用投稿を1件ずつ開き、いいね数等を取り直してPostにする(逐次・各アクセス前にランダム待機)。"""
    collected_at = now_iso()
    source_query = build_source_query(code, RELATION_QUOTE)
    posts: list[Post] = []
    page = context.new_page()
    capture = ThreadsCapture()
    capture.attach(page)
    try:
        for index, ref in enumerate(refs, start=1):
            page.wait_for_timeout(random.uniform(*wait_range) * 1000)
            page.goto(ref.url, wait_until="domcontentloaded")
            page.wait_for_timeout(EMBEDDED_WAIT_MS)
            capture.raise_if_blocked(page)
            capture.clear()
            post = find_threads_post(
                load_embedded_payloads(page), code=ref.shortcode, pk=ref.pk,
                source_query=source_query, collected_at=collected_at,
            )
            if post is None:
                logger.warning("引用投稿を開きましたが投稿データが見つからないためスキップします(%d/%d)", index, len(refs))
                continue
            posts.append(post)
            logger.info("引用投稿を取得しました(%d/%d)", index, len(refs))
        return posts, None
    except (RateLimitedError, ChallengeDetectedError, SessionExpiredError) as exc:
        return posts, exc
    finally:
        try:
            capture.detach(page)
        except Exception as exc:
            logger.warning("ThreadsCaptureのdetachに失敗しました: %s", exc)
        page.close()


def run(args: argparse.Namespace) -> int:
    try:
        username, code = parse_threads_post_url(args.url)
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
    post_url = build_threads_post_url(username, code)

    quotes: list[Post] = []
    refs: list[ThreadsQuoteRef] = []
    try:
        with sync_playwright() as playwright:
            browser = launch_browser(
                playwright, browser_name, headless=not args.headed,
                executable_path=get_browser_executable_path(config),
            )
            try:
                context = new_threads_context(browser, config)
                require_threads_login(context)
                origin, origin_pk, origin_handle, replies, refs, blocked = collect_replies_and_quote_refs(
                    context, post_url, username, code, max_items, wait_range
                )
                if blocked is None and refs:
                    quotes, blocked = fetch_quote_posts(context, refs[:max_items], code, wait_range)
            finally:
                browser.close()
    except FileNotFoundError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except SessionExpiredError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return EXIT_SESSION_EXPIRED

    collected: dict[str, Post] = {}
    merge_new(collected, replies)
    merge_new(collected, quotes)
    out_path = next_related_out_path(raw_dir, prefix="th_related")
    written = storage.append_jsonl(out_path, [p.to_dict() for p in collected.values()]) if collected else 0

    quote_ids = {p.id for p in quotes}
    sections = ranking.build_sections(
        list(collected.values()),
        [
            ("リプライ", lambda p: p.in_reply_to_id == origin_pk),
            ("引用", lambda p: p.id in quote_ids),
        ],
        origin_pk, origin_handle, args.top,
    )
    notes = ["ブロック検知で収集を途中終了しています(収集済み分のみで集計)"] if blocked else None
    text = ranking.render_top_markdown(
        title=f"Threads 人気リプライ・引用 上位{args.top}件(いいね数順)",
        origin_url=post_url,
        origin_handle=origin_handle,
        generated_at=now_iso(),
        sections=sections,
        notes=notes,
    )
    md_path = ranking.write_top_markdown(ranking.top_markdown_path(digest_dir, "th", code), text)

    print(f"起点投稿: {code} (@{origin_handle})")
    print(f"取得: リプライ {len(replies)}件 / 引用一覧 {len(refs)}件 → 引用投稿 {len(quotes)}件 / 保存: {written}件")
    if written:
        print(f"保存先: {out_path}")
    for label, ranked, total in sections:
        print(f"{label}: 対象{total}件 / 上位{len(ranked)}件")
    print(f"上位Markdown: {md_path}")

    if blocked is not None:
        print(f"エラー: {blocked}", file=sys.stderr)
        print("検知時点までの収集分は保存済みです。残りは処理していません。", file=sys.stderr)
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
