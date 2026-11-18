"""saezuri TikTok 起点動画のコメント収集・いいね数上位抽出

起点となるTikTok動画(URL指定)のページでコメント欄をスクロールし、内部API
(/api/comment/list/)のレスポンスからコメントを収集して、実行ごとに
data/raw/tt_related_<日付>_<連番3桁>.jsonl へ全件保存する。
あわせて、動画への直接のコメント(返信・動画投稿者本人のコメントは除く)を
いいね数の降順で上位--top件 digest_dir/top_tt_<動画ID>_<日付>.md に書き出す。

TikTokは自動操作で開いたタブから動画ページへ遷移するとHTTPエラー(403)で弾かれるため、
CDP接続(--cdp)時は新規タブを開かず「管理人のChromeで手で開いた、その動画のタブ」を使う。
タブが無ければ手で開くよう表示して--wait-tab秒まで待つ(初回表示分のコメントも拾えるよう、
通信はタブ単位ではなくcontext単位で傍受する)。既存タブは読み込み直さず、閉じもしない。
コメントの返信を展開する追加操作・APIの直接呼び出し・リクエスト書き換えはしない
(画面のスクロールと通信の傍受のみ)。

使用例:
    python scripts/tt_related.py --url https://www.tiktok.com/@some_brand/video/1234567890 --cdp http://127.0.0.1:9222
    python scripts/tt_related.py --url ... --max 300 --top 20

終了コード: 0=正常 / 1=設定・環境エラー(動画ページが403で開けない場合を含む) / 2=セッション失効 /
3=レートリミット(429)検知 / 4=チャレンジ(CAPTCHA)検知
(2(収集途中の失効)・3・4の場合も検知時点までの収集分は保存してから終了する)
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
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
from related import next_related_out_path
from schema import Post
from session import (
    SUPPORTED_BROWSERS,
    CdpConnectionError,
    SessionExpiredError,
    get_browser_name,
    load_config,
)
from tiktok import (
    TikTokCapture,
    get_tiktok_cdp_url,
    open_tiktok_context,
    parse_tiktok_comments,
    parse_tiktok_video_url,
)
from user_tweets import dedupe_by_id

logger = logging.getLogger(__name__)

EXIT_ERROR = 1
EXIT_SESSION_EXPIRED = 2
EXIT_RATE_LIMITED = 3
EXIT_CHALLENGE = 4

# ページ表示後・コメント欄を開いた後に待つ時間(描画待ち)
INITIAL_WAIT_MS = 3000
COMMENT_OPEN_WAIT_MS = 2500
# CDP接続時、管理人が動画を手で開くのを待つ上限(秒)
DEFAULT_WAIT_TAB_SECONDS = 180

# コメント1件(トップレベル)の要素。表示済みか(コメント欄が開いているか)の判定に使う
COMMENT_ITEM_SELECTOR = '[data-e2e="comment-level-1"]'
# コメント欄を開くボタン(動画ページの右側にコメントが出ていない表示形式のとき)
COMMENT_OPEN_SELECTORS = (
    '[data-e2e="comment-icon"]',
    'button[aria-label*="コメント"]',
    'button[aria-label*="comment" i]',
)

# コメント欄は内部のスクロールコンテナで追加読み込みされるため、最後のコメント要素の
# スクロール可能な祖先を末尾まで送る。見つからなければスクロール量が最大の要素、それも無ければウィンドウ
COMMENT_SCROLL_SCRIPT = """
() => {
  const scrollable = (el) => {
    const style = getComputedStyle(el);
    return ['auto', 'scroll'].includes(style.overflowY) && el.scrollHeight - el.clientHeight > 50;
  };
  const items = document.querySelectorAll('[data-e2e="comment-level-1"]');
  let el = items.length ? items[items.length - 1].parentElement : null;
  while (el && el !== document.body) {
    if (scrollable(el)) { el.scrollTop = el.scrollHeight; return 'comment-container'; }
    el = el.parentElement;
  }
  let best = null, bestExtra = 0;
  for (const c of document.querySelectorAll('div, ul, section')) {
    if (!scrollable(c) || c.clientHeight < 150) continue;
    const extra = c.scrollHeight - c.clientHeight;
    if (extra > bestExtra) { best = c; bestExtra = extra; }
  }
  if (best) { best.scrollTop = best.scrollHeight; return 'scroller'; }
  window.scrollBy(0, window.innerHeight * 2);
  return 'window';
}
"""


class VideoPageBlockedError(Exception):
    """動画ページが403等で開けなかった(自動操作のタブが弾かれた)。"""


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="TikTok動画1件のコメントを収集し、いいね数上位を抽出する"
    )
    parser.add_argument("--url", required=True,
                        help="起点動画のURL(https://www.tiktok.com/@<ユーザー名>/video/<動画ID>)")
    parser.add_argument("--max", type=int, default=None,
                        help="コメントの取得上限件数(省略時はconfig.yamlのmax_items)")
    parser.add_argument("--top", type=int, default=ranking.DEFAULT_TOP,
                        help=f"Markdownに出す上位件数(いいね数順。既定: {ranking.DEFAULT_TOP})")
    parser.add_argument("--config", default="config.yaml", help="設定ファイルのパス(既定: config.yaml)")
    parser.add_argument("--browser", choices=SUPPORTED_BROWSERS, default=None,
                        help="起動するブラウザエンジン(省略時はconfig.yamlのbrowser)")
    parser.add_argument("--cdp", default=None, metavar="URL",
                        help="管理人が起動・ログイン済みのChromeにCDPで接続する(例: http://127.0.0.1:9222。"
                             "省略時はconfig.yamlのtiktok.cdp_url、それも無ければstorage_state方式)")
    parser.add_argument("--wait-tab", type=float, default=DEFAULT_WAIT_TAB_SECONDS, metavar="SEC",
                        help="CDP接続時、動画のタブが無ければ手で開くのを待つ秒数"
                             f"(既定: {DEFAULT_WAIT_TAB_SECONDS})")
    parser.add_argument("--headed", action="store_true", help="ブラウザを可視状態で起動する(デバッグ用)")
    return parser


def build_source_query(video_id: str) -> str:
    return f"tt-related:{video_id}:comment"


def find_video_tab(pages, video_id: str):
    """開いているタブのうち、URLがその動画のページであるものを返す(無ければNone)。"""
    for page in pages:
        try:
            url = page.url
        except Exception:
            continue
        if f"/video/{video_id}" in url or f"/photo/{video_id}" in url:
            return page
    return None


def open_comments(page) -> bool:
    """コメント欄が表示されていなければ開く。開けた(または既に開いていた)らTrue。"""
    if page.query_selector(COMMENT_ITEM_SELECTOR) is not None:
        return True
    for selector in COMMENT_OPEN_SELECTORS:
        button = page.query_selector(selector)
        if button is None:
            continue
        try:
            button.click()
        except Exception as exc:
            logger.warning("コメント欄のボタン(%s)を押せませんでした: %s", selector, exc)
            continue
        page.wait_for_timeout(COMMENT_OPEN_WAIT_MS)
        if page.query_selector(COMMENT_ITEM_SELECTOR) is not None:
            return True
    return False


def _pump_events(context, ms: int) -> None:
    """Playwrightにイベントを処理させながらms待つ。

    time.sleepで待つと、CDP接続中に開かれた新しいタブが接続側の準備待ちで止まったまま
    読み込みが進まない(sync APIはwait系の呼び出し中にしかイベントを処理しないため)。
    """
    pages = list(context.pages)
    if pages:
        pages[0].wait_for_timeout(ms)
        return
    try:
        context.wait_for_event("page", timeout=ms)
    except PlaywrightTimeoutError:
        pass


def wait_for_video_tab(context, video_id: str, timeout_s: float, poll_s: float = 2.0):
    """管理人が接続先のChromeで動画を手で開くのを待ち、そのタブを返す(時間切れはNone)。"""
    deadline = time.monotonic() + timeout_s
    while True:
        page = find_video_tab(context.pages, video_id)
        if page is not None or time.monotonic() >= deadline:
            return page
        _pump_events(context, int(poll_s * 1000))


def open_video_page(context, url: str, video_id: str, use_existing_tab: bool, wait_tab_s: float):
    """収集に使う動画ページを用意する。戻り値は(page, 自分で開いたタブか)。

    use_existing_tab(CDP接続時): 自動操作で開いたタブは弾かれるため新規タブは開かず、
      既に開いている動画のタブを使う。無ければ管理人が手で開くのをwait_tab_s秒まで待つ。
      既存タブは読み込み直さない(自動操作の遷移として弾かれうるため)。
    それ以外(storage_state方式): 新規タブで開き、HTTPエラーならVideoPageBlockedError。
    """
    if use_existing_tab:
        page = find_video_tab(context.pages, video_id)
        if page is None:
            print("接続先のChromeで次の動画を手で開いてください(開いたタブはそのままにしてください):\n"
                  f"  {url}\n{int(wait_tab_s)}秒まで待ちます。", flush=True)
            page = wait_for_video_tab(context, video_id, wait_tab_s)
        if page is None:
            raise VideoPageBlockedError(
                "動画のタブが見つかりませんでした。接続先のChromeで動画を開いてから、もう一度実行してください。"
            )
        logger.info("Chromeで開いている動画のタブを使います(このタブは閉じません): %s", page.url)
        return page, False
    page = context.new_page()
    try:
        response = page.goto(url, wait_until="domcontentloaded")
    except PlaywrightError as exc:
        page.close()
        raise VideoPageBlockedError(f"動画ページを開けませんでした({exc})。") from exc
    if response is not None and response.status >= 400:
        page.close()
        raise VideoPageBlockedError(
            f"動画ページがHTTP {response.status}で開けませんでした(自動操作で開いたタブが弾かれています)。"
            "--cdpで管理人のChromeに接続し、その動画を手で開いて実行してください。"
        )
    return page, True


def collect_comments(
    context,
    url: str,
    handle: str,
    video_id: str,
    max_items: int,
    wait_range: tuple[float, float],
    use_existing_tab: bool,
    wait_tab_s: float = DEFAULT_WAIT_TAB_SECONDS,
) -> tuple[list[Post], Exception | None]:
    """動画ページのコメントを収集する。戻り値は(コメント, ブロック検知例外)。

    手で開くタブの初回読み込み分も拾えるよう、レスポンスはcontext単位で傍受する。
    """
    collected_at = now_iso()
    source_query = build_source_query(video_id)
    buffer: list[Post] = []

    def parse(payload: dict, endpoint: str) -> list:
        comments = parse_tiktok_comments(
            payload, endpoint, video_id=video_id, video_handle=handle,
            source_query=source_query, collected_at=collected_at,
        )
        buffer.extend(comments)
        return comments

    capture = TikTokCapture()
    capture.attach(context)
    page = None
    owned = False
    try:
        page, owned = open_video_page(context, url, video_id, use_existing_tab, wait_tab_s)
        page.wait_for_timeout(INITIAL_WAIT_MS)
        capture.raise_if_blocked(page)
        if not open_comments(page):
            logger.warning("コメント欄が見つかりませんでした。スクロールだけ試します")
        logger.info("TikTok(Web)のコメントは既定の並びのまま収集し、手元でいいね数順に並べ直します")
        comments = scroll_and_collect(
            page, capture, parse, max_items=max_items, wait_seconds_range=wait_range,
            scroll_script=COMMENT_SCROLL_SCRIPT,
        )
        return comments, None
    except (RateLimitedError, ChallengeDetectedError, SessionExpiredError) as exc:
        return dedupe_by_id(buffer)[:max_items], exc
    finally:
        try:
            capture.detach(context)
        except Exception as exc:
            logger.warning("TikTokCaptureのdetachに失敗しました: %s", exc)
        if owned and page is not None:
            page.close()


def run(args: argparse.Namespace) -> int:
    try:
        handle, video_id = parse_tiktok_video_url(args.url)
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
    cdp_url = get_tiktok_cdp_url(args.cdp, config)
    url = f"https://www.tiktok.com/@{handle}/video/{video_id}"

    try:
        with sync_playwright() as playwright:
            with open_tiktok_context(
                playwright, config, cdp_url, browser_name, args.headed
            ) as context:
                comments, blocked = collect_comments(
                    context, url, handle, video_id, max_items, wait_range,
                    use_existing_tab=bool(cdp_url), wait_tab_s=args.wait_tab,
                )
    except (FileNotFoundError, CdpConnectionError, VideoPageBlockedError) as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except SessionExpiredError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return EXIT_SESSION_EXPIRED

    out_path = next_related_out_path(raw_dir, prefix="tt_related")
    written = storage.append_jsonl(out_path, [c.to_dict() for c in comments]) if comments else 0

    sections = ranking.build_sections(
        comments, [("コメント", lambda p: p.in_reply_to_id == video_id)], video_id, handle, args.top
    )
    notes = ["ブロック検知で収集を途中終了しています(収集済み分のみで集計)"] if blocked else None
    text = ranking.render_top_markdown(
        title=f"TikTok 人気コメント 上位{args.top}件(いいね数順)",
        origin_url=url,
        origin_handle=handle,
        generated_at=now_iso(),
        sections=sections,
        notes=notes,
    )
    md_path = ranking.write_top_markdown(ranking.top_markdown_path(digest_dir, "tt", video_id), text)

    print(f"起点動画: {video_id} (@{handle})")
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
