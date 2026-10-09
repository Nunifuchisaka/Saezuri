"""saezuri GraphQLレスポンス傍受・正規化 共通基盤

収集モジュール(search.py / user_tweets.py / trends.py / bookmarks.py)が共通で
importして使う。DOMパースではなくXの内部GraphQL APIレスポンスの傍受を主体とし、
DOM依存を最小化する(設計書「技術選定」「リスク・留意点」参照)。

提供するもの:
- GraphQLオペレーション名のURL判定 (extract_operation_name)
- page.on("response")に登録するレスポンス捕捉クラス (GraphQLCapture)
- 捕捉JSONをschema.Post / schema.TrendEntryへ正規化するパーサー
  (parse_timeline_payload / parse_trends_payload)
- スクロール+ランダム待機+上限制御のページネーションループ (scroll_and_collect)
- ブロック検知例外 (RateLimitedError / ChallengeDetectedError)

Xの内部API仕様は非公開かつ予告なく変わるため、パーサーはベストエフォート。
想定キーが欠けている・型が違う場合は例外を投げず、logger.warning()を出して
そのエントリだけスキップする(全体を落とさない)。
"""
from __future__ import annotations

import logging
import random
import re
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Optional, Sequence

from playwright.sync_api import Page, Response

from schema import MediaItem, Post, TrendEntry
from session import is_challenge_url, is_rate_limited

logger = logging.getLogger(__name__)

# XのGraphQLエンドポイント: https://x.com/i/api/graphql/<hash>/<OperationName>
GRAPHQL_URL_RE = re.compile(
    r"https://(?:x|twitter)\.com/i/api/graphql/[^/]+/([A-Za-z0-9_]+)"
)

# ツイート系タイムラインのオペレーション名(完全一致で判定)
TWEET_OPERATIONS = (
    "SearchTimeline",
    "UserTweets",
    "UserTweetsAndReplies",
    "Bookmarks",
    "TweetDetail",
)

# トレンド系のオペレーション名は正確な名称が変動しうる(ExplorePage等)ため、
# 完全一致ではなく「これらの語を含むか」で緩めに判定する
# Xがオペレーション名を差し替えた場合の別名(旧名指定でも新名のレスポンスを捕捉する)。
# 2026-10に UserTweets のプロフィール投稿一覧が UserOriginalsTimeline で届くことを確認(ペイロード構造は同等)
OPERATION_ALIASES = {
    "UserTweets": ("UserOriginalsTimeline",),
}

TREND_OPERATION_KEYWORDS =("ExplorePage", "Explore")

DEFAULT_WAIT_SECONDS_RANGE = (3.0, 15.0)
DEFAULT_MAX_ITEMS = 100

# 新規アイテムが増えないままこの回数スクロールしたら「末尾に達した」とみなす
DEFAULT_MAX_IDLE_ROUNDS = 3

SCROLL_SCRIPT = "window.scrollBy(0, window.innerHeight * 2)"


class RateLimitedError(RuntimeError):
    """レートリミット(429)を検知したことを表す例外。検知したら即中断する。"""


class ChallengeDetectedError(RuntimeError):
    """CAPTCHA/チャレンジ画面への遷移を検知したことを表す例外。自動突破は試みない。"""


def now_iso() -> str:
    """collected_atに使うISO 8601形式の現在時刻(ローカルタイムゾーン付き)を返す。"""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def extract_operation_name(url: str) -> Optional[str]:
    """GraphQLエンドポイントURLからオペレーション名を抽出する。対象外URLはNone。"""
    match = GRAPHQL_URL_RE.match(url)
    if match is None:
        return None
    return match.group(1)


def get_wait_seconds_range(config: dict) -> tuple[float, float]:
    """config.yamlのwait_seconds(min/max)を(最小秒, 最大秒)で返す。欠如時はデフォルト。"""
    wait = config.get("wait_seconds") or {}
    try:
        low = float(wait.get("min", DEFAULT_WAIT_SECONDS_RANGE[0]))
        high = float(wait.get("max", DEFAULT_WAIT_SECONDS_RANGE[1]))
    except (TypeError, ValueError):
        logger.warning("config: wait_secondsの値が不正のためデフォルト%sを使います", DEFAULT_WAIT_SECONDS_RANGE)
        return DEFAULT_WAIT_SECONDS_RANGE
    if low > high:
        low, high = high, low
    return (low, high)


def get_max_items(config: dict) -> int:
    """config.yamlのmax_itemsを返す。欠如・不正時はデフォルト(100)。"""
    try:
        return int(config.get("max_items", DEFAULT_MAX_ITEMS))
    except (TypeError, ValueError):
        logger.warning("config: max_itemsの値が不正のためデフォルト%dを使います", DEFAULT_MAX_ITEMS)
        return DEFAULT_MAX_ITEMS


class GraphQLCapture:
    """page.on("response")に登録して対象GraphQLレスポンスを溜めるバッファ。

    使い方(収集モジュール側):
        capture = GraphQLCapture(operations=["SearchTimeline"])
        capture.attach(page)
        page.goto(...)
        ... scroll_and_collect(page, capture, ...) ...
        capture.detach(page)

    レスポンスハンドラ内では本文を読まずResponseオブジェクトだけ保持し、
    drain()呼び出し時(スクロール後の待機を挟んだ後)にjson()で本文を読む。
    json()はPlaywright内部で本文受信完了を待ってからパースするため、
    別途finished()での二重待機はしない(finished()はブラウザ終了時に
    回収されないタスクを生成し無害な警告の原因になるため呼ばない)。
    ハンドラ内でのブロッキング読み出しによる取りこぼし・デッドロックを
    避けるための設計。
    """

    def __init__(
        self,
        operations: Sequence[str] = TWEET_OPERATIONS,
        operation_keywords: Sequence[str] = (),
    ) -> None:
        self._operations = set(operations)
        for operation in operations:
            self._operations.update(OPERATION_ALIASES.get(operation, ()))
        self._operation_keywords = tuple(operation_keywords)
        self._pending: list[tuple[str, Response]] = []
        self._injected: list[tuple[str, dict]] = []
        self._rate_limited = False

    def matches(self, operation: str) -> bool:
        """オペレーション名が捕捉対象かどうかを判定する。"""
        if operation in self._operations:
            return True
        return any(keyword in operation for keyword in self._operation_keywords)

    def attach(self, page: Page) -> None:
        """pageのresponseイベントにハンドラを登録する。"""
        page.on("response", self._on_response)

    def detach(self, page: Page) -> None:
        """attach()で登録したハンドラを解除する。"""
        page.remove_listener("response", self._on_response)

    def _on_response(self, response: Response) -> None:
        operation = extract_operation_name(response.url)
        if operation is None or not self.matches(operation):
            return
        if is_rate_limited(response.status):
            logger.warning("%s: レートリミット(429)を検知しました", operation)
            self._rate_limited = True
            return
        if response.status != 200:
            logger.warning("%s: status=%d のためスキップします", operation, response.status)
            return
        self._pending.append((operation, response))

    @property
    def rate_limited(self) -> bool:
        """捕捉対象レスポンスで429を検知していたらTrue。"""
        return self._rate_limited

    def raise_if_blocked(self, page: Optional[Page] = None) -> None:
        """429検知済みまたはチャレンジ画面遷移を検知したら対応する例外を送出する。"""
        if self._rate_limited:
            raise RateLimitedError("レートリミット(429)を検知したため収集を中断します。しばらく時間を置いてください。")
        if page is not None and is_challenge_url(page.url):
            raise ChallengeDetectedError(
                f"チャレンジ画面らしきURLに遷移しました: {page.url} "
                "自動突破は行いません。ブラウザで手動対応してください。"
            )

    def inject(self, operation: str, payload: dict) -> None:
        """レスポンス以外(ページ埋め込みJSON・読み出し済みペイロード等)を次のdrain()に載せる。"""
        self._injected.append((operation, payload))

    def drain(self) -> list[tuple[str, dict]]:
        """溜まった分のJSONペイロードを(オペレーション名, dict)のリストで取り出す。

        取り出した分は内部バッファから消える。inject()で差し込んだ分はレスポンスより先に返す。
        本文の読み出しに失敗したレスポンスは警告を出してスキップする(落とさない)。
        """
        pending, self._pending = self._pending, []
        results, self._injected = self._injected, []
        for operation, response in pending:
            try:
                payload = response.json()
            except Exception as exc:
                logger.warning("%s: レスポンス本文の読み出しに失敗したためスキップします: %s", operation, exc)
                continue
            if not isinstance(payload, dict):
                logger.warning("%s: ペイロードがJSONオブジェクトでないためスキップします", operation)
                continue
            results.append((operation, payload))
        return results

    def clear(self) -> None:
        """内部バッファを破棄する(429検知フラグは維持する)。"""
        self._pending = []


def _dig(data: Any, *keys: str) -> Any:
    """ネストしたdictをキー列でたどる。途中で欠如・型不一致ならNoneを返す。"""
    current = data
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _find_instructions(payload: dict) -> list:
    """ペイロードから timeline -> instructions[] を再帰探索で見つける。

    data配下のルートキーは種別ごとに異なる(search_by_raw_query / user /
    bookmark_timeline_v2 等)うえ予告なく変わるため、キー名を決め打ちせず
    「値がlistであるinstructionsキー」を探すことで構造変更に耐性を持たせる。
    """
    found: list = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            instructions = node.get("instructions")
            if isinstance(instructions, list):
                found.extend(instructions)
                return
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(payload)
    return found


def _iter_item_contents(instructions: list) -> Iterable[dict]:
    """instructions[]からitemContent(dict)を順に取り出す。

    TimelineAddEntriesのentries[]、TimelinePinEntry/TimelineReplaceEntryのentry、
    モジュール型エントリ(content.items[])のいずれにも対応する。
    """
    for instruction in instructions:
        if not isinstance(instruction, dict):
            continue
        entries = instruction.get("entries")
        if not isinstance(entries, list):
            entry = instruction.get("entry")
            entries = [entry] if isinstance(entry, dict) else []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            content = entry.get("content")
            if not isinstance(content, dict):
                continue
            item_content = content.get("itemContent")
            if isinstance(item_content, dict):
                yield item_content
            items = content.get("items")
            if isinstance(items, list):
                for item in items:
                    nested = _dig(item, "item", "itemContent")
                    if isinstance(nested, dict):
                        yield nested


def _parse_media(legacy: dict) -> list[MediaItem]:
    """legacy配下のextended_entities/entitiesからメディア情報を抽出する。"""
    media_list = _dig(legacy, "extended_entities", "media") or _dig(legacy, "entities", "media")
    if not isinstance(media_list, list):
        return []
    items: list[MediaItem] = []
    for media in media_list:
        if not isinstance(media, dict):
            continue
        media_type = media.get("type") or "unknown"
        url = None
        if media_type in ("video", "animated_gif"):
            variants = _dig(media, "video_info", "variants")
            if isinstance(variants, list):
                mp4s = [
                    v for v in variants
                    if isinstance(v, dict) and v.get("content_type") == "video/mp4" and v.get("url")
                ]
                if mp4s:
                    url = max(mp4s, key=lambda v: v.get("bitrate") or 0).get("url")
        if url is None:
            url = media.get("media_url_https") or media.get("media_url")
        if not url:
            logger.warning("media: URLキーが見つからないため1件スキップします(type=%s)", media_type)
            continue
        items.append(MediaItem(type=media_type, url=url))
    return items


def _unwrap_visibility_results(result: Any) -> Optional[dict]:
    """TweetWithVisibilityResults(閲覧制限付きツイート)の1段ネストを吸収する。"""
    if not isinstance(result, dict):
        return None
    if result.get("__typename") == "TweetWithVisibilityResults":
        result = result.get("tweet")
        if not isinstance(result, dict):
            return None
    return result


def _resolve_tweet_result(item_content: dict) -> Optional[dict]:
    """itemContentからtweet_results.resultを取り出す。ツイートでなければNone。"""
    return _unwrap_visibility_results(_dig(item_content, "tweet_results", "result"))


def _resolve_quoted_result(result: dict) -> Optional[dict]:
    """resultのquoted_status_result.resultを取り出す。引用ポストでなければNone。"""
    return _unwrap_visibility_results(_dig(result, "quoted_status_result", "result"))


def _parse_author(result: dict) -> tuple[Optional[str], Optional[str]]:
    """resultから(author_handle, author_name)を取り出す。

    従来はuser.legacy配下だが、新しいペイロードではuser.core配下に
    移っている場合があるため両方を試す。
    """
    user = _dig(result, "core", "user_results", "result")
    if not isinstance(user, dict):
        return (None, None)
    handle = _dig(user, "legacy", "screen_name") or _dig(user, "core", "screen_name")
    name = _dig(user, "legacy", "name") or _dig(user, "core", "name")
    return (handle, name)


def _parse_article(result: dict) -> tuple[Optional[str], Optional[str]]:
    """resultのarticle.article_results.resultから(記事タイトル, 本文)を取り出す。

    記事(X Article)へのリンクを含まない通常の投稿では(None, None)を返す
    (これは異常系ではなく通常のケースのため警告は出さない)。本文は
    content_state.blocks[].textを改行で連結したプレーンテキスト(太字等の
    インライン装飾・箇条書き記号は失われる)。blocksが無い・空の場合は
    本文None(タイトルのみのケースを許容する)。
    """
    article = _dig(result, "article", "article_results", "result")
    if not isinstance(article, dict):
        return None, None
    title = article.get("title")
    blocks = _dig(article, "content_state", "blocks")
    text = None
    if isinstance(blocks, list):
        lines = [b.get("text") or "" for b in blocks if isinstance(b, dict)]
        if lines:
            text = "\n".join(lines)
    return title, text


def _parse_tweet_result(
    result: dict,
    operation: str,
    source_query: Optional[str],
    collected_at: str,
) -> Optional[Post]:
    """tweet_results.result 1件をPostへ正規化する。必須キー欠如時はNone(警告済み)。"""
    legacy = result.get("legacy") or _dig(result, "tweet", "legacy")
    if not isinstance(legacy, dict):
        logger.warning("%s: legacyキーが見つからないため1件スキップします", operation)
        return None

    post_id = legacy.get("id_str") or result.get("rest_id")
    if not post_id:
        logger.warning("%s: id_str/rest_idが見つからないため1件スキップします", operation)
        return None

    text = legacy.get("full_text")
    if text is None:
        logger.warning("%s: full_textが見つからないため1件スキップします(id=%s)", operation, post_id)
        return None

    handle, name = _parse_author(result)
    if not handle:
        logger.warning("%s: 著者情報(screen_name)が見つからないため1件スキップします(id=%s)", operation, post_id)
        return None

    article_title, article_text = _parse_article(result)

    return Post(
        id=str(post_id),
        url=f"https://x.com/{handle}/status/{post_id}",
        author_handle=handle,
        author_name=name or "",
        created_at=legacy.get("created_at") or "",
        text=text,
        article_title=article_title,
        article_text=article_text,
        lang=legacy.get("lang"),
        like_count=int(legacy.get("favorite_count") or 0),
        repost_count=int(legacy.get("retweet_count") or 0),
        reply_count=int(legacy.get("reply_count") or 0),
        quote_count=int(legacy.get("quote_count") or 0),
        media=_parse_media(legacy),
        is_repost="retweeted_status_result" in legacy,
        quoted_id=legacy.get("quoted_status_id_str"),
        in_reply_to_id=legacy.get("in_reply_to_status_id_str"),
        source_query=source_query,
        collected_at=collected_at,
    )


def parse_timeline_payload(
    payload: dict,
    operation: str = "",
    source_query: Optional[str] = None,
    collected_at: Optional[str] = None,
) -> list[Post]:
    """タイムライン系GraphQLペイロード1件からPostのリストを抽出する。

    SearchTimeline / UserTweets / UserTweetsAndReplies / Bookmarks共通。
    source_queryには検索クエリ・対象ハンドル等の呼び出し元コンテキストを渡す。
    構造が想定と違う箇所は警告を出してそのエントリだけスキップする。
    """
    collected_at = collected_at or now_iso()
    instructions = _find_instructions(payload)
    if not instructions:
        logger.warning("%s: instructionsが見つかりません(API構造変更の可能性)", operation)
        return []

    posts: list[Post] = []
    for item_content in _iter_item_contents(instructions):
        if item_content.get("itemType") not in (None, "TimelineTweet"):
            continue
        # プロモーション(広告)は収集対象外
        if "promotedMetadata" in item_content:
            continue
        result = _resolve_tweet_result(item_content)
        if result is None:
            continue
        post = _parse_tweet_result(result, operation, source_query, collected_at)
        if post is not None:
            posts.append(post)
    return posts


def find_tweet_result(payload: dict, tweet_id: str) -> Optional[dict]:
    """payload中からidがtweet_idに一致するtweet_results.resultの生dictを探す。

    parse_timeline_payload()は正規化後のPostしか返さないため、quoted_status_result
    等の正規化前データを参照したい呼び出し元(tweet.pyの引用元投稿取得)向けに用意する。
    見つからなければNone。
    """
    instructions = _find_instructions(payload)
    for item_content in _iter_item_contents(instructions):
        if item_content.get("itemType") not in (None, "TimelineTweet"):
            continue
        result = _resolve_tweet_result(item_content)
        if result is None:
            continue
        legacy = result.get("legacy") or _dig(result, "tweet", "legacy")
        if not isinstance(legacy, dict):
            continue
        post_id = legacy.get("id_str") or result.get("rest_id")
        if post_id and str(post_id) == tweet_id:
            return result
    return None


def parse_quoted_post(
    result: dict,
    origin_id: str,
    operation: str,
    collected_at: str,
) -> Optional[Post]:
    """resultのquoted_status_resultを検出し、引用元投稿をPostへ正規化する。

    起点投稿(result)が引用ツイートでない場合、または引用元が削除済み等で
    必須キーを欠く場合はNone(後者は_parse_tweet_result内でlogger.warning済み)。
    tweet.py専用(parse_timeline_payloadを使う他の収集モジュールはこの関数を呼ばない)。
    """
    quoted_result = _resolve_quoted_result(result)
    if quoted_result is None:
        return None
    return _parse_tweet_result(
        quoted_result, operation, f"quoted-by:{origin_id}", collected_at
    )


_VOLUME_RE = re.compile(r"([\d,.]+)\s*([KkMm万])?")

_VOLUME_UNITS = {"k": 1_000, "m": 1_000_000, "万": 10_000}


def _parse_tweet_volume(text: Optional[str]) -> Optional[int]:
    """"12.3K posts" / "1.5万件のポスト" 等の文字列から件数を推定する。不明ならNone。"""
    if not text:
        return None
    match = _VOLUME_RE.search(text)
    if match is None:
        return None
    number_text = match.group(1).replace(",", "").rstrip(".")
    if not number_text:
        return None
    try:
        number = float(number_text)
    except ValueError:
        return None
    unit = (match.group(2) or "").lower()
    return int(number * _VOLUME_UNITS.get(unit, 1))


def parse_trends_payload(
    payload: dict,
    operation: str = "",
    collected_at: Optional[str] = None,
) -> list[TrendEntry]:
    """トレンド系GraphQLペイロード1件からTrendEntryのリストを抽出する。

    ExplorePage系の正確なレスポンス構造は非公開で変動しうるため、
    itemType == "TimelineTrend" のitemContent(なければtrend系キーを持つdict)を
    ベストエフォートで拾う。rankは出現順で振る。
    """
    collected_at = collected_at or now_iso()
    instructions = _find_instructions(payload)
    if not instructions:
        logger.warning("%s: instructionsが見つかりません(API構造変更の可能性)", operation)
        return []

    entries: list[TrendEntry] = []
    for item_content in _iter_item_contents(instructions):
        if item_content.get("itemType") != "TimelineTrend":
            continue
        word = item_content.get("name") or _dig(item_content, "trend", "name")
        if not word:
            logger.warning("%s: トレンド名(name)が見つからないため1件スキップします", operation)
            continue
        volume = _parse_tweet_volume(
            item_content.get("trend_metadata", {}).get("meta_description")
            if isinstance(item_content.get("trend_metadata"), dict)
            else item_content.get("metaDescription")
        )
        entries.append(
            TrendEntry(
                trend_word=word,
                tweet_volume=volume,
                rank=len(entries) + 1,
                collected_at=collected_at,
            )
        )
    return entries


def _default_key(item: Any) -> Optional[str]:
    """重複排除キー。Postはid、TrendEntryはtrend_wordを使う。"""
    key = getattr(item, "id", None) or getattr(item, "trend_word", None)
    return str(key) if key is not None else None


def scroll_and_collect(
    page: Page,
    capture: GraphQLCapture,
    parse_payload: Callable[[dict, str], list],
    max_items: int = DEFAULT_MAX_ITEMS,
    wait_seconds_range: tuple[float, float] = DEFAULT_WAIT_SECONDS_RANGE,
    max_idle_rounds: int = DEFAULT_MAX_IDLE_ROUNDS,
    key_fn: Callable[[Any], Optional[str]] = _default_key,
    scroll_script: str = SCROLL_SCRIPT,
) -> list:
    """上限に達するまで「捕捉分の取り出し→スクロール→ランダム待機」を繰り返す。

    - parse_payload: (payload, operation) -> アイテムのリスト。収集モジュール側で
      functools.partial(parse_timeline_payload, source_query=...) 等を渡す
    - max_items: 上限件数(config.yamlのmax_itemsを渡す)。達したら停止し切り詰めて返す
    - wait_seconds_range: スクロール間のランダム待機秒(config.yamlのwait_seconds)
    - max_idle_rounds: 新規アイテムなしがこの回数続いたら末尾到達とみなし停止
    - scroll_script: スクロールに使うJS。既定はウィンドウ全体のスクロールで、内部の
      スクロールコンテナで追加読み込みするページ(Instagramのコメント欄等)は差し替える
    - 429検知でRateLimitedError、チャレンジ画面検知でChallengeDetectedErrorを送出

    呼び出し前にcapture.attach(page)とpage.goto(対象URL)を済ませておくこと。
    待機はtime.sleep()ではなくpage.wait_for_timeout()を使う(sync APIでは
    Playwright呼び出し中でないとresponseイベントが配送されないため)。
    """
    items: list = []
    seen_keys: set[str] = set()
    idle_rounds = 0

    while True:
        capture.raise_if_blocked(page)

        new_count = 0
        for operation, payload in capture.drain():
            for item in parse_payload(payload, operation):
                key = key_fn(item)
                if key is not None:
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                items.append(item)
                new_count += 1

        capture.raise_if_blocked(page)

        if len(items) >= max_items:
            logger.info("取得上限(%d件)に達したため停止します", max_items)
            return items[:max_items]

        if new_count == 0:
            idle_rounds += 1
            if idle_rounds >= max_idle_rounds:
                logger.info("新規取得なしが%d回続いたため停止します(計%d件)", idle_rounds, len(items))
                return items
        else:
            idle_rounds = 0

        page.evaluate(scroll_script)
        wait_ms = random.uniform(*wait_seconds_range) * 1000
        page.wait_for_timeout(wait_ms)
