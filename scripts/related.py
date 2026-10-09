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

並び(--sort):
    popular(既定): ①は会話スレッドの並び替えメニューで「いいね」を選んでから集め、
        ②は検索の f=top(話題) と f=live(最新) を各--max件まで集めてIDで合算する。
        収集後、起点への直接のリプライ・引用(起点投稿者本人のものは除く)を
        いいね数の降順で上位--top件ずつ digest_dir/top_x_<起点ID>_<日付>.md に書き出す。
        並び替えメニューが見つからない等で切り替えられなかった場合は警告して既定の並びのまま続ける。
    recent: 従来どおり(①は既定の並び、②は f=live のみ)。Markdownは書き出さない。
③(author)はどちらでも従来どおりでランキング対象外。

使用例:
    python scripts/related.py --url https://x.com/foo/status/1234567890
    python scripts/related.py --id 1234567890 --replies --quotes --top 20
    python scripts/related.py --id 1234567890 --sort recent --unseen-only

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

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

import ranking
import storage
from capture import (
    ChallengeDetectedError,
    GraphQLCapture,
    RateLimitedError,
    SCROLL_SCRIPT,
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

SORT_POPULAR = "popular"
SORT_RECENT = "recent"

# 会話スレッドの並び替えボタン(data-testid・aria-labelが無いため文言で特定する)。
# ボタンのテキストは現在の並び。英語UIの文言は未確認の想定値
REPLY_SORT_BUTTON_TEXTS = ("関連性が高い", "最新", "いいね", "Relevant", "Recent", "Likes")
REPLY_SORT_LIKES_TEXTS = ("いいね", "Likes")
# 並び替えボタンの描画待ち(1秒×回数)と、「いいね」選択後の取り直し待ち
REPLY_SORT_WAIT_ROUNDS = 10
REPLY_SORT_SETTLE_MS = 4000
VIEWPORT = {"width": 1280, "height": 1000}
# 人気順の引用検索は1回でページ末尾まで送る(既定の2画面分では20件分の末尾に届かず次ページが来ないため)
QUOTE_SEARCH_SCROLL_SCRIPT = "window.scrollBy(0, document.body.scrollHeight)"

FIND_REPLY_SORT_BUTTON_JS = """
(texts) => {
  const root = document.querySelector('[data-testid="primaryColumn"]');
  if (!root) return null;
  for (const el of root.querySelectorAll('[aria-haspopup="menu"]')) {
    if (el.hasAttribute('data-testid')) continue;
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
  for (const el of document.querySelectorAll('#layers [role="menuitem"]')) {
    const text = (el.innerText || '').trim().split('\\n')[0].trim();
    if (!texts.includes(text)) continue;
    el.setAttribute('data-saezuri-sort-item', '1');
    return text;
  }
  return null;
}
"""


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
                        help="1種別あたりの取得上限件数(省略時はconfig.yamlのmax_items。"
                             "popularの引用は話題・最新それぞれの上限)")
    parser.add_argument("--sort", choices=(SORT_POPULAR, SORT_RECENT), default=SORT_POPULAR,
                        help="popular=人気順で集めて上位Markdownも出力(既定) / recent=従来の表示順で収集のみ")
    parser.add_argument("--top", type=int, default=ranking.DEFAULT_TOP,
                        help=f"Markdownに出す種別ごとの上位件数(いいね数順。既定: {ranking.DEFAULT_TOP})")
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


def next_related_out_path(raw_dir: Path, today: str | None = None, prefix: str = "related") -> Path:
    """raw_dir内の<prefix>_<今日の日付>_<連番>.jsonlを走査し、次の連番のパスを返す。"""
    today = today or datetime.now().strftime("%Y%m%d")
    max_seq = 0
    for path in Path(raw_dir).glob(f"{prefix}_{today}_*.jsonl"):
        suffix = path.stem.rsplit("_", 1)[-1]
        if suffix.isdigit():
            max_seq = max(max_seq, int(suffix))
    return Path(raw_dir) / f"{prefix}_{today}_{max_seq + 1:03d}.jsonl"


def quote_search_modes(sort: str) -> list[str]:
    """引用検索で回す検索タブ。popularは話題(top)と最新(live)の両方、recentは従来どおりliveのみ。"""
    return ["top", "live"] if sort == SORT_POPULAR else ["live"]


def quote_scroll_script(sort: str) -> str:
    """引用検索のスクロールJS。popularは末尾まで送り、recentは従来どおり既定のスクロール。"""
    return QUOTE_SEARCH_SCROLL_SCRIPT if sort == SORT_POPULAR else SCROLL_SCRIPT


def build_x_top_sections(
    posts: list[Post], start_id: str, start_handle: str | None, relations: set[str], top: int
) -> list[tuple[str, list[Post], int]]:
    """収集したrelationsのうちreply/quoteについて、直接のリプライ・引用のいいね数上位を作る。

    リプライは in_reply_to_id == 起点ID(返信への返信は除く)、引用は quoted_id == 起点ID で判定する
    (source_queryは初出の種別しか残らないため構造で判定する)。
    """
    specs = []
    if RELATION_REPLY in relations:
        specs.append(("リプライ", lambda p: p.in_reply_to_id == start_id))
    if RELATION_QUOTE in relations:
        specs.append(("引用", lambda p: p.quoted_id == start_id))
    return ranking.build_sections(posts, specs, start_id, start_handle, top)


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

def switch_reply_sort_to_likes(page, capture: GraphQLCapture) -> bool:
    """会話スレッドの並び替えメニューで「いいね」を選ぶ。切り替えられなければ警告してFalse。

    画面の操作だけで切り替え、通信の書き換えはしない。「いいね」を押す直前に関連度順の
    初回分を取り出しておき(いいね順の取り直しで起点ごと再取得されるため捨てる)、
    切り替えに失敗したらinject()で戻して既定の並びのまま収集を続けられるようにする。
    ブロック検知(429/チャレンジ)はここでは扱わず、この後のscroll_and_collectに任せる。
    """
    held: list[tuple[str, dict]] = []
    try:
        current = None
        for _ in range(REPLY_SORT_WAIT_ROUNDS):
            current = page.evaluate(FIND_REPLY_SORT_BUTTON_JS, list(REPLY_SORT_BUTTON_TEXTS))
            if current:
                break
            page.wait_for_timeout(1000)
        if not current:
            logger.warning("返信の並び替えメニューが見つからないため、既定の並びのまま収集します")
            return False
        if current in REPLY_SORT_LIKES_TEXTS:
            logger.info("返信の並びは既に「%s」です", current)
            return True
        page.locator('[data-saezuri-sort="1"]').first.click(timeout=5000)
        item = None
        for _ in range(5):
            page.wait_for_timeout(500)
            item = page.evaluate(FIND_REPLY_SORT_ITEM_JS, list(REPLY_SORT_LIKES_TEXTS))
            if item:
                break
        if not item:
            logger.warning("並び替えメニューに「いいね」が見つからないため、既定の並びのまま収集します")
            page.keyboard.press("Escape")
            return False
        held = capture.drain()
        page.locator('[data-saezuri-sort-item="1"]').first.click(timeout=5000)
        page.wait_for_timeout(REPLY_SORT_SETTLE_MS)
        after = page.evaluate(FIND_REPLY_SORT_BUTTON_JS, list(REPLY_SORT_BUTTON_TEXTS))
        if after not in REPLY_SORT_LIKES_TEXTS:
            logger.warning("返信の並びが「いいね」に切り替わらなかったため(表示: %s)、既定の並びのまま収集します", after)
            for operation, payload in held:
                capture.inject(operation, payload)
            return False
        logger.info("返信の並びを「%s」に切り替えました", after)
        return True
    except PlaywrightError as exc:
        logger.warning("返信の並び替えに失敗したため、既定の並びのまま収集します: %s", exc)
        for operation, payload in held:
            capture.inject(operation, payload)
        return False


def collect_thread(
    context,
    goto_url: str,
    start_id: str,
    source_query: str,
    save_replies: bool,
    max_items: int,
    wait_range: tuple[float, float],
    collected: dict[str, Post],
    sort_by_likes: bool = False,
) -> tuple[Post | None, Exception | None]:
    """①TweetDetailを傍受して会話スレッドを収集し、起点Postを特定する。

    save_repliesがTrueなら収集分(起点Post自身=focalを除く)をcollectedへマージする
    (reply種別)。Falseの場合は起点Postのハンドル取得のみ行いマージしない。
    sort_by_likesがTrueなら収集前に並び替えメニューで「いいね」を選ぶ(失敗しても続行)。
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
        if sort_by_likes:
            switch_reply_sort_to_likes(page, capture)
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
    scroll_script: str = SCROLL_SCRIPT,
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
            page, capture, parse, max_items=max_items, wait_seconds_range=wait_range,
            scroll_script=scroll_script,
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

    if args.top < 1:
        print("エラー: --topは1以上で指定してください", file=sys.stderr)
        return EXIT_ERROR

    relations = resolve_relations(args.replies, args.quotes, args.author)
    popular = args.sort == SORT_POPULAR

    config = load_config(args.config)
    raw_dir = Path(config["output"]["raw_dir"])
    state_dir = Path(config["output"]["state_dir"])
    max_items = args.max if args.max is not None else get_max_items(config)
    wait_range = get_wait_seconds_range(config)
    browser_name = args.browser or get_browser_name(config)

    # id→Post(初出優先)。中断時もここに溜まった分を保存する。
    collected: dict[str, Post] = {}
    blocked: Exception | None = None
    eff_handle = start_handle

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
                # 人気順は並び替えボタンを実機調査と同じ画面サイズで探す(recentは従来どおり既定サイズ)
                context = new_context(browser, config, **({"viewport": VIEWPORT} if popular else {}))
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
                        sort_by_likes=popular and RELATION_REPLY in relations,
                    )

                eff_handle = start_handle or (start_post.author_handle if start_post else None)

                # ②引用ポスト(popularは話題・最新の両方をそれぞれ上限まで集めてIDで合算)
                if RELATION_QUOTE in relations:
                    for mode in quote_search_modes(args.sort):
                        if blocked is not None:
                            break
                        quote_url = build_search_url(f"quoted_tweet_id:{start_id}", mode)
                        blocked = collect_timeline_relation(
                            context, quote_url, "SearchTimeline",
                            build_source_query(start_id, RELATION_QUOTE),
                            start_id, max_items, wait_range, collected,
                            scroll_script=quote_scroll_script(args.sort),
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

    if popular and relations & {RELATION_REPLY, RELATION_QUOTE}:
        sections = build_x_top_sections(
            list(collected.values()), start_id, eff_handle, relations, args.top
        )
        notes = ["ブロック検知で収集を途中終了しています(収集済み分のみで集計)"] if blocked else None
        text = ranking.render_top_markdown(
            title=f"X 人気リプライ・引用 上位{args.top}件(いいね数順)",
            origin_url=build_status_url(start_id, eff_handle),
            origin_handle=eff_handle,
            generated_at=now_iso(),
            sections=sections,
            notes=notes,
        )
        digest_dir = config["output"].get("digest_dir") or "digest"
        md_path = ranking.write_top_markdown(
            ranking.top_markdown_path(digest_dir, "x", start_id), text
        )
        for label, ranked, total in sections:
            print(f"{label}: 対象{total}件 / 上位{len(ranked)}件")
        print(f"上位Markdown: {md_path}")

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
