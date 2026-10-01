"""saezuri TikTok収集 共通基盤

tt_login.py / tt_user_posts.py / tt_profiles.py から import して使う。X・Instagram・Threadsと
同じ方針(手動ログインのstorage_state再利用、内部APIレスポンスの傍受、オンデマンド実行、
自動リトライ・自動突破なし)でTikTok(www.tiktok.com)の特定アカウントの投稿を扱う。

提供するもの:
- TikTok用storage_stateのパス解決・BrowserContext生成・ログイン状態検証
  (get_tiktok_storage_state_path / new_tiktok_context / require_tiktok_login)
- 内部APIのエンドポイント判定 (extract_tiktok_endpoint_name)
- レスポンス捕捉クラス (TikTokCapture)。InstagramCaptureを継承しているため
  capture.scroll_and_collectにそのまま渡せる。CAPTCHA(スライダー/パズル)は
  通信先URL・画面のDOM要素・チャレンジURLのいずれかで検知し、ChallengeDetectedErrorを送出する
  (突破・回避は一切行わない)
- 捕捉JSONをschema.Postへ正規化するパーサー (parse_tiktok_payload)

TikTokの内部API仕様は非公開かつ予告なく変わるため、パーサーはベストエフォート。
ルートキーを決め打ちせず「TikTok動画らしいdict」を再帰探索で拾い、想定キーが欠けている場合は
例外を投げずlogger.warning()を出してその1件だけスキップする。
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import urlparse

from playwright.sync_api import BrowserContext, Page, Response

from capture import now_iso
from instagram import InstagramCapture, _dig, _timestamp_to_iso, _to_int
from schema import MediaItem, Post
from session import SessionExpiredError

logger = logging.getLogger(__name__)

TIKTOK_HOME_URL = "https://www.tiktok.com/"
TIKTOK_LOGIN_URL = "https://www.tiktok.com/login"

DEFAULT_STORAGE_STATE_PATH = "session/tiktok_storage_state.json"

# ログイン済みブラウザにのみ付与されるCookie名
SESSION_COOKIE_NAMES = ("sessionid", "sessionid_ss")
SESSION_COOKIE_DOMAIN = "tiktok.com"

LOGIN_URL_KEYWORDS = ("/login",)
# CAPTCHA画面のURL(想定)。ユーザーページ(/@xxx)や動画ページでは現れない
CHALLENGE_URL_KEYWORDS = ("/captcha",)

# 画面上のCAPTCHA要素(想定)。見つかったら即中断する
CAPTCHA_SELECTORS = (
    "#captcha-verify-container-main-page",
    ".captcha-verify-container",
    "#captcha_container",
    "[class*='captcha_verify']",
    "#tiktok-verify-ele",
)

RELOGIN_MESSAGE = (
    "TikTokのセッションが失効しています。tt_login.pyを再実行して手動ログインし直してください。"
)

# 捕捉対象の内部API。(URL正規表現, 名前)
# - item_list: プロフィールの投稿一覧(初回・スクロール時の追加読み込み。リポスト一覧は /api/repost/ なので対象外)
# - user_detail: プロフィール情報(フォロワー数等)
ENDPOINT_PATTERNS = (
    (re.compile(r"https://(?:www\.)?tiktok\.com/api/post/item_list/"), "item_list"),
    (re.compile(r"https://(?:www\.)?tiktok\.com/api/user/detail/"), "user_detail"),
)

# カバー画像の優先順(最初に見つかった1件だけをphotoにする。重複保存を避けるため)
COVER_KEYS = ("originCover", "cover", "dynamicCover")
VIDEO_URL_KEYS = ("playAddr", "downloadAddr")


# ---------------------------------------------------------------------------
# セッション
# ---------------------------------------------------------------------------

def get_tiktok_config(config: dict) -> dict:
    """config.yamlのtiktokセクションを返す。欠如時は空辞書。"""
    section = config.get("tiktok")
    return section if isinstance(section, dict) else {}


def get_tiktok_storage_state_path(config: dict) -> Path:
    """TikTok用storage_stateファイルのパスを返す(他SNS用とは別ファイル)。"""
    path = get_tiktok_config(config).get("storage_state_path") or DEFAULT_STORAGE_STATE_PATH
    return Path(path)


def get_tiktok_watch_accounts(config: dict) -> list[str]:
    """config.yamlのtiktok.watch_accountsを@除去済みのリストで返す。"""
    accounts = get_tiktok_config(config).get("watch_accounts") or []
    return [str(a).strip().lstrip("@") for a in accounts if str(a).strip()]


def new_tiktok_context(browser, config: dict, **kwargs) -> BrowserContext:
    """TikTok用storage_stateをロードしてBrowserContextを生成する。

    tt_login.py未実行でファイルが存在しない場合はFileNotFoundErrorを送出する。
    """
    storage_state_path = get_tiktok_storage_state_path(config)
    if not storage_state_path.exists():
        raise FileNotFoundError(
            f"TikTokのstorage_stateファイルが見つかりません: {storage_state_path}\n"
            "先にtt_login.pyを実行して手動ログインしてください。"
        )
    return browser.new_context(storage_state=str(storage_state_path), **kwargs)


def has_tiktok_session_cookie(cookies: Iterable[dict], now: Optional[float] = None) -> bool:
    """Cookie一覧にTikTokのログインセッションCookie(sessionid)が有効期限内で含まれるか。"""
    now = time.time() if now is None else now
    for cookie in cookies:
        if (
            cookie.get("name") in SESSION_COOKIE_NAMES
            and cookie.get("value")
            and SESSION_COOKIE_DOMAIN in (cookie.get("domain") or "")
        ):
            expires = cookie.get("expires")
            # expires=-1(または未設定)はセッションCookie扱いで有効とみなす
            if isinstance(expires, (int, float)) and 0 < expires < now:
                continue
            return True
    return False


def is_tiktok_login_url(url: str) -> bool:
    return any(keyword in url for keyword in LOGIN_URL_KEYWORDS)


def is_tiktok_challenge_url(url: str) -> bool:
    return any(keyword in url for keyword in CHALLENGE_URL_KEYWORDS)


def is_captcha_request_url(url: str) -> bool:
    """通信先がTikTokのCAPTCHA配信(.../captcha/...)かを判定する。"""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    return (host == "tiktok.com" or host.endswith(".tiktok.com")) and "/captcha/" in parsed.path


def require_tiktok_login(context: BrowserContext) -> None:
    """storage_stateにsessionid Cookieが無い・期限切れならSessionExpiredErrorを送出する。

    TikTokは未ログインでもプロフィールを開けるため、追加アクセス(CAPTCHAのリスク)を避けて
    Cookieの有無・期限のみで判定する。サーバー側で失効した場合は収集中のログイン画面遷移で検知する。
    自動再ログインはしない。
    """
    if not has_tiktok_session_cookie(context.cookies()):
        raise SessionExpiredError(RELOGIN_MESSAGE)


# ---------------------------------------------------------------------------
# レスポンス捕捉
# ---------------------------------------------------------------------------

def extract_tiktok_endpoint_name(url: str) -> Optional[str]:
    """捕捉対象の内部APIエンドポイントURLなら名前を、対象外ならNoneを返す。"""
    for pattern, name in ENDPOINT_PATTERNS:
        if pattern.match(url):
            return name
    return None


class TikTokCapture(InstagramCapture):
    """TikTokのAPIレスポンスを溜めるバッファ(InstagramCaptureの判定条件をTikTok用に差し替えたもの)。

    429・ログイン画面遷移の検知はInstagramCaptureと共通。CAPTCHAは
    (1)CAPTCHA配信URLへの通信 (2)チャレンジURLへの遷移 (3)画面上のCAPTCHA要素 のいずれかで検知する。
    """

    service_name = "TikTok"
    relogin_message = RELOGIN_MESSAGE

    def endpoint_name(self, url: str) -> Optional[str]:
        return extract_tiktok_endpoint_name(url)

    def is_login_url(self, url: str) -> bool:
        return is_tiktok_login_url(url)

    def is_challenge_url(self, url: str) -> bool:
        return is_tiktok_challenge_url(url)

    def mark_captcha_request(self, url: str) -> None:
        """CAPTCHA配信への通信ならチャレンジ検知フラグを立てる。"""
        if is_captcha_request_url(url):
            logger.warning("CAPTCHA配信への通信を検知しました: %s", url)
            self._challenge = True

    def _on_response(self, response: Response) -> None:
        if is_captcha_request_url(response.url):
            self.mark_captcha_request(response.url)
            return
        super()._on_response(response)

    def raise_if_blocked(self, page: Optional[Page] = None) -> None:
        if page is not None and not self._challenge and _captcha_in_dom(page):
            logger.warning("画面上のCAPTCHA要素を検知しました")
            self._challenge = True
        super().raise_if_blocked(page)


def _captcha_in_dom(page: Page) -> bool:
    for selector in CAPTCHA_SELECTORS:
        try:
            if page.query_selector(selector) is not None:
                return True
        except Exception as exc:
            logger.warning("CAPTCHA要素の確認に失敗しました(%s): %s", selector, exc)
    return False


# ---------------------------------------------------------------------------
# パーサー
# ---------------------------------------------------------------------------

def _is_tiktok_item(node: dict) -> bool:
    """TikTok動画/フォト投稿ノードらしいか(itemList要素・itemStruct)。"""
    item_id = node.get("id")
    return (
        isinstance(item_id, (str, int))
        and not isinstance(item_id, bool)
        and "createTime" in node
        and any(isinstance(node.get(key), dict) for key in ("video", "imagePost", "stats", "statsV2"))
    )


def iter_tiktok_items(payload: Any) -> Iterable[dict]:
    """ペイロードから動画ノードを再帰探索で順に取り出す(見つけたノードの内側には潜らない)。"""
    if isinstance(payload, dict):
        if _is_tiktok_item(payload):
            yield payload
            return
        for value in payload.values():
            yield from iter_tiktok_items(value)
    elif isinstance(payload, list):
        for value in payload:
            yield from iter_tiktok_items(value)


def _stat(node: dict, key: str) -> int:
    """stats(数値)を優先し、無ければstatsV2(文字列の数値)から読む。"""
    for container in ("stats", "statsV2"):
        value = _dig(node, container, key)
        if value is not None:
            return _to_int(value)
    return 0


def _first_str(values: Iterable[Any]) -> Optional[str]:
    for value in values:
        if isinstance(value, str) and value:
            return value
    return None


def _parse_media(node: dict) -> list[MediaItem]:
    items: list[MediaItem] = []
    images = _dig(node, "imagePost", "images")
    if isinstance(images, list) and images:
        for image in images:
            url_list = _dig(image, "imageURL", "urlList")
            url = _first_str(url_list) if isinstance(url_list, list) else None
            if url:
                items.append(MediaItem(type="photo", url=url))
            else:
                logger.warning("media: 画像URLが見つからないため1件スキップします(id=%s)", node.get("id"))
        return items
    video = node.get("video") if isinstance(node.get("video"), dict) else {}
    cover = _first_str(video.get(key) for key in COVER_KEYS)
    if cover:
        items.append(MediaItem(type="photo", url=cover))
    video_url = _first_str(video.get(key) for key in VIDEO_URL_KEYS)
    if video_url:
        items.append(MediaItem(type="video", url=video_url))
    return items


def _author(node: dict) -> tuple[Optional[str], str]:
    author = node.get("author")
    if isinstance(author, dict):
        handle = author.get("uniqueId")
        return (handle if isinstance(handle, str) and handle else None), author.get("nickname") or ""
    if isinstance(author, str) and author:
        return author, ""
    return None, ""


def _parse_tiktok_node(
    node: dict, endpoint: str, source_query: Optional[str], collected_at: str
) -> Optional[Post]:
    video_id = str(node["id"])
    handle, name = _author(node)
    if not handle:
        logger.warning("%s: 投稿者(author.uniqueId)が見つからないため1件スキップします(id=%s)", endpoint, video_id)
        return None
    language = node.get("textLanguage")
    return Post(
        id=video_id,
        url=f"https://www.tiktok.com/@{handle}/video/{video_id}",
        author_handle=handle,
        author_name=name,
        created_at=_timestamp_to_iso(node.get("createTime")),
        text=node.get("desc") or "",
        like_count=_stat(node, "diggCount"),
        repost_count=_stat(node, "shareCount"),
        reply_count=_stat(node, "commentCount"),
        quote_count=0,
        lang=language if isinstance(language, str) and language else None,
        media=_parse_media(node),
        source_query=source_query,
        collected_at=collected_at,
    )


def parse_tiktok_payload(
    payload: dict,
    endpoint: str = "",
    source_query: Optional[str] = None,
    collected_at: Optional[str] = None,
) -> list[Post]:
    """TikTokのAPIペイロード1件からPostのリストを抽出する。

    - like_count=diggCount(いいね)、reply_count=commentCount、repost_count=shareCount(シェア数)。
      quote_countはTikTokに該当概念がないため常に0(collectCount=保存数は引用ではないので使わない)
    - 再生数(playCount)・保存数(collectCount)はPostスキーマに項目がないため保持しない
    - 作者による絞り込みは呼び出し側(tt_user_posts.filter_by_author)で行う
    """
    collected_at = collected_at or now_iso()
    posts: list[Post] = []
    for node in iter_tiktok_items(payload):
        post = _parse_tiktok_node(node, endpoint, source_query, collected_at)
        if post is not None:
            posts.append(post)
    return posts
