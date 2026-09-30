"""saezuri Instagram収集 共通基盤

ig_login.py / ig_user_posts.py から import して使う。X向けの session.py / capture.py と
同じ方針(手動ログインのstorage_state再利用、内部APIレスポンスの傍受、DOM依存の最小化)で
Instagram(www.instagram.com)を扱う。

提供するもの:
- Instagram用storage_stateのパス解決・BrowserContext生成・ログイン状態検証
  (get_instagram_storage_state_path / new_instagram_context / require_instagram_login)
- 内部APIのエンドポイント判定 (extract_endpoint_name)
- page.on("response")に登録するレスポンス捕捉クラス (InstagramCapture)。
  capture.GraphQLCaptureを継承しているため、capture.scroll_and_collectにそのまま渡せる
- 捕捉JSONをschema.Postへ正規化するパーサー (parse_instagram_payload)

Instagramの内部API仕様は非公開かつ予告なく変わるため、パーサーはベストエフォート。
キー名・ルート構造を決め打ちせず「投稿(メディア)らしいdict」を再帰探索で拾い、
想定キーが欠けている場合は例外を投げずlogger.warning()を出してその1件だけスキップする。
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from playwright.sync_api import BrowserContext, Page, Response

from capture import ChallengeDetectedError, GraphQLCapture, RateLimitedError, now_iso
from schema import MediaItem, Post
from session import SessionExpiredError, is_rate_limited

logger = logging.getLogger(__name__)

INSTAGRAM_HOME_URL = "https://www.instagram.com/"
INSTAGRAM_LOGIN_URL = "https://www.instagram.com/accounts/login/"

DEFAULT_STORAGE_STATE_PATH = "session/instagram_storage_state.json"

# ログイン済みブラウザにのみ付与されるCookie名
SESSION_COOKIE_NAME = "sessionid"

# ログイン画面へのリダイレクトを判定するためのURLキーワード
LOGIN_URL_KEYWORDS = ("/accounts/login",)

# チャレンジ画面/アカウント制限画面を判定するためのURLキーワード
CHALLENGE_URL_KEYWORDS = ("/challenge", "/checkpoint", "/accounts/suspended", "/accounts/disabled")

RELOGIN_MESSAGE = (
    "Instagramのセッションが失効しています。ig_login.pyを再実行して手動ログインし直してください。"
)

# 捕捉対象の内部APIエンドポイント。(URL正規表現, ログ等に使う名前)
# - web_profile_info: プロフィールを開いた直後の初回分(旧GraphQL形式のノード)
# - feed_user: v1形式のユーザーフィード
# - graphql_query / api_graphql: スクロール時の追加読み込み(v1形式のノード)。
#   他用途のクエリも同じURLを使うため、ここでは広めに拾いパーサー側で投稿だけを抽出する
ENDPOINT_PATTERNS = (
    (re.compile(r"https://(?:www\.)?instagram\.com/api/v1/users/web_profile_info/"), "web_profile_info"),
    (re.compile(r"https://(?:www\.)?instagram\.com/api/v1/feed/user/"), "feed_user"),
    (re.compile(r"https://(?:www\.)?instagram\.com/graphql/query"), "graphql_query"),
    (re.compile(r"https://(?:www\.)?instagram\.com/api/graphql"), "api_graphql"),
)

# v1形式のmedia_type
MEDIA_TYPE_PHOTO = 1
MEDIA_TYPE_VIDEO = 2
MEDIA_TYPE_CAROUSEL = 8

# ストーリーズ(ハイライト含む)は投稿ではないため収集対象外
EXCLUDED_PRODUCT_TYPES = ("story",)


# ---------------------------------------------------------------------------
# セッション
# ---------------------------------------------------------------------------

def get_instagram_config(config: dict) -> dict:
    """config.yamlのinstagramセクションを返す。欠如時は空辞書。"""
    section = config.get("instagram")
    return section if isinstance(section, dict) else {}


def get_instagram_storage_state_path(config: dict) -> Path:
    """Instagram用storage_stateファイルのパスを返す(X用とは別ファイル)。"""
    path = get_instagram_config(config).get("storage_state_path") or DEFAULT_STORAGE_STATE_PATH
    return Path(path)


def get_instagram_watch_accounts(config: dict) -> list[str]:
    """config.yamlのinstagram.watch_accountsを@除去済みのリストで返す。"""
    accounts = get_instagram_config(config).get("watch_accounts") or []
    return [str(a).strip().lstrip("@") for a in accounts if str(a).strip()]


def new_instagram_context(browser, config: dict, **kwargs) -> BrowserContext:
    """Instagram用storage_stateをロードしてBrowserContextを生成する。

    ig_login.py未実行でファイルが存在しない場合はFileNotFoundErrorを送出する。
    """
    storage_state_path = get_instagram_storage_state_path(config)
    if not storage_state_path.exists():
        raise FileNotFoundError(
            f"Instagramのstorage_stateファイルが見つかりません: {storage_state_path}\n"
            "先にig_login.pyを実行して手動ログインしてください。"
        )
    return browser.new_context(storage_state=str(storage_state_path), **kwargs)


def has_session_cookie(cookies: Iterable[dict]) -> bool:
    """Cookie一覧にInstagramのログインセッションCookie(sessionid)が含まれるか。"""
    for cookie in cookies:
        if cookie.get("name") == SESSION_COOKIE_NAME and "instagram.com" in (cookie.get("domain") or ""):
            if cookie.get("value"):
                return True
    return False


def is_login_url(url: str) -> bool:
    return any(keyword in url for keyword in LOGIN_URL_KEYWORDS)


def is_instagram_challenge_url(url: str) -> bool:
    """URLがInstagramのチャレンジ画面/アカウント制限画面らしきものかを簡易判定する。"""
    return any(keyword in url for keyword in CHALLENGE_URL_KEYWORDS)


def verify_instagram_login(context: BrowserContext) -> bool:
    """instagram.comを開き、ログイン画面へ飛ばされずsessionid Cookieが残っているかで判定する。"""
    page = context.new_page()
    try:
        page.goto(INSTAGRAM_HOME_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
        current_url = page.url
    finally:
        page.close()
    if is_login_url(current_url):
        return False
    return has_session_cookie(context.cookies())


def require_instagram_login(context: BrowserContext) -> None:
    """verify_instagram_login()がFalseならSessionExpiredErrorを送出する。自動再ログインはしない。"""
    if not verify_instagram_login(context):
        raise SessionExpiredError(RELOGIN_MESSAGE)


# ---------------------------------------------------------------------------
# レスポンス捕捉
# ---------------------------------------------------------------------------

def extract_endpoint_name(url: str) -> Optional[str]:
    """捕捉対象の内部APIエンドポイントURLなら名前を、対象外ならNoneを返す。"""
    for pattern, name in ENDPOINT_PATTERNS:
        if pattern.match(url):
            return name
    return None


def payload_requests_challenge(payload: dict) -> bool:
    """APIレスポンス本文がチェックポイント(チャレンジ)要求かどうかを判定する。"""
    message = payload.get("message")
    return (
        message in ("checkpoint_required", "challenge_required")
        or isinstance(payload.get("checkpoint_url"), str)
        or isinstance(payload.get("challenge"), dict)
    )


class InstagramCapture(GraphQLCapture):
    """InstagramのAPIレスポンスを溜めるバッファ。

    GraphQLCaptureと同じく、ハンドラ内では本文を読まずResponseだけ保持し、
    drain()で初めてjson()を読む。429検知でrate_limitedフラグを立て、
    raise_if_blocked()でRateLimitedError/ChallengeDetectedErrorを送出する
    (capture.scroll_and_collectからそのまま呼ばれる)。
    """

    # サブクラス(threads.pyのThreadsCapture)で差し替えるメッセージ
    service_name = "Instagram"
    relogin_message = RELOGIN_MESSAGE

    def __init__(self) -> None:
        super().__init__(operations=())
        self._challenge = False

    def endpoint_name(self, url: str) -> Optional[str]:
        """捕捉対象URLならエンドポイント名を返す。サブクラスで差し替える。"""
        return extract_endpoint_name(url)

    def is_login_url(self, url: str) -> bool:
        return is_login_url(url)

    def is_challenge_url(self, url: str) -> bool:
        return is_instagram_challenge_url(url)

    def _on_response(self, response: Response) -> None:
        endpoint = self.endpoint_name(response.url)
        if endpoint is None:
            return
        if is_rate_limited(response.status):
            logger.warning("%s: レートリミット(429)を検知しました", endpoint)
            self._rate_limited = True
            return
        if response.status != 200:
            logger.warning("%s: status=%d のためスキップします", endpoint, response.status)
            return
        self._pending.append((endpoint, response))

    def drain(self) -> list[tuple[str, dict]]:
        """GraphQLCapture.drain()に加え、チャレンジ要求の本文を検知したらフラグを立てる。"""
        results: list[tuple[str, dict]] = []
        for endpoint, payload in super().drain():
            if payload_requests_challenge(payload):
                logger.warning("%s: チャレンジ(checkpoint)要求を検知しました", endpoint)
                self._challenge = True
                continue
            results.append((endpoint, payload))
        return results

    def raise_if_blocked(self, page: Optional[Page] = None) -> None:
        if self._rate_limited:
            raise RateLimitedError("レートリミット(429)を検知したため収集を中断します。しばらく時間を置いてください。")
        if self._challenge or (page is not None and self.is_challenge_url(page.url)):
            raise ChallengeDetectedError(
                f"{self.service_name}のチャレンジ画面/確認要求を検知しました: {page.url if page else ''} "
                "自動突破は行いません。ブラウザで手動対応してください。"
            )
        if page is not None and self.is_login_url(page.url):
            raise SessionExpiredError(self.relogin_message)


# ---------------------------------------------------------------------------
# パーサー
# ---------------------------------------------------------------------------

def _dig(data: Any, *keys: Any) -> Any:
    """ネストしたdict/listをキー(listは整数index)列でたどる。途中で欠如・型不一致ならNone。"""
    current = data
    for key in keys:
        if isinstance(key, int):
            if not isinstance(current, list) or not -len(current) <= key < len(current):
                return None
            current = current[key]
        else:
            if not isinstance(current, dict):
                return None
            current = current.get(key)
    return current


def _is_v1_media(node: dict) -> bool:
    """v1形式(graphql/query・feed系)の投稿ノードらしいか。"""
    return isinstance(node.get("code"), str) and "taken_at" in node and ("pk" in node or "id" in node)


def _is_graph_media(node: dict) -> bool:
    """旧GraphQL形式(web_profile_info系)の投稿ノードらしいか。"""
    return isinstance(node.get("shortcode"), str) and "taken_at_timestamp" in node


def iter_media_nodes(payload: Any) -> Iterable[dict]:
    """ペイロードから投稿ノード(dict)を再帰探索で順に取り出す。

    ルートキー(xdt_api__v1__feed__user_timeline_graphql_connection / user.edge_owner_to_timeline_media 等)
    は予告なく変わるため決め打ちしない。投稿ノードを見つけたらその内側(カルーセルの子要素等)には
    潜らない(子要素を別投稿として重複計上しないため)。
    """
    if isinstance(payload, dict):
        if _is_v1_media(payload) or _is_graph_media(payload):
            yield payload
            return
        for value in payload.values():
            yield from iter_media_nodes(value)
    elif isinstance(payload, list):
        for value in payload:
            yield from iter_media_nodes(value)


def _timestamp_to_iso(value: Any) -> str:
    """UNIX秒をUTCのISO 8601文字列に変換する。不正値は空文字。"""
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def _to_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _v1_single_media(node: dict) -> Optional[MediaItem]:
    """v1形式の1メディア(投稿本体またはカルーセル子要素)をMediaItemにする。"""
    media_type = node.get("media_type")
    if media_type == MEDIA_TYPE_VIDEO:
        url = _dig(node, "video_versions", 0, "url")
        if url:
            return MediaItem(type="video", url=url)
    url = _dig(node, "image_versions2", "candidates", 0, "url")
    if url:
        return MediaItem(type="photo", url=url)
    return None


def _parse_v1_media_list(node: dict) -> list[MediaItem]:
    children = node.get("carousel_media")
    targets = children if isinstance(children, list) and children else [node]
    items = []
    for child in targets:
        if not isinstance(child, dict):
            continue
        item = _v1_single_media(child)
        if item is None:
            logger.warning("media: URLキーが見つからないため1件スキップします(code=%s)", node.get("code"))
            continue
        items.append(item)
    return items


def _graph_single_media(node: dict) -> Optional[MediaItem]:
    if node.get("is_video") and node.get("video_url"):
        return MediaItem(type="video", url=node["video_url"])
    url = node.get("display_url")
    if url:
        return MediaItem(type="photo", url=url)
    return None


def _parse_graph_media_list(node: dict) -> list[MediaItem]:
    edges = _dig(node, "edge_sidecar_to_children", "edges")
    children = [e.get("node") for e in edges if isinstance(e, dict)] if isinstance(edges, list) else []
    targets = [c for c in children if isinstance(c, dict)] or [node]
    items = []
    for child in targets:
        item = _graph_single_media(child)
        if item is None:
            logger.warning("media: URLキーが見つからないため1件スキップします(shortcode=%s)", node.get("shortcode"))
            continue
        items.append(item)
    return items


def _parse_v1_node(
    node: dict, endpoint: str, default_handle: Optional[str], source_query: Optional[str], collected_at: str
) -> Optional[Post]:
    if node.get("product_type") in EXCLUDED_PRODUCT_TYPES:
        return None
    code = node["code"]
    post_id = node.get("pk") or str(node.get("id") or "").split("_")[0]
    if not post_id:
        logger.warning("%s: pk/idが見つからないため1件スキップします(code=%s)", endpoint, code)
        return None
    handle = _dig(node, "user", "username") or _dig(node, "owner", "username") or default_handle
    if not handle:
        logger.warning("%s: 投稿者(username)が見つからないため1件スキップします(code=%s)", endpoint, code)
        return None
    name = _dig(node, "user", "full_name") or _dig(node, "owner", "full_name") or ""
    return Post(
        id=str(post_id),
        url=f"https://www.instagram.com/p/{code}/",
        author_handle=handle,
        author_name=name,
        created_at=_timestamp_to_iso(node.get("taken_at")),
        text=_dig(node, "caption", "text") or "",
        like_count=_to_int(node.get("like_count")),
        repost_count=0,
        reply_count=_to_int(node.get("comment_count")),
        quote_count=0,
        media=_parse_v1_media_list(node),
        source_query=source_query,
        collected_at=collected_at,
    )


def _parse_graph_node(
    node: dict, endpoint: str, default_handle: Optional[str], source_query: Optional[str], collected_at: str
) -> Optional[Post]:
    shortcode = node["shortcode"]
    post_id = node.get("id")
    if not post_id:
        logger.warning("%s: idが見つからないため1件スキップします(shortcode=%s)", endpoint, shortcode)
        return None
    handle = _dig(node, "owner", "username") or default_handle
    if not handle:
        logger.warning("%s: 投稿者(username)が見つからないため1件スキップします(shortcode=%s)", endpoint, shortcode)
        return None
    like_count = _dig(node, "edge_liked_by", "count")
    if like_count is None:
        like_count = _dig(node, "edge_media_preview_like", "count")
    return Post(
        id=str(post_id),
        url=f"https://www.instagram.com/p/{shortcode}/",
        author_handle=handle,
        author_name=_dig(node, "owner", "full_name") or "",
        created_at=_timestamp_to_iso(node.get("taken_at_timestamp")),
        text=_dig(node, "edge_media_to_caption", "edges", 0, "node", "text") or "",
        like_count=_to_int(like_count),
        repost_count=0,
        reply_count=_to_int(_dig(node, "edge_media_to_comment", "count")),
        quote_count=0,
        media=_parse_graph_media_list(node),
        source_query=source_query,
        collected_at=collected_at,
    )


def parse_instagram_payload(
    payload: dict,
    endpoint: str = "",
    default_handle: Optional[str] = None,
    source_query: Optional[str] = None,
    collected_at: Optional[str] = None,
) -> list[Post]:
    """InstagramのAPIペイロード1件からPostのリストを抽出する。

    - default_handle: 投稿ノードに投稿者情報が含まれない場合に使うユーザー名
      (web_profile_infoのノードはownerを持たないことがあるため)
    - Postのrepost_count/quote_countはInstagramに該当概念がないため常に0、
      reply_countにはコメント数を入れる。いいね数非表示の投稿はlike_count=0になる
    """
    collected_at = collected_at or now_iso()
    posts: list[Post] = []
    for node in iter_media_nodes(payload):
        if _is_v1_media(node):
            post = _parse_v1_node(node, endpoint, default_handle, source_query, collected_at)
        else:
            post = _parse_graph_node(node, endpoint, default_handle, source_query, collected_at)
        if post is not None:
            posts.append(post)
    return posts
